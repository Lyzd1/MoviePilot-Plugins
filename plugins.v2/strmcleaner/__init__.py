import os
import platform
import threading
import time
import traceback
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.chain.storage import StorageChain
from app import schemas
from app.helper.storage import StorageHelper

batch_lock = threading.Lock()
queue_lock = threading.Lock()
flood_lock = threading.Lock()


@dataclass
class EventEntry:
    path: Path
    is_directory: bool
    is_music: bool
    storage_type: str = ""
    storage_path: str = ""
    local_storage_type: Optional[str] = None
    local_storage_path: Optional[str] = None


@dataclass
class BatchGroup:
    parent_dir: str
    entries: List[EventEntry] = field(default_factory=list)
    has_folder_event: bool = False


@dataclass
class DeletionTask:
    task_type: str
    is_music: bool = False
    file_path: Optional[Path] = None
    folder_path: Optional[Path] = None
    storage_type: str = ""
    storage_path: str = ""
    local_storage_type: Optional[str] = None
    local_storage_path: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    processed: bool = False


@dataclass
class FloodRecord:
    timestamp: datetime
    file_count: int


@dataclass
class DeletionResult:
    file_path: Path
    task_type: str
    is_music: bool = False
    success: bool = False
    storage_type: Optional[str] = None
    storage_path: Optional[str] = None
    metadata_deleted: int = 0
    files_deleted: int = 0


class FileMonitorHandler(FileSystemEventHandler):
    def __init__(self, monpath: str, plugin: "StrmCleaner"):
        super().__init__()
        self._watch_path = monpath
        self.plugin = plugin

    def on_created(self, event):
        if event.is_directory:
            return
        file_path = Path(event.src_path)
        if file_path.suffix.lower() == ".strm":
            self.plugin._on_strm_created(file_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        src_path = Path(event.src_path)
        dest_path = Path(event.dest_path)
        if src_path.suffix.lower() == ".strm" or dest_path.suffix.lower() == ".strm":
            self.plugin._on_strm_moved(src_path, dest_path)

    def on_deleted(self, event):
        file_path = Path(event.src_path)

        # 防雪崩已触发时，仅丢弃新事件的采集，但保留批量计时器，
        # 让已入队/已入批次的任务继续按 延迟 正常推进；否则延迟会被绕过。
        if self.plugin._flood_triggered:
            logger.debug(f"[StrmCleaner] 防雪崩已触发，忽略事件: {file_path}")
            return

        if event.is_directory:
            self.plugin._handle_directory_deleted(file_path)
            return

        if file_path.suffix.lower() != ".strm":
            return

        self.plugin._handle_file_deleted(file_path)


class StrmCleaner(_PluginBase):
    plugin_name = "StrmCleaner"
    plugin_desc = "监控STRM文件及文件夹删除，联动清理openlist/local云盘文件及元数据"
    plugin_icon = "Ombi_A.png"
    plugin_version = "1.6"
    plugin_author = "Lyzd1"
    author_url = "https://github.com/Lyzd1"
    plugin_config_prefix = "strmcleaner_"
    plugin_order = 0
    auth_level = 1

    METADATA_EXTENSIONS = [
        ".nfo", ".xml", ".json",
        ".jpg", ".jpeg", ".png", ".webp", ".tbn", ".fanart", ".gif", ".bmp",
        ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".pgs", ".smi", ".rt", ".sbv",
    ]

    MUSIC_EXTENSIONS = [
        ".flac", ".mp3", ".wav", ".aac", ".m4a", ".ogg", ".wma",
        ".alac", ".ape", ".dsd", ".dff", ".dsf", ".aiff", ".pcm", ".opus",
    ]

    LYRICS_EXTENSIONS = [
        ".lrc", ".lyric", ".txt",
    ]

    _enabled = False
    _notify = False
    _delayed_deletion = True
    _delay_seconds = 30
    _delete_metadata = False
    _flood_protection_enabled = True
    _flood_threshold = 50
    _flood_window_seconds = 60

    path_mappings = ""
    music_prefixes = ""

    _api_url = ""
    _api_token = ""

    _storagechain = None
    _observer = None
    _batch_timer: Optional[threading.Timer] = None
    _batch_generation: int = 0
    _stopped: bool = False

    batch: Dict[str, BatchGroup] = {}
    deletion_queue: List[DeletionTask] = []
    flood_records: List[FloodRecord] = []
    _flood_triggered: bool = False
    _file_state: Dict[str, datetime] = {}

    def __choose_observer(self):
        system = platform.system()
        try:
            if system == "Linux":
                from watchdog.observers.inotify import InotifyObserver
                return InotifyObserver()
            elif system == "Darwin":
                from watchdog.observers.fsevents import FSEventsObserver
                return FSEventsObserver()
            elif system == "Windows":
                from watchdog.observers.read_directory_changes import WindowsApiObserver
                return WindowsApiObserver()
        except Exception as e:
            logger.warning(f"[StrmCleaner] 导入模块错误：{e}，将使用 PollingObserver")
        return PollingObserver()

    def init_plugin(self, config: dict = None):
        logger.info("[StrmCleaner] 正在初始化")

        self._storagechain = StorageChain()

        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", False)
            self._delayed_deletion = config.get("delayed_deletion", True)
            self._delete_metadata = config.get("delete_metadata", False)
            self._flood_protection_enabled = config.get("flood_protection_enabled", True)
            self._flood_threshold = int(config.get("flood_threshold", 50))
            self._flood_window_seconds = int(config.get("flood_window_seconds", 60))
            self.path_mappings = config.get("path_mappings") or ""
            self.music_prefixes = config.get("music_prefixes") or ""

            delay_seconds = config.get("delay_seconds", 30)
            self._delay_seconds = max(10, min(300, int(delay_seconds))) if delay_seconds else 30

        self.stop_service()

        self.batch = {}
        self.deletion_queue = []
        self.flood_records = []
        self._flood_triggered = False
        self._file_state = {}
        self._batch_generation = 0

        if not self._enabled:
            logger.info("[StrmCleaner] 插件未启用")
            return

        # 重新启用：清除停止标志，允许后续批次/队列正常处理
        self._stopped = False

        if self._delayed_deletion:
            logger.info(f"[StrmCleaner] 延迟删除已启用，延迟 {self._delay_seconds}s")
        else:
            logger.info("[StrmCleaner] 立即删除模式")

        self._auto_fetch_openlist_config()

        monitor_dirs = self._collect_monitor_dirs()
        if not monitor_dirs:
            logger.warning("[StrmCleaner] 未配置任何监控目录")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="StrmCleaner",
                    text="未配置路径映射，无法启动监控。",
                )
            return

        try:
            observer = self.__choose_observer()
            self._observer = observer
            handler = FileMonitorHandler("", self)
            for mon_dir in monitor_dirs:
                observer.schedule(handler, mon_dir, recursive=True)
                logger.info(f"[StrmCleaner] 监控目录: {mon_dir}")
            observer.daemon = True
            observer.start()
        except Exception as e:
            logger.error(f"[StrmCleaner] 启动监控失败: {e}")

        music_list = [p.strip() for p in self.music_prefixes.split("\n") if p.strip()]
        logger.info(
            f"[StrmCleaner] 已启动 | 监控: {len(monitor_dirs)} 个目录"
            f" | 音乐前缀: {music_list}"
            f" | 延迟: {self._delay_seconds}s"
            f" | 防雪崩: {self._flood_threshold}/{self._flood_window_seconds}s"
        )

    def _auto_fetch_openlist_config(self):
        has_openlist = any(
            seg in line
            for line in self.path_mappings.split("\n") if line.strip()
            for seg in (":openlist:", ":alist:")
        )
        if not has_openlist:
            return
        if self._api_url and self._api_token:
            return
        try:
            storages = StorageHelper.get_storagies()
            for s in storages:
                if s.type in ("openlist", "alist"):
                    s_url = s.config.get("host") or s.config.get("url")
                    s_token = s.config.get("token") or s.config.get("password")
                    if s_url and s_token:
                        if not self._api_url:
                            self._api_url = s_url.rstrip("/")
                        if not self._api_token:
                            self._api_token = s_token
                        logger.info(f"[StrmCleaner] 自动获取到 OpenList 配置: {s.name}")
                        break
        except Exception as e:
            logger.error(f"[StrmCleaner] 自动获取 OpenList 配置失败: {e}")

    def _collect_monitor_dirs(self) -> List[str]:
        dirs = set()
        for line in self.path_mappings.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            strm_path = line.split(":")[0].strip()
            if strm_path:
                dirs.add(strm_path)
        return list(dirs)

    def _match_mapping(self, strm_path: str) -> Optional[Tuple[str, Tuple[str, str, Optional[str], Optional[str]]]]:
        """
        在已配置的路径映射中匹配 strm_path：
        - 必须命中完整前缀边界（避免 /a/b 误匹配 /a/bc）
        - 多命中时取最长前缀（避免短前缀覆盖更精确的长前缀）
        """
        mappings = self._parse_all_mappings()
        best_prefix: Optional[str] = None
        best_value: Optional[Tuple[str, str, Optional[str], Optional[str]]] = None
        for raw_prefix, value in mappings.items():
            prefix = raw_prefix.rstrip("/\\")
            if not prefix:
                # 根映射（"/"）：所有绝对路径都匹配，按长度 0 处理
                if strm_path.startswith("/"):
                    if best_prefix is None or 0 > len(best_prefix):
                        best_prefix = ""
                        best_value = value
                continue
            if strm_path == prefix or strm_path.startswith(prefix + "/"):
                if best_prefix is None or len(prefix) > len(best_prefix):
                    best_prefix = prefix
                    best_value = value
        if best_value is None:
            return None
        return best_prefix, best_value

    def _parse_path_mapping(self, strm_path: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        matched = self._match_mapping(strm_path)
        if not matched:
            return None, None, None, None
        strm_prefix, (storage_type, storage_prefix, local_type, local_prefix) = matched
        relative = strm_path[len(strm_prefix):].lstrip("/\\")
        relative_no_ext = relative
        if relative_no_ext.lower().endswith(".strm"):
            relative_no_ext = relative_no_ext[:-5]
        storage_file_path = storage_prefix.rstrip("/") + "/" + relative_no_ext if relative_no_ext else storage_prefix
        local_file_path = None
        if local_type and local_prefix:
            local_file_path = local_prefix.rstrip("/") + "/" + relative_no_ext if relative_no_ext else local_prefix
        return storage_type, storage_file_path, local_type, local_file_path

    def _parse_folder_mapping(self, strm_folder_path: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        matched = self._match_mapping(strm_folder_path)
        if not matched:
            return None, None, None, None
        strm_prefix, (storage_type, storage_prefix, local_type, local_prefix) = matched
        relative = strm_folder_path[len(strm_prefix):].lstrip("/\\")
        storage_folder = storage_prefix.rstrip("/") + "/" + relative if relative else storage_prefix
        local_folder = None
        if local_type and local_prefix:
            local_folder = local_prefix.rstrip("/") + "/" + relative if relative else local_prefix
        return storage_type, storage_folder, local_type, local_folder

    def _parse_all_mappings(self) -> Dict[str, Tuple[str, str, Optional[str], Optional[str]]]:
        result = {}
        if not self.path_mappings:
            return result
        for line in self.path_mappings.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            try:
                parts = line.split(":", 4)
                local_type = None
                local_prefix = None
                if len(parts) == 2:
                    strm_path, storage_path = parts
                    storage_type = "local"
                elif len(parts) == 3:
                    strm_path, storage_type, storage_path = parts
                elif len(parts) == 5:
                    strm_path, storage_type, storage_path, local_type, local_prefix = parts
                else:
                    logger.warning(f"[StrmCleaner] 无效路径映射: {line}")
                    continue
                result[strm_path.strip()] = (
                    self._normalize_storage_type(storage_type.strip()),
                    storage_path.strip(),
                    local_type.strip() if local_type else None,
                    local_prefix.strip() if local_prefix else None,
                )
            except Exception:
                logger.warning(f"[StrmCleaner] 解析路径映射失败: {line}")
        return result

    def _normalize_storage_type(self, storage_type: str) -> str:
        if storage_type == "openlist":
            return "alist"
        return storage_type

    def _display_storage_type(self, storage_type: str) -> str:
        if storage_type == "alist":
            return "openlist"
        return storage_type

    def _is_music_path(self, path: str) -> bool:
        if not self.music_prefixes:
            return False
        for prefix in self.music_prefixes.split("\n"):
            prefix = prefix.strip()
            if prefix and path.startswith(prefix):
                return True
        return False

    def _on_strm_created(self, file_path: Path):
        self._file_state[str(file_path)] = datetime.now()

    def _on_strm_moved(self, src_path: Path, dest_path: Path):
        self._file_state.pop(str(src_path), None)
        if dest_path.suffix.lower() == ".strm":
            self._file_state[str(dest_path)] = datetime.now()

    def _handle_directory_deleted(self, dir_path: Path):
        logger.info(f"[StrmCleaner] 监测到目录删除: {dir_path}")

        is_music = self._is_music_path(str(dir_path))
        storage_type, storage_path, local_type, local_path = self._parse_folder_mapping(str(dir_path))

        if not storage_type or not storage_path:
            logger.info(f"[StrmCleaner] 未找到目录映射，忽略: {dir_path}")
            return

        entry = EventEntry(
            path=dir_path,
            is_directory=True,
            is_music=is_music,
            storage_type=storage_type,
            storage_path=storage_path,
            local_storage_type=local_type,
            local_storage_path=local_path,
        )
        self._add_to_batch(entry)

    def _handle_file_deleted(self, file_path: Path):
        # 清理 _file_state 避免内存泄漏
        self._file_state.pop(str(file_path), None)

        storage_type, storage_path, local_type, local_path = self._parse_path_mapping(str(file_path))

        if not storage_type or not storage_path:
            logger.info(f"[StrmCleaner] 未找到路径映射，忽略: {file_path}")
            return

        is_music = self._is_music_path(str(file_path))
        category = "音乐" if is_music else "视频"
        logger.info(f"[StrmCleaner] 监测到{category}STRM文件删除: {file_path}")

        entry = EventEntry(
            path=file_path,
            is_directory=False,
            is_music=is_music,
            storage_type=storage_type,
            storage_path=storage_path,
            local_storage_type=local_type,
            local_storage_path=local_path,
        )
        self._add_to_batch(entry)

    def _add_to_batch(self, entry: EventEntry):
        with batch_lock:
            parent_dir = str(Path(entry.path).parent) if not entry.is_directory else str(entry.path)

            if parent_dir not in self.batch:
                self.batch[parent_dir] = BatchGroup(parent_dir=parent_dir)

            self.batch[parent_dir].entries.append(entry)
            if entry.is_directory:
                self.batch[parent_dir].has_folder_event = True

            # 新事件到来时重置计时器。由于旧计时器可能已被取消但仍未触发，
            # 每次重启都用“代数”令牌让旧回调作废，避免旧的 _process_batch 回调
            # 在新批次刚加入时（几乎零延迟）就把它们处理掉，从而绕过延迟删除。
            generation = self._batch_generation + 1
            self._batch_generation = generation

            if self._batch_timer:
                try:
                    self._batch_timer.cancel()
                except Exception:
                    pass

            if self._delayed_deletion:
                wait = self._delay_seconds
            else:
                wait = 3

            self._batch_timer = threading.Timer(wait, self._process_batch, args=[generation])
            self._batch_timer.daemon = True
            self._batch_timer.start()

    def _process_batch(self, generation: int = None):
        try:
            with batch_lock:
                # 旧代数回调：当前已有更新的计时器在跑，说明有新事件加入，
                # 绝不能处理这批（会绕过延迟），直接退出。
                if generation is not None and generation != self._batch_generation:
                    return
                if self._stopped:
                    return
                if not self.batch:
                    return
                groups = dict(self.batch)
                self.batch.clear()

            strm_count = sum(
                len([e for e in group.entries if not e.is_directory and e.path.suffix.lower() == ".strm"])
                for group in groups.values()
            )

            if self._check_flood_protection(strm_count):
                logger.warning("[StrmCleaner] 防雪崩触发，放弃本次批次")
                return

            if strm_count > 0:
                self._record_flood_event(strm_count)

            tasks: List[DeletionTask] = []
            folder_candidates: List[dict] = []

            for parent_dir, group in groups.items():
                is_folder_deletion = group.has_folder_event or not os.path.exists(parent_dir)

                if is_folder_deletion:
                    ref_entry = next(
                        (e for e in group.entries if e.is_directory),
                        group.entries[0] if group.entries else None,
                    )
                    if not ref_entry:
                        continue

                    folder_path = Path(parent_dir) if not any(
                        e.is_directory and str(e.path) == parent_dir for e in group.entries
                    ) else Path(parent_dir)

                    storage_type, storage_path, local_type, local_path = self._parse_folder_mapping(parent_dir)

                    if not storage_type:
                        continue

                    strm_count = len([e for e in group.entries if not e.is_directory and e.path.suffix.lower() == ".strm"])
                    folder_candidates.append({
                        "parent_dir": parent_dir,
                        "strm_count": strm_count,
                        "ref_entry": ref_entry,
                        "folder_path": folder_path,
                        "storage_type": storage_type,
                        "storage_path": storage_path,
                        "local_type": local_type,
                        "local_path": local_path,
                    })
                else:
                    strm_entries = [e for e in group.entries if not e.is_directory]
                    if strm_entries:
                        music_count = sum(1 for e in strm_entries if e.is_music)
                        video_count = len(strm_entries) - music_count
                        parts = []
                        if video_count:
                            parts.append(f"{video_count}个视频")
                        if music_count:
                            parts.append(f"{music_count}个音乐")
                        logger.info(
                            f"[StrmCleaner] 检测到STRM文件删除: {parent_dir} ({', '.join(parts)})"
                        )
                    for entry in strm_entries:
                        task = DeletionTask(
                            task_type="file",
                            is_music=entry.is_music,
                            file_path=entry.path,
                            storage_type=entry.storage_type,
                            storage_path=entry.storage_path,
                            local_storage_type=entry.local_storage_type,
                            local_storage_path=entry.local_storage_path,
                        )
                        tasks.append(task)

            if folder_candidates:
                folder_candidates.sort(key=lambda c: c["parent_dir"])
                top_folders: List[dict] = []
                for fc in folder_candidates:
                    if not any(
                        fc["parent_dir"].startswith(t["parent_dir"] + os.sep)
                        for t in top_folders
                    ):
                        top_folders.append(fc)
                    else:
                        logger.info(
                            f"[StrmCleaner] 跳过子文件夹删除: {fc['parent_dir']}"
                            f" (父文件夹 {fc['parent_dir'].split(os.sep)[-2]} 将被整体删除)"
                        )

                for fc in top_folders:
                    logger.info(
                        f"[StrmCleaner] 检测到文件夹删除: {fc['parent_dir']} ({fc['strm_count']}个STRM)"
                        f" → [{self._display_storage_type(fc['storage_type'])}] {fc['storage_path']}"
                    )
                    task = DeletionTask(
                        task_type="folder",
                        is_music=fc["ref_entry"].is_music,
                        folder_path=fc["folder_path"],
                        storage_type=fc["storage_type"],
                        storage_path=fc["storage_path"],
                        local_storage_type=fc["local_type"],
                        local_storage_path=fc["local_path"],
                    )
                    tasks.append(task)

            for task in tasks:
                self.deletion_queue.append(task)

            self._process_deletion_queue()

        except Exception as e:
            logger.error(f"[StrmCleaner] 处理批次失败: {e} - {traceback.format_exc()}")

    def _check_flood_protection(self, file_count: int) -> bool:
        # 已触发防雪崩后一律判定为“应终止本次批次”，避免继续删除
        if self._flood_triggered:
            return True
        if not self._flood_protection_enabled:
            return False
        if file_count == 0:
            return False

        now = datetime.now()
        cutoff = now.timestamp() - self._flood_window_seconds

        with flood_lock:
            self.flood_records = [r for r in self.flood_records if r.timestamp.timestamp() > cutoff]
            window_total = sum(r.file_count for r in self.flood_records) + file_count

        if window_total >= self._flood_threshold:
            logger.warning(
                f"[StrmCleaner] 防雪崩保护触发！{self._flood_window_seconds}s内删除 {window_total} 个文件，"
                f"超过阈值 {self._flood_threshold}，插件将自动停用"
            )

            self._flood_triggered = True
            self.batch.clear()
            self.deletion_queue.clear()

            if self._batch_timer:
                self._batch_timer.cancel()
                self._batch_timer = None

            self.stop_service()
            self._enabled = False

            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="StrmCleaner 防雪崩保护 - 已自动停用",
                    text=(
                        f"检测到短时间内大量 STRM 文件删除事件，可能为本地存储异常。\n\n"
                        f"时间窗口: {self._flood_window_seconds}s\n"
                        f"删除数量: {window_total} 个\n"
                        f"触发阈值: {self._flood_threshold} 个\n\n"
                        f"插件已自动停用，所有云盘删除操作已阻止。\n"
                        f"请检查本地存储状态，确认正常后手动重新启用插件。"
                    ),
                )

            return True

        return False

    def _record_flood_event(self, file_count: int):
        if self._flood_protection_enabled:
            with flood_lock:
                self.flood_records.append(FloodRecord(datetime.now(), file_count))

    def _process_deletion_queue(self):
        results: List[DeletionResult] = []

        with queue_lock:
            pending = [t for t in self.deletion_queue if not t.processed]

        for task in pending:
            if self._stopped:
                logger.info("[StrmCleaner] 服务已停止，中止删除任务")
                break
            try:
                result = self._execute_deletion(task)
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"[StrmCleaner] 执行删除任务失败: {e} - {traceback.format_exc()}")
            finally:
                task.processed = True

        with queue_lock:
            self.deletion_queue = [t for t in self.deletion_queue if not t.processed]

        if results and self._notify:
            self._send_batch_notification(results)

    def _execute_deletion(self, task: DeletionTask) -> Optional[DeletionResult]:
        if task.task_type == "folder":
            return self._execute_folder_deletion(task)
        elif task.task_type == "file":
            if task.is_music:
                return self._execute_music_file_deletion(task)
            else:
                return self._execute_video_file_deletion(task)
        return None

    def _execute_folder_deletion(self, task: DeletionTask) -> Optional[DeletionResult]:
        result = DeletionResult(
            file_path=task.folder_path,
            task_type="folder",
            is_music=task.is_music,
            storage_type=task.storage_type,
            storage_path=task.storage_path,
        )

        if task.folder_path and os.path.exists(task.folder_path):
            logger.info(f"[StrmCleaner] 文件夹已重新创建，跳过: {task.folder_path}")
            return None

        try:
            if task.storage_type == "alist" and self._api_url and self._api_token:
                deleted = self._call_openlist_api_delete_dir(task.storage_path)
            else:
                folder_item = schemas.FileItem(
                    storage=task.storage_type,
                    path=task.storage_path if task.storage_path.endswith("/") else task.storage_path + "/",
                    type="dir",
                )
                deleted = self._storagechain.delete_file(folder_item)

            if deleted:
                result.success = True
                logger.info(f"[StrmCleaner] 已删除云盘文件夹: [{self._display_storage_type(task.storage_type)}] {task.storage_path}")

                if task.local_storage_type == "openlistlocal" and task.local_storage_path:
                    if self._api_url and self._api_token:
                        if self._call_openlist_api_delete_dir(task.local_storage_path):
                            logger.info(f"[StrmCleaner] 同步删除本地目录: [{task.local_storage_type}] {task.local_storage_path}")
                    else:
                        local_item = schemas.FileItem(
                            storage="local",
                            path=task.local_storage_path if task.local_storage_path.endswith("/") else task.local_storage_path + "/",
                            type="dir",
                        )
                        if self._storagechain.delete_file(local_item):
                            logger.info(f"[StrmCleaner] 同步删除本地目录: {task.local_storage_path}")

            else:
                logger.error(f"[StrmCleaner] 删除云盘文件夹失败: [{self._display_storage_type(task.storage_type)}] {task.storage_path}")

        except Exception as e:
            logger.error(f"[StrmCleaner] 文件夹删除异常: {e} - {traceback.format_exc()}")

        return result

    def _execute_video_file_deletion(self, task: DeletionTask) -> Optional[DeletionResult]:
        from app.core.config import settings

        result = DeletionResult(
            file_path=task.file_path,
            task_type="file",
            is_music=False,
            storage_type=task.storage_type,
            storage_path=task.storage_path,
        )

        if task.file_path and os.path.exists(task.file_path):
            logger.info(f"[StrmCleaner] 文件已被重新创建，跳过: {task.file_path}")
            return None

        try:
            cloud_path = task.storage_path
            parent_dir = str(Path(cloud_path).parent)
            parent_item = schemas.FileItem(
                storage=task.storage_type,
                path=parent_dir if parent_dir.endswith("/") else parent_dir + "/",
                type="dir",
            )

            files = self._storagechain.list_files(parent_item, recursion=False)
            if not files:
                logger.info(f"[StrmCleaner] 云盘目录为空或不存在: [{self._display_storage_type(task.storage_type)}] {parent_dir}")
                return result

            base_name = Path(cloud_path).name
            found_video = None
            for f in files:
                if f.type == "file" and f.name.startswith(base_name):
                    if f.extension and f".{f.extension.lower()}" in settings.RMT_MEDIAEXT:
                        found_video = f
                        break

            if not found_video:
                logger.info(f"[StrmCleaner] 未找到云盘视频文件: [{self._display_storage_type(task.storage_type)}] {cloud_path}")
                return result

            if self._storagechain.delete_file(found_video):
                result.success = True
                result.files_deleted = 1
                logger.info(f"[StrmCleaner] 已删除云盘视频: [{self._display_storage_type(task.storage_type)}] {found_video.path}")
            else:
                logger.error(f"[StrmCleaner] 删除云盘视频失败: [{self._display_storage_type(task.storage_type)}] {found_video.path}")
                return result

            if self._delete_metadata:
                result.metadata_deleted = self._delete_cloud_metadata(task.storage_type, parent_item, Path(cloud_path))

        except Exception as e:
            logger.error(f"[StrmCleaner] 视频文件删除异常: {e} - {traceback.format_exc()}")

        return result

    def _execute_music_file_deletion(self, task: DeletionTask) -> Optional[DeletionResult]:
        result = DeletionResult(
            file_path=task.file_path,
            task_type="file",
            is_music=True,
            storage_type=task.storage_type,
            storage_path=task.storage_path,
        )

        if task.file_path and os.path.exists(task.file_path):
            logger.info(f"[StrmCleaner] 文件已被重新创建，跳过: {task.file_path}")
            return None

        try:
            cloud_path = task.storage_path
            parent_dir = str(Path(cloud_path).parent)
            parent_item = schemas.FileItem(
                storage=task.storage_type,
                path=parent_dir if parent_dir.endswith("/") else parent_dir + "/",
                type="dir",
            )

            files = self._storagechain.list_files(parent_item, recursion=False)
            if not files:
                logger.info(f"[StrmCleaner] 云盘目录为空或不存在: [{self._display_storage_type(task.storage_type)}] {parent_dir}")
                return result

            base_stem = Path(cloud_path).stem
            matched = []
            for f in files:
                if f.type == "file" and f.name.startswith(base_stem):
                    ext = f".{f.extension.lower()}" if f.extension else ""
                    if ext in self.MUSIC_EXTENSIONS or ext in self.LYRICS_EXTENSIONS:
                        matched.append(f)

            if not matched:
                logger.info(f"[StrmCleaner] 未找到云盘音乐文件: [{self._display_storage_type(task.storage_type)}] {cloud_path}")
                return result

            logger.info(f"[StrmCleaner] 找到 {len(matched)} 个关联音乐/歌词文件")

            deleted_count = 0
            for f in matched:
                if self._storagechain.delete_file(f):
                    deleted_count += 1
                    logger.info(f"[StrmCleaner] 已删除: [{self._display_storage_type(task.storage_type)}] {f.path}")

            if deleted_count > 0:
                result.success = True
                result.files_deleted = deleted_count

            if self._delete_metadata:
                result.metadata_deleted = self._delete_cloud_metadata(task.storage_type, parent_item, Path(cloud_path))

        except Exception as e:
            logger.error(f"[StrmCleaner] 音乐文件删除异常: {e} - {traceback.format_exc()}")

        return result

    def _delete_cloud_metadata(self, storage_type: str, parent_item: schemas.FileItem, cloud_file_path: Path) -> int:
        deleted = 0
        try:
            files = self._storagechain.list_files(parent_item, recursion=False)
            if not files:
                return 0

            base_stem = cloud_file_path.stem
            base_name = cloud_file_path.name

            for f in files:
                if f.type != "file":
                    continue
                should_delete = False
                f_stem = Path(f.name).stem
                f_ext = Path(f.name).suffix.lower()

                if f_stem.startswith(base_stem) and f_ext in self.METADATA_EXTENSIONS:
                    should_delete = True
                if f.name == f"{base_name}-mediainfo.json":
                    should_delete = True

                if should_delete:
                    if self._storagechain.delete_file(f):
                        deleted += 1
                        logger.info(f"[StrmCleaner] 已清理元数据: [{storage_type}] {f.path}")

            if deleted > 0:
                logger.info(f"[StrmCleaner] 共清理 {deleted} 个元数据文件")

        except Exception as e:
            logger.error(f"[StrmCleaner] 清理元数据异常: {e}")

        return deleted

    def _call_openlist_api_delete_dir(self, dir_path: str) -> bool:
        try:
            p = Path(dir_path)
            parent_dir = str(p.parent)
            dir_name = p.name

            payload = {"dir": parent_dir, "names": [dir_name]}
            data = json.dumps(payload).encode("utf-8")
            api_url = f"{self._api_url.rstrip('/')}/api/fs/remove"

            headers = {
                "Content-Type": "application/json",
                "Authorization": self._api_token,
                "User-Agent": "MoviePilot-StrmCleaner",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            with urllib.request.urlopen(req, timeout=10) as response:
                resp_body = response.read().decode("utf-8")
                if response.getcode() == 200:
                    try:
                        resp_data = json.loads(resp_body)
                        if resp_data.get("code") == 200:
                            logger.info(f"[StrmCleaner] OpenList API 成功删除目录: {dir_path}")
                            return True
                        else:
                            logger.warning(f"[StrmCleaner] OpenList API 返回失败: {resp_data.get('message')}")
                    except json.JSONDecodeError:
                        logger.error(f"[StrmCleaner] OpenList API 响应解析失败: {resp_body}")
                else:
                    logger.warning(f"[StrmCleaner] OpenList API 状态码: {response.getcode()}")

        except urllib.error.URLError as e:
            logger.error(f"[StrmCleaner] OpenList API 请求失败: {e}")
        except Exception as e:
            logger.error(f"[StrmCleaner] OpenList API 异常: {e}")

        return False

    def _send_batch_notification(self, results: List[DeletionResult]):
        if not results:
            return

        folder_results = [r for r in results if r.task_type == "folder" and r.success]
        video_results = [r for r in results if r.task_type == "file" and not r.is_music and r.success]
        music_results = [r for r in results if r.task_type == "file" and r.is_music and r.success]

        total_metadata = sum(r.metadata_deleted for r in results)
        total_files = sum(r.files_deleted for r in video_results) + sum(r.files_deleted for r in music_results)

        parts = []

        if folder_results:
            parts.append(f"文件夹: {len(folder_results)} 个")
            for r in folder_results[:5]:
                parts.append(f"  - {r.file_path.name} → [{self._display_storage_type(r.storage_type)}]")
            if len(folder_results) > 5:
                parts.append(f"  ... 等 {len(folder_results) - 5} 个")

        if video_results:
            parts.append(f"视频文件: {len(video_results)} 个")
            for r in video_results[:5]:
                parts.append(f"  - {r.file_path.name} → [{self._display_storage_type(r.storage_type)}]")
            if len(video_results) > 5:
                parts.append(f"  ... 等 {len(video_results) - 5} 个")

        if music_results:
            parts.append(f"音乐文件: {len(music_results)} 个")
            for r in music_results[:5]:
                parts.append(f"  - {r.file_path.name} → [{self._display_storage_type(r.storage_type)}] {r.files_deleted} 个关联文件")
            if len(music_results) > 5:
                parts.append(f"  ... 等 {len(music_results) - 5} 个")

        summary = []
        if self._delete_metadata and total_metadata > 0:
            summary.append(f"已清理元数据 {total_metadata} 个")

        if summary:
            parts.append("")
            parts.extend(summary)

        mode = "延迟删除" if self._delayed_deletion else "立即删除"

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=f"StrmCleaner - 批量处理 {len(results)} 个",
            text=f"{mode}完成\n\n" + "\n".join(parts),
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        logger.debug("[StrmCleaner] 停止服务")

        # 先置停止标志，让正在执行的 _process_batch / _process_deletion_queue 立即中止
        self._stopped = True

        if self._observer:
            try:
                self._observer.stop()
                self._observer.join()
            except Exception as e:
                logger.error(f"[StrmCleaner] 停止 observer 失败: {e}")
        self._observer = None

        # 使已到期的批次定时器失效，避免停止期间继续处理
        with batch_lock:
            self._batch_generation += 1
            if self._batch_timer:
                try:
                    self._batch_timer.cancel()
                except Exception:
                    pass
                self._batch_timer = None

        # 停止/重载插件时绝不立即执行删除：
        # MoviePilot 在保存配置、更新插件、系统设置变更、重启时都会调用 stop_service，
        # 若在此处强制清空队列并执行删除，延迟删除保护会被完全绕过（删除立即发生）。
        # 未到延迟时间的批次/任务在此丢弃，仅记录日志。
        dropped_events = 0
        dropped_tasks = 0
        with batch_lock:
            if self.batch:
                dropped_events = sum(len(g.entries) for g in self.batch.values())
                self.batch.clear()
        with queue_lock:
            if self.deletion_queue:
                dropped_tasks = len([t for t in self.deletion_queue if not t.processed])
                self.deletion_queue.clear()

        if dropped_events or dropped_tasks:
            logger.warning(
                f"[StrmCleaner] 停止服务：丢弃 {dropped_events} 个批次事件 / {dropped_tasks} 个待删除任务"
                f"（延迟删除保护，未执行删除，云盘对应文件需手动处理或等待下次删除事件）"
            )

        logger.debug("[StrmCleaner] 服务已停止")

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "StrmCleaner - STRM 文件/文件夹联动删除",
                                            "text": "监控 STRM 文件及文件夹的删除事件，自动清理 openlist/local 云盘上的对应文件。支持视频和音乐两种删除模式，通过音乐前缀自动识别。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "delayed_deletion", "label": "启用延迟删除"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay_seconds",
                                            "label": "延迟时间(秒)",
                                            "type": "number",
                                            "placeholder": "30",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "delete_metadata", "label": "清理元数据文件"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"style": "margin: 16px 0;"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "路径映射",
                                            "rows": 5,
                                            "placeholder": "每行一个映射，格式: STRM目录:存储类型:云盘目录[:openlistlocal:本地目录]\n例如: /strm/video:local:/media/video\n/strm/anime:openlist:/cloud/anime:openlistlocal:/mnt/local/anime\n/strm/music:openlist:/cloud/music:openlistlocal:/mnt/local/music",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "music_prefixes",
                                            "label": "音乐目录前缀",
                                            "rows": 2,
                                            "placeholder": "每行一个前缀，匹配到则走音乐删除逻辑\n例如: /strm/music\n/strm/音乐",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"style": "margin: 16px 0;"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "title": "防雪崩保护",
                                            "text": "短时间内大量文件删除时，自动判定为存储异常并停用插件，防止批量误删。需手动排查后重新启用。文件夹删除不计入计数。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "flood_protection_enabled", "label": "启用防雪崩保护"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "flood_threshold",
                                            "label": "触发阈值(文件数)",
                                            "type": "number",
                                            "placeholder": "50",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "flood_window_seconds",
                                            "label": "统计窗口(秒)",
                                            "type": "number",
                                            "placeholder": "60",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "delayed_deletion": True,
            "delay_seconds": 30,
            "delete_metadata": False,
            "path_mappings": "",
            "music_prefixes": "",
            "flood_protection_enabled": True,
            "flood_threshold": 50,
            "flood_window_seconds": 60,
        }

    def get_page(self) -> List[dict]:
        pass
