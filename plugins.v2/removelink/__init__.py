import os
import platform
import threading
import time
import traceback
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.core.event import eventmanager
from app.schemas.types import EventType
from app.chain.storage import StorageChain
from app import schemas

# === 新增引用 ===
from app.helper.storage import StorageHelper
# ================

state_lock = threading.Lock()
deletion_queue_lock = threading.Lock()


class FileInfo(NamedTuple):
    """文件信息"""

    inode: int
    add_time: datetime


@dataclass
class DeletionTask:
    """延迟删除任务"""

    file_path: Path
    timestamp: datetime
    task_type: str  # "hardlink" 或 "strm"
    deleted_inode: Optional[int] = None  # 仅 hardlink 任务使用
    processed: bool = False


@dataclass
class DeletionResult:
    """删除结果"""

    file_path: Path
    task_type: str  # "hardlink" 或 "strm"
    success: bool
    storage_type: Optional[str] = None
    storage_path: Optional[str] = None
    scrap_deleted: int = 0
    dirs_deleted: int = 0
    history_deleted: bool = False
    hardlink_count: int = 0  # 仅 hardlink 任务使用


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控处理
    """

    def __init__(
        self, monpath: str, sync: Any, monitor_type: str = "hardlink", **kwargs
    ):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync
        self.monitor_type = monitor_type  # "hardlink" 或 "strm"

    def _is_excluded_file(self, file_path: Path) -> bool:
        """检查文件是否应该被排除"""
        # 排除临时文件
        if file_path.suffix in [".!qB", ".part", ".mp", ".tmp", ".temp"]:
            return True
        # 检查关键字过滤
        if self.sync.exclude_keywords:
            for keyword in self.sync.exclude_keywords.split("\n"):
                if keyword and keyword in str(file_path):
                    logger.debug(f"{file_path} 命中过滤关键字 {keyword}，不处理")
                    return True
        return False

    def _add_file_to_state(self, file_path: Path):
        """添加文件到状态管理"""
        if self._is_excluded_file(file_path):
            return

        with state_lock:
            try:
                if not file_path.exists():
                    return
                stat_info = file_path.stat()
                file_info = FileInfo(inode=stat_info.st_ino, add_time=datetime.now())
                self.sync.file_state[str(file_path)] = file_info
                logger.debug(f"添加文件到监控：{file_path}")
            except (OSError, PermissionError) as e:
                logger.debug(f"无法访问文件 {file_path}：{e}")
            except Exception as e:
                logger.error(f"新增文件记录失败：{str(e)}")

    def on_created(self, event):
        if event.is_directory:
            return
        file_path = Path(event.src_path)
        logger.info(f"监测到新增文件：{file_path}")
        self._add_file_to_state(file_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        # 处理移动事件：移除源文件，添加目标文件
        src_path = Path(event.src_path)
        dest_path = Path(event.dest_path)

        logger.info(f"监测到文件移动：{src_path} -> {dest_path}")

        # 从状态中移除源文件
        with state_lock:
            self.sync.file_state.pop(str(src_path), None)

        # 添加目标文件
        self._add_file_to_state(dest_path)

    def on_deleted(self, event):
        file_path = Path(event.src_path)
        if event.is_directory:
            # STRM 监控：文件夹删除时直接删除云盘对应文件夹
            if self.monitor_type == "strm":
                self.sync.handle_strm_folder_deleted(file_path)
                return
            # 硬链接监控：文件夹删除触发删除种子
            if self.sync._delete_torrents:
                logger.info(f"监测到删除文件夹：{file_path}")
                eventmanager.send_event(
                    EventType.DownloadFileDeleted, {"src": str(file_path)}
                )
            return
        if file_path.suffix in [".!qB", ".part", ".mp"]:
            return
        logger.info(f"监测到删除文件：{file_path}")
        # 命中过滤关键字不处理
        if self.sync.exclude_keywords:
            for keyword in self.sync.exclude_keywords.split("\n"):
                if keyword and keyword in str(file_path):
                    logger.info(f"{file_path} 命中过滤关键字 {keyword}，不处理")
                    return

        # 根据监控类型处理删除事件
        if self.monitor_type == "strm":
            # STRM 监控目录：只处理 strm 文件删除，其他文件忽略
            if file_path.suffix.lower() == ".strm":
                self.sync.handle_strm_deleted(file_path)
            # 其他文件（如刮削文件）在 STRM 监控目录中被忽略，避免触发硬链接清理
        else:
            # 硬链接监控目录：处理硬链接文件删除
            self.sync.handle_deleted(file_path)


def updateState(monitor_dirs: List[str]):
    """
    更新监控目录的文件列表
    """
    # 记录开始时间
    start_time = time.time()
    file_state = {}
    init_time = datetime.now()
    error_count = 0

    for mon_path in monitor_dirs:
        if not os.path.exists(mon_path):
            logger.warning(f"监控目录不存在：{mon_path}")
            continue

        try:
            for root, _, files in os.walk(mon_path):
                for file_name in files:
                    file_path = Path(root) / file_name
                    try:
                        if not file_path.exists():
                            continue
                        # 获取文件统计信息
                        stat_info = file_path.stat()
                        # 记录文件信息
                        file_info = FileInfo(inode=stat_info.st_ino, add_time=init_time)
                        file_state[str(file_path)] = file_info
                    except (OSError, PermissionError) as e:
                        error_count += 1
                        logger.debug(f"无法访问文件 {file_path}：{e}")
        except Exception as e:
            logger.error(f"扫描目录 {mon_path} 时发生错误：{e}")

    # 记录结束时间
    end_time = time.time()
    # 计算耗时
    elapsed_time = end_time - start_time

    logger.info(
        f"更新文件列表完成，共计 {len(file_state)} 个文件，耗时 {elapsed_time:.2f} 秒"
    )
    if error_count > 0:
        logger.warning(f"扫描过程中有 {error_count} 个文件无法访问")

    return file_state


class RemoveLink(_PluginBase):
    # 插件名称
    plugin_name = "清理媒体文件"
    # 插件描述
    plugin_desc = "媒体文件清理工具：支持硬链接文件清理、STRM文件清理、刮削文件清理（元数据、图片、字幕）、转移记录清理、种子联动删除等功能"
    # 插件图标
    plugin_icon = "Ombi_A.png"
    # 插件版本
    plugin_version = "2.9"
    # 插件作者
    plugin_author = "Lyzd1,DzAvril"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "linkdeleted_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 1

    # 刮削文件扩展名（包括字幕文件）
    SCRAP_EXTENSIONS = [
        # 元数据文件
        ".nfo",
        ".xml",
        # 图片文件
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".tbn",
        ".fanart",
        ".gif",
        ".bmp",
        # 字幕文件
        ".srt",
        ".ass",
        ".ssa",
        ".sub",
        ".idx",
        ".vtt",
        ".sup",
        ".pgs",
        ".smi",
        ".rt",
        ".sbv",
        ".csf-bk",
        ".csf-tmp",
    ]

    # preivate property
    monitor_dirs = ""
    exclude_dirs = ""
    exclude_keywords = ""
    _enabled = False
    _notify = False
    _delete_scrap_infos = False
    _delete_torrents = False
    _delete_history = False
    _delayed_deletion = True
    _delay_seconds = 30
    _monitor_strm_deletion = False
    strm_path_mappings = ""
    _transferhistory = None
    _storagechain = None
    _observer = []
    # 监控目录的文件列表 {文件路径: FileInfo(inode, add_time)}
    file_state: Dict[str, FileInfo] = {}
    # 延迟删除队列
    deletion_queue: List[DeletionTask] = []
    # 延迟删除定时器
    _deletion_timer = None
    # 已删除的 STRM 文件夹路径集合（用于过滤文件夹内的文件事件）
    deleted_strm_folders: set = set()
    # AList API 配置
    _api_delete_empty_dirs = False
    _api_delete_url = ""
    _api_delete_token = ""

    @staticmethod
    def __choose_observer():
        """
        选择最优的监控模式
        """
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
        except Exception as error:
            logger.warn(f"导入模块错误：{error}，将使用 PollingObserver 监控目录")
        return PollingObserver()

    def init_plugin(self, config: dict = None):
        logger.info(f"初始化媒体文件清理插件")
        self._transferhistory = TransferHistoryOper()
        self._storagechain = StorageChain()

        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self.monitor_dirs = config.get("monitor_dirs")
            self.exclude_dirs = config.get("exclude_dirs") or ""
            self.exclude_keywords = config.get("exclude_keywords") or ""
            self._delete_scrap_infos = config.get("delete_scrap_infos")
            self._delete_torrents = config.get("delete_torrents")
            self._delete_history = config.get("delete_history")
            self._delayed_deletion = config.get("delayed_deletion", True)
            self._monitor_strm_deletion = config.get("monitor_strm_deletion", False)
            self.strm_path_mappings = config.get("strm_path_mappings") or ""
            # 验证延迟时间范围
            delay_seconds = config.get("delay_seconds", 30)
            self._delay_seconds = (
                max(10, min(300, int(delay_seconds))) if delay_seconds else 30
            )
            # AList API 配置
            self._api_delete_empty_dirs = config.get("api_delete_empty_dirs", False)
            self._api_delete_url = config.get("api_delete_url") or ""
            self._api_delete_token = config.get("api_delete_token") or ""

        # 停止现有任务
        self.stop_service()

        # 初始化延迟删除队列
        self.deletion_queue = []
        # 初始化已删除文件夹集合
        self.deleted_strm_folders = set()

        if self._enabled:
            # =========================================================
            # 自动配置逻辑：如果开启了 API 删除且 URL 或 Token 未配置，尝试从系统存储中获取
            # =========================================================
            if self._api_delete_empty_dirs and (not self._api_delete_url or not self._api_delete_token):
                try:
                    logger.debug("RemoveLink: AList API 清理已开启但 URL/Token 未配置，尝试从系统存储配置自动获取...")
                    storage_configs = StorageHelper.get_storagies()
                    for s in storage_configs:
                        if s.type in ['alist', 'openlist']:
                            s_url = s.config.get('host') or s.config.get('url')
                            s_token = s.config.get('token') or s.config.get('password')
                            
                            if s_url and s_token:
                                logger.info(f"RemoveLink: 自动检测到存储配置 [{s.name}]，将应用到 AList API 清理配置。")
                                if not self._api_delete_url:
                                    self._api_delete_url = s_url.rstrip('/')
                                if not self._api_delete_token:
                                    self._api_delete_token = s_token
                                break # 找到第一个符合的即可
                except Exception as e:
                    logger.error(f"RemoveLink: 自动读取系统存储配置失败: {e}")
            # =========================================================

            # 记录延迟删除配置状态
            if self._delayed_deletion:
                logger.info(f"延迟删除功能已启用，延迟时间: {self._delay_seconds} 秒")
                logger.info("延迟删除将同时应用于硬链接和STRM文件")
            else:
                logger.info("延迟删除功能已禁用，将使用立即删除模式")

            # 记录 STRM 监控配置状态
            strm_monitor_dirs = []
            if self._monitor_strm_deletion:
                logger.info("STRM 文件删除监控功能已启用")
                if self.strm_path_mappings:
                    mappings = self._parse_strm_path_mappings()
                    logger.info(f"配置了 {len(mappings)} 个 STRM 路径映射")
                    # 从映射配置中提取 STRM 监控目录
                    strm_monitor_dirs = list(mappings.keys())
                    logger.info(f"STRM 监控目录：{strm_monitor_dirs}")
                else:
                    logger.warning("STRM 监控已启用但未配置路径映射")
                
                if self._api_delete_empty_dirs:
                    if self._api_delete_url and self._api_delete_token:
                        logger.info(f"AList API 空目录清理功能已启用，URL: {self._api_delete_url}")
                    else:
                        logger.warning("AList API 空目录清理已启用，但 URL 或 Token 未配置（自动获取失败）")
            else:
                logger.info("STRM 文件删除监控功能已禁用")

            # 读取硬链接监控目录配置
            hardlink_monitor_dirs = []
            if self.monitor_dirs:
                hardlink_monitor_dirs = [
                    d.strip() for d in self.monitor_dirs.split("\n") if d.strip()
                ]
                logger.info(f"硬链接监控目录：{hardlink_monitor_dirs}")

            # 启动硬链接监控
            for mon_path in hardlink_monitor_dirs:
                if not mon_path:
                    continue
                try:
                    # 使用优化的监控器选择
                    observer = self.__choose_observer()
                    self._observer.append(observer)
                    observer.schedule(
                        FileMonitorHandler(mon_path, self, monitor_type="hardlink"),
                        mon_path,
                        recursive=True,
                    )
                    observer.daemon = True
                    observer.start()
                    logger.info(f"{mon_path} 的硬链接监控服务启动")
                except Exception as e:
                    err_msg = str(e)
                    # 特殊处理 inotify 限制错误
                    if "inotify" in err_msg and "reached" in err_msg:
                        logger.warn(
                            f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                            + """
                             echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                             echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                             sudo sysctl -p
                             """
                        )
                    else:
                        logger.error(f"{mon_path} 启动硬链接监控失败：{err_msg}")
                    self.systemmessage.put(
                        f"{mon_path} 启动硬链接监控失败：{err_msg}",
                        title="媒体文件清理",
                    )

            # 启动 STRM 监控
            for mon_path in strm_monitor_dirs:
                if not mon_path:
                    continue
                try:
                    # 使用优化的监控器选择
                    observer = self.__choose_observer()
                    self._observer.append(observer)
                    observer.schedule(
                        FileMonitorHandler(mon_path, self, monitor_type="strm"),
                        mon_path,
                        recursive=True,
                    )
                    observer.daemon = True
                    observer.start()
                    logger.info(f"{mon_path} 的 STRM 监控服务启动")
                except Exception as e:
                    err_msg = str(e)
                    # 特殊处理 inotify 限制错误
                    if "inotify" in err_msg and "reached" in err_msg:
                        logger.warn(
                            f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                            + """
                             echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                             echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                             sudo sysctl -p
                             """
                        )
                    else:
                        logger.error(f"{mon_path} 启动 STRM 监控失败：{err_msg}")
                    self.systemmessage.put(
                        f"{mon_path} 启动 STRM 监控失败：{err_msg}",
                        title="媒体文件清理",
                    )

            # 合并所有监控目录用于文件状态更新
            all_monitor_dirs = hardlink_monitor_dirs + strm_monitor_dirs

            # 更新监控集合 - 在所有线程停止后安全获取锁
            with state_lock:
                self.file_state = updateState(all_monitor_dirs)
                logger.debug("监控集合更新完成")

    def get_state(self) -> bool:
        return self._enabled

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
                    # 插件总体说明
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
                                            "title": "🧹 媒体文件清理插件",
                                            "text": "全面的媒体文件清理工具，支持硬链接文件清理和STRM文件清理两种模式，可独立启用。硬链接清理用于监控硬链接文件删除并自动清理相关文件；STRM清理用于监控STRM文件删除并删除对应的网盘文件。同时支持刮削文件清理（元数据、图片、字幕）、转移记录清理、种子联动删除等功能。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 公用配置
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
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
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
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
                                        "props": {
                                            "model": "delete_scrap_infos",
                                            "label": "清理刮削文件",
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
                                        "props": {
                                            "model": "delete_torrents",
                                            "label": "联动删除种子",
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
                                        "props": {
                                            "model": "delete_history",
                                            "label": "删除转移记录",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 延迟删除配置（通用）
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"style": "margin: 20px 0;"},
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
                                            "title": "⏰ 通用延迟删除配置",
                                            "text": "启用后，文件删除（包括硬链接和STRM文件）不会立即触发清理，而是等待指定时间后再检查。这可以防止媒体重整理或误操作导致的意外删除。",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delayed_deletion",
                                            "label": "启用延迟删除 (同时用于硬链接和STRM)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay_seconds",
                                            "label": "延迟时间(秒)",
                                            "type": "number",
                                            "min": 10,
                                            "max": 300,
                                            "placeholder": "30",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 硬链接清理配置分隔线
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"style": "margin: 20px 0;"},
                                    }
                                ],
                            },
                        ],
                    },
                    # 硬链接清理配置标题
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
                                            "type": "primary",
                                            "variant": "tonal",
                                            "title": "🔗 硬链接清理配置",
                                            "text": "监控硬链接文件删除，自动清理相关的硬链接文件、刮削文件和转移记录。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 硬链接监控目录配置
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
                                            "model": "monitor_dirs",
                                            "label": "硬链接监控目录",
                                            "rows": 5,
                                            "placeholder": "硬链接源目录及目标目录均需加入监控，每一行一个目录",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 硬链接排除配置
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "exclude_dirs",
                                            "label": "不删除目录",
                                            "rows": 3,
                                            "placeholder": "该目录下的文件不会被动删除，一行一个目录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "exclude_keywords",
                                            "label": "排除关键词",
                                            "rows": 3,
                                            "placeholder": "每一行一个关键词",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 硬链接配置说明
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
                                            "text": "硬链接监控：源目录和硬链接目录都需要添加到监控目录中；如需实现删除硬链接时不删除源文件，可把源文件目录配置到不删除目录中。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # STRM清理配置分隔线
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"style": "margin: 20px 0;"},
                                    }
                                ],
                            },
                        ],
                    },
                    # STRM清理配置标题
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
                                            "type": "success",
                                            "variant": "tonal",
                                            "title": "📺 STRM文件清理配置",
                                            "text": "监控STRM文件删除，自动删除网盘上对应的视频文件。监控目录会自动从路径映射中获取。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # STRM功能开关
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "monitor_strm_deletion",
                                            "label": "启用STRM文件监控",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # STRM路径映射配置
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
                                            "model": "strm_path_mappings",
                                            "label": "STRM路径映射",
                                            "rows": 4,
                                            "placeholder": "STRM目录:存储类型:网盘目录[:alistlocal:本地目录]，每行一个映射关系\n例如：/ssd/strm:u115:/media\n例如：/nas/strm:alipan:/阿里云盘/媒体:alistlocal:/mnt/local_media",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # AList API 删除空目录配置
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
                                            "title": "AList API 空目录清理 (可选)",
                                            "text": "启用后，当清理 Alist 上的 STRM 对应文件后，将调用 AList API 来删除空目录。仅当存储类型为 'alist' 时生效。如果不填写 URL 或 Token，将尝试自动从系统存储配置中读取。",
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
                                        "props": {
                                            "model": "api_delete_empty_dirs",
                                            "label": "启用 AList API 删除空目录",
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
                                            "model": "api_delete_url",
                                            "label": "AList URL (留空自动获取)",
                                            "placeholder": "例如: http://127.0.0.1:5244",
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
                                            "model": "api_delete_token",
                                            "label": "AList Token (留空自动获取)",
                                            "type": "password",
                                            "placeholder": "AList 管理员 Token",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # STRM配置说明
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
                                            "text": "STRM文件监控：启用后会自动监控映射中的STRM目录，当STRM文件删除时会查找并删除网盘上对应的视频文件。路径映射格式：STRM目录:存储类型:网盘目录[:本地存储类型:本地目录]，例如 /strm:alist:/remote:alistlocal:/local/media 表示 /strm/test.strm 对应 alist 上的 /remote/test 和本地的 /local/media/test。本地目录为可选配置，用于联动删除本地空目录。",
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "success",
                                            "variant": "tonal",
                                            "text": "支持的存储类型：local（本地存储）、alipan（阿里云盘）、u115（115网盘）、rclone（Rclone挂载）、alist（Alist挂载）。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 公用功能说明
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"style": "margin: 20px 0;"},
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
                                            "text": "联动删除种子需安装插件[下载器助手]并打开监听源文件事件。清理刮削文件功能会删除相关的.nfo、.jpg等元数据文件，请谨慎开启。",
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
            "delete_scrap_infos": False,
            "delete_torrents": False,
            "delete_history": False,
            "delayed_deletion": True,
            "delay_seconds": 30,
            "monitor_dirs": "",
            "exclude_dirs": "",
            "exclude_keywords": "",
            "monitor_strm_deletion": False,
            "strm_path_mappings": "",
            "api_delete_empty_dirs": False,
            "api_delete_url": "",
            "api_delete_token": "",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        logger.debug("开始停止服务")

        # 首先停止文件监控，防止新的删除事件
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
                    logger.error(f"停止目录监控失败：{str(e)}")
        self._observer = []
        logger.debug("文件监控已停止")

        # 停止延迟删除定时器
        if self._deletion_timer:
            try:
                self._deletion_timer.cancel()
                self._deletion_timer = None
                logger.debug("延迟删除定时器已停止")
            except Exception as e:
                logger.error(f"停止延迟删除定时器失败：{str(e)}")

        # 处理剩余的延迟删除任务
        tasks_to_process = []
        with deletion_queue_lock:
            if self.deletion_queue:
                logger.info(f"处理剩余的 {len(self.deletion_queue)} 个延迟删除任务")
                tasks_to_process = [
                    task for task in self.deletion_queue if not task.processed
                ]
                self.deletion_queue.clear()

        # 在锁外处理任务，避免死锁
        for task in tasks_to_process:
            self._execute_delayed_deletion(task)

        logger.debug("服务停止完成")

    def __is_excluded(self, file_path: Path) -> bool:
        """
        是否排除目录
        """
        for exclude_dir in self.exclude_dirs.split("\n"):
            if exclude_dir and exclude_dir in str(file_path):
                return True
        return False

    @staticmethod
    def scrape_files_left(path):
        """
        检查path目录是否只包含刮削文件
        """
        # 检查path下是否有目录
        for dir_path in os.listdir(path):
            if os.path.isdir(os.path.join(path, dir_path)):
                return False

        # 检查path下是否有非刮削文件
        for file in path.iterdir():
            if not file.suffix.lower() in RemoveLink.SCRAP_EXTENSIONS:
                return False
        return True

    def delete_scrap_infos(self, path):
        """
        清理path相关的刮削文件
        """
        if not self._delete_scrap_infos:
            return
        # 文件所在目录已被删除则退出
        if not os.path.exists(path.parent):
            return
        try:
            if not path.suffix.lower() in self.SCRAP_EXTENSIONS:
                # 清理与path相关的刮削文件
                name_prefix = path.stem
                for file in path.parent.iterdir():
                    if (
                        file.name.startswith(name_prefix)
                        and file.suffix.lower() in self.SCRAP_EXTENSIONS
                    ):
                        file.unlink()
                        logger.info(f"删除刮削文件：{file}")
        except Exception as e:
            logger.error(f"清理刮削文件发生错误：{str(e)}.")
        # 清理空目录
        self.delete_empty_folders(path)

    def delete_history(self, path):
        """
        清理path相关的转移记录
        """
        if not self._delete_history:
            return
        # 查找转移记录
        transfer_history = self._transferhistory.get_by_src(path)
        if transfer_history:
            # 删除转移记录
            self._transferhistory.delete(transfer_history.id)
            logger.info(f"删除转移记录：{transfer_history.id}")

    def delete_empty_folders(self, path):
        """
        从指定路径开始，逐级向上层目录检测并删除空目录，直到遇到非空目录或到达指定监控目录为止
        """
        # logger.info(f"清理空目录: {path}")
        while True:
            parent_path = path.parent
            if self.__is_excluded(parent_path):
                break
            # parent_path如已被删除则退出检查
            if not os.path.exists(parent_path):
                break
            # 如果当前路径等于监控目录之一，停止向上检查
            if parent_path in self.monitor_dirs.split("\n"):
                break

            # 若目录下只剩刮削文件，则清空文件夹
            try:
                if self.scrape_files_left(parent_path):
                    # 清除目录下所有文件
                    for file in parent_path.iterdir():
                        file.unlink()
                        logger.info(f"删除刮削文件：{file}")
            except Exception as e:
                logger.error(f"清理刮削文件发生错误：{str(e)}.")

            try:
                if not os.listdir(parent_path):
                    os.rmdir(parent_path)
                    logger.info(f"清理空目录：{parent_path}")
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="📁 目录清理",
                            text=f"🗑️ 清理空目录：{parent_path}",
                        )
                else:
                    break
            except Exception as e:
                logger.error(f"清理空目录发生错误：{str(e)}")

            # 更新路径为父目录，准备下一轮检查
            path = parent_path

    def _execute_delayed_deletion(self, task: DeletionTask) -> Optional[DeletionResult]:
        """
        执行延迟删除任务 - 路由到特定处理器
        返回 DeletionResult 对象
        """
        result = None
        try:
            if task.task_type == "hardlink":
                result = self._execute_hardlink_delayed_deletion(task)
            elif task.task_type == "strm":
                result = self._execute_strm_delayed_deletion(task)
            elif task.task_type == "strm_folder":
                result = self._execute_strm_folder_delayed_deletion(task)
            else:
                logger.warning(f"未知的延迟删除任务类型: {task.task_type}")

        except Exception as e:
            logger.error(f"执行延迟删除任务失败 ({task.task_type}): {str(e)} - {traceback.format_exc()}")
        finally:
            task.processed = True

        return result

    def _execute_strm_folder_delayed_deletion(self, task: DeletionTask) -> Optional[DeletionResult]:
        """
        执行 STRM 文件夹的延迟删除任务
        """
        logger.debug(f"开始执行延迟删除任务 (strm_folder): {task.file_path}")

        # 检查文件夹是否被重新创建
        if task.file_path.exists():
            logger.info(f"STRM 文件夹 {task.file_path} 已被重新创建，跳过删除操作")
            # 从已删除文件夹集合中移除
            self.deleted_strm_folders.discard(str(task.file_path))
            return None

        # 执行删除
        result = self._execute_strm_folder_deletion(task.file_path, send_notify=False)

        # 处理完成后从已删除文件夹集合中移除
        self.deleted_strm_folders.discard(str(task.file_path))

        return result
            
    def _execute_strm_delayed_deletion(self, task: DeletionTask) -> Optional[DeletionResult]:
        """
        执行 STRM 的延迟删除任务
        返回 DeletionResult 对象
        """
        logger.debug(f"开始执行延迟删除任务 (strm): {task.file_path}")

        # 检查文件是否属于已删除的文件夹（由文件夹删除统一处理）
        for deleted_folder in self.deleted_strm_folders.copy():
            if str(task.file_path).startswith(str(deleted_folder) + os.sep):
                logger.debug(f"文件 {task.file_path} 属于已删除文件夹 {deleted_folder}，跳过")
                return None

        # 1. 检查文件是否被重新创建
        if task.file_path.exists():
            logger.info(f"STRM 文件 {task.file_path} 已被重新创建，跳过删除操作")
            return None

        # 2. 执行实际的删除逻辑（不发送通知，由批量通知处理）
        logger.debug(
            f"STRM 文件 {task.file_path} 确认被删除，开始执行延迟删除操作"
        )
        return self._execute_strm_deletion(task.file_path, send_notify=False)

    def _execute_hardlink_delayed_deletion(self, task: DeletionTask) -> Optional[DeletionResult]:
        """
        执行硬链接的延迟删除任务
        返回 DeletionResult 对象
        """
        logger.debug(f"开始执行延迟删除任务 (hardlink): {task.file_path}")

        result = DeletionResult(
            file_path=task.file_path,
            task_type="hardlink",
            success=False
        )

        # 验证原文件是否仍然被删除（未被重新创建）
        if task.file_path.exists():
            logger.info(f"文件 {task.file_path} (hardlink) 已被重新创建，跳过删除操作")
            return None

        # 检查是否有相同inode的新文件（重新硬链接的情况）
        with state_lock:
            for path, file_info in self.file_state.items():
                if file_info.inode == task.deleted_inode and path != str(
                    task.file_path
                ):
                    # 检查文件是否在删除任务创建之后被添加到监控中
                    if file_info.add_time > task.timestamp:
                        logger.info(
                            f"检测到相同inode的新文件 {path}，添加时间 {file_info.add_time} 晚于删除时间 {task.timestamp}，可能是重新硬链接，跳过删除操作"
                        )
                        return None

        # 延迟执行所有删除相关操作
        logger.debug(
            f"文件 {task.file_path} 确认被删除且无重新硬链接，开始执行延迟删除操作"
        )

        # 清理刮削文件
        self.delete_scrap_infos(task.file_path)
        if self._delete_torrents:
            # 只有非刮削文件才发送 DownloadFileDeleted 事件
            if task.file_path.suffix.lower() not in self.SCRAP_EXTENSIONS:
                eventmanager.send_event(
                    EventType.DownloadFileDeleted, {"src": str(task.file_path)}
                )
        # 删除转移记录
        if self._delete_history:
            self.delete_history(str(task.file_path))
            result.history_deleted = True

        # 查找并删除硬链接文件
        deleted_files = []

        with state_lock:
            for path, file_info in self.file_state.copy().items():
                if file_info.inode == task.deleted_inode:
                    file = Path(path)
                    if self.__is_excluded(file):
                        logger.debug(f"文件 {file} 在不删除目录中，跳过")
                        continue

                    # 删除硬链接文件
                    logger.info(f"延迟删除硬链接文件：{path}")
                    file.unlink()
                    deleted_files.append(path)

                    # 清理硬链接文件相关的刮削文件
                    self.delete_scrap_infos(file)
                    if self._delete_torrents:
                        # 只有非刮削文件才发送 DownloadFileDeleted 事件
                        if file.suffix.lower() not in self.SCRAP_EXTENSIONS:
                            eventmanager.send_event(
                                EventType.DownloadFileDeleted, {"src": str(file)}
                            )
                    # 删除硬链接文件的转移记录
                    self.delete_history(str(file))

                    # 从状态集合中移除
                    self.file_state.pop(path, None)

        if deleted_files:
            result.success = True
            result.hardlink_count = len(deleted_files)

        return result

    def _process_deletion_queue(self):
        """
        处理延迟删除队列
        """
        try:
            current_time = datetime.now()
            tasks_to_process = []

            # 滑动窗口模式：处理队列中所有未处理的任务
            with deletion_queue_lock:
                tasks_to_process = [task for task in self.deletion_queue if not task.processed]
                if tasks_to_process:
                    logger.info(f"处理延迟删除队列，待处理任务数: {len(tasks_to_process)}")

            # 在锁外处理任务，避免死锁
            results: List[DeletionResult] = []
            for task in tasks_to_process:
                try:
                    result = self._execute_delayed_deletion(task)
                    if result:
                        results.append(result)
                except Exception as e:
                    logger.error(f"处理延迟删除任务失败：{task.file_path} - {e}")

            # 发送批量通知
            if results and self._notify:
                self._send_batch_notification(results)

            # 清理已处理的任务
            with deletion_queue_lock:
                self.deletion_queue = [
                    task for task in self.deletion_queue if not task.processed
                ]
                self._deletion_timer = None
                logger.debug("延迟删除队列处理完成")

        except Exception as e:
            logger.error(f"处理延迟删除队列失败：{str(e)} - {traceback.format_exc()}")
            # 确保定时器状态正确
            with deletion_queue_lock:
                self._deletion_timer = None

    def _start_deletion_timer(self, delay_time: float = None):
        """
        启动延迟删除定时器
        注意：此方法假设调用前已检查没有运行中的定时器
        """
        if delay_time is None:
            delay_time = self._delay_seconds

        self._deletion_timer = threading.Timer(delay_time, self._process_deletion_queue)
        self._deletion_timer.daemon = True
        self._deletion_timer.start()

    def _send_batch_notification(self, results: List[DeletionResult]):
        """
        发送批量删除通知
        """
        if not results:
            return

        # 分类统计
        strm_results = [r for r in results if r.task_type == "strm" and r.success]
        strm_folder_results = [r for r in results if r.task_type == "strm_folder" and r.success]
        hardlink_results = [r for r in results if r.task_type == "hardlink" and r.success]

        total_scrap = sum(r.scrap_deleted for r in results)
        total_dirs = sum(r.dirs_deleted for r in results)
        total_hardlinks = sum(r.hardlink_count for r in results)
        history_deleted = any(r.history_deleted for r in results)

        # 构建通知内容
        parts = []

        # STRM 文件夹列表
        if strm_folder_results:
            parts.append(f"📁 STRM 文件夹: {len(strm_folder_results)} 个")
            for r in strm_folder_results[:5]:
                parts.append(f"  └─ {r.file_path.name} → [{r.storage_type}]")
            if len(strm_folder_results) > 5:
                parts.append(f"  └─ ... 等 {len(strm_folder_results) - 5} 个")

        # STRM 文件列表
        if strm_results:
            parts.append(f"📺 STRM 文件: {len(strm_results)} 个")
            for r in strm_results[:5]:  # 最多显示5个
                parts.append(f"  └─ {r.file_path.name} → [{r.storage_type}]")
            if len(strm_results) > 5:
                parts.append(f"  └─ ... 等 {len(strm_results) - 5} 个")

        # 硬链接文件列表
        if hardlink_results:
            parts.append(f"🔗 硬链接文件: {len(hardlink_results)} 个 (共 {total_hardlinks} 个链接)")
            for r in hardlink_results[:5]:
                parts.append(f"  └─ {r.file_path.name}")
            if len(hardlink_results) > 5:
                parts.append(f"  └─ ... 等 {len(hardlink_results) - 5} 个")

        # 汇总信息
        summary = []
        if self._delete_scrap_infos:
            if total_scrap > 0 or total_dirs > 0:
                msg = f"🖼️ 清理刮削文件 {total_scrap} 个"
                if total_dirs > 0:
                    msg += f"，空目录 {total_dirs} 个"
                    # 检查是否使用了 AList API
                    if (
                        self._api_delete_empty_dirs
                        and self._api_delete_url
                        and self._api_delete_token
                        and any(r.storage_type == "alist" for r in strm_results)
                    ):
                        msg += " (使用 AList API)"
                summary.append(msg)
            else:
                summary.append("🖼️ 无刮削文件需要清理")

        if self._delete_history:
            if history_deleted:
                summary.append("📝 已清理转移记录")
        if self._delete_torrents and hardlink_results:
            summary.append("🌱 已联动删除种子")

        if summary:
            parts.append("")
            parts.extend(summary)

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=f"🧹 媒体文件清理 - 批量处理 {len(results)} 个",
            text="⏰ 延迟删除完成\n\n" + "\n".join(parts),
        )

    def handle_deleted(self, file_path: Path):
        """
        处理删除事件
        """
        logger.debug(f"处理删除事件: {file_path}")

        # 删除的文件对应的监控信息
        with state_lock:
            # 删除的文件信息
            file_info = self.file_state.get(str(file_path))
            if not file_info:
                logger.debug(f"文件 {file_path} 未在监控列表中，跳过处理")
                return
            else:
                deleted_inode = file_info.inode
                self.file_state.pop(str(file_path))

            # 根据配置选择立即删除或延迟删除
            if self._delayed_deletion:
                # 延迟删除模式 - 所有删除操作都延迟执行
                logger.info(
                    f"文件 {file_path.name} 加入延迟删除队列，延迟 {self._delay_seconds} 秒"
                )
                task = DeletionTask(
                    file_path=file_path,
                    timestamp=datetime.now(),
                    task_type="hardlink",
                    deleted_inode=deleted_inode
                )

                with deletion_queue_lock:
                    self.deletion_queue.append(task)
                    # 滑动窗口延迟：每次有新任务时重置定时器，合并连续删除
                    if self._deletion_timer:
                        self._deletion_timer.cancel()
                    self._start_deletion_timer()
                    logger.debug(f"延迟删除定时器已重置，当前队列 {len(self.deletion_queue)} 个任务")
            else:
                # 立即删除模式（原有逻辑）
                deleted_files = []

                # 清理刮削文件
                self.delete_scrap_infos(file_path)
                if self._delete_torrents:
                    # 只有非刮削文件才发送 DownloadFileDeleted 事件
                    if file_path.suffix.lower() not in self.SCRAP_EXTENSIONS:
                        eventmanager.send_event(
                            EventType.DownloadFileDeleted, {"src": str(file_path)}
                        )
                # 删除转移记录
                self.delete_history(str(file_path))

                try:
                    # 在file_state中查找与deleted_inode有相同inode的文件并删除
                    for path, file_info in self.file_state.copy().items():
                        if file_info.inode == deleted_inode:
                            file = Path(path)
                            if self.__is_excluded(file):
                                logger.debug(f"文件 {file} 在不删除目录中，跳过")
                                continue
                            # 删除硬链接文件
                            logger.info(f"立即删除硬链接文件：{path}")
                            file.unlink()
                            deleted_files.append(path)

                            # 清理刮削文件
                            self.delete_scrap_infos(file)
                            if self._delete_torrents:
                                # 只有非刮削文件才发送 DownloadFileDeleted 事件
                                if file.suffix.lower() not in self.SCRAP_EXTENSIONS:
                                    eventmanager.send_event(
                                        EventType.DownloadFileDeleted,
                                        {"src": str(file)},
                                    )
                            # 删除转移记录
                            self.delete_history(str(file))

                    # 发送通知
                    if self._notify and deleted_files:
                        file_count = len(deleted_files)

                        # 构建通知内容
                        notification_parts = [f"🗂️ 源文件：{file_path}"]

                        if file_count == 1:
                            notification_parts.append(f"🔗 硬链接：{deleted_files[0]}")
                        else:
                            notification_parts.append(
                                f"🔗 删除了 {file_count} 个硬链接文件"
                            )

                        # 添加其他操作记录
                        if self._delete_history:
                            notification_parts.append("📝 已清理转移记录")
                        if self._delete_torrents:
                            notification_parts.append("🌱 已联动删除种子")
                        if self._delete_scrap_infos:
                            notification_parts.append("🖼️ 已清理刮削文件")

                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="🧹 媒体文件清理",
                            text=f"⚡ 立即删除完成\n\n" + "\n".join(notification_parts),
                        )

                except Exception as e:
                    logger.error(
                        "删除硬链接文件发生错误：%s - %s"
                        % (str(e), traceback.format_exc())
                    )

    def _parse_strm_path_mappings(self) -> Dict[str, Tuple[str, str, Optional[str], Optional[str]]]:
        """
        解析 strm 路径映射配置
        返回格式: {strm_path: (storage_type, storage_path, local_storage_type, local_storage_path)}
        """
        mappings = {}
        if not self.strm_path_mappings:
            return mappings

        for line in self.strm_path_mappings.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            try:
                # 支持格式: 
                # strm_path:storage_path (默认local)
                # strm_path:storage_type:storage_path
                # strm_path:storage_type:storage_path:local_storage_type:local_storage_path
                parts = line.split(":", 4)
                local_storage_type = None
                local_storage_path = None

                if len(parts) == 2:
                    # 默认使用 local 存储
                    strm_path, storage_path = parts
                    storage_type = "local"
                elif len(parts) == 3:
                    # 指定存储类型
                    strm_path, storage_type, storage_path = parts
                elif len(parts) == 5:
                    # 指定存储类型 和 本地映射
                    strm_path, storage_type, storage_path, local_storage_type, local_storage_path = parts
                    local_storage_type = local_storage_type.strip()
                    local_storage_path = local_storage_path.strip()
                else:
                    logger.warning(f"无效的 strm 路径映射配置 (部件数量错误): {line}")
                    continue

                mappings[strm_path.strip()] = (
                    storage_type.strip(),
                    storage_path.strip(),
                    local_storage_type,
                    local_storage_path
                )
            except ValueError:
                logger.warning(f"无效的 strm 路径映射配置: {line}")

        return mappings

    def _get_storage_path_from_strm(
        self, strm_file_path: Path
    ) -> Tuple[str, str, Optional[str], Optional[str]]:
        """
        根据 strm 文件路径获取对应的网盘存储路径和本地存储路径
        返回 (storage_type, storage_path, local_storage_type, local_storage_path) 或 (None, None, None, None)
        """
        mappings = self._parse_strm_path_mappings()
        strm_path_str = str(strm_file_path)

        for strm_prefix, (
            storage_type,
            storage_prefix,
            local_storage_type,
            local_storage_prefix,
        ) in mappings.items():
            if strm_path_str.startswith(strm_prefix):
                # 计算相对路径
                relative_path_strm = strm_path_str[len(strm_prefix) :].lstrip("/")
                
                # 去掉 .strm 后缀
                relative_path_no_ext = relative_path_strm
                if relative_path_no_ext.lower().endswith(".strm"):
                    relative_path_no_ext = relative_path_no_ext[:-5]

                # 构建网盘路径
                storage_file_path = storage_prefix.rstrip("/") + "/" + relative_path_no_ext

                # 构建本地路径
                local_file_path = None
                if local_storage_type and local_storage_prefix:
                    local_file_path = (
                        local_storage_prefix.rstrip("/") + "/" + relative_path_no_ext
                    )

                return storage_type, storage_file_path, local_storage_type, local_file_path

        return None, None, None, None

    def _find_storage_media_file(
        self, storage_type: str, base_path: str
    ) -> schemas.FileItem:
        """
        在网盘中查找以指定路径为前缀的视频文件
        """
        from app.core.config import settings

        # 获取父目录
        parent_path = str(Path(base_path).parent)
        parent_item = schemas.FileItem(
            storage=storage_type,
            path=parent_path if parent_path.endswith("/") else parent_path + "/",
            type="dir",
        )

        # 检查父目录是否存在
        if not self._storagechain.exists(parent_item):
            logger.debug(f"父目录不存在: [{storage_type}] {parent_path}")
            return None

        # 列出父目录中的文件
        files = self._storagechain.list_files(parent_item, recursion=False)
        if not files:
            logger.debug(f"父目录为空: [{storage_type}] {parent_path}")
            return None

        # 查找以 base_path 为前缀的视频文件
        base_name = Path(base_path).name
        for file_item in files:
            if file_item.type == "file" and file_item.name.startswith(base_name):
                # 检查是否为视频文件
                if (
                    file_item.extension
                    and f".{file_item.extension.lower()}" in settings.RMT_MEDIAEXT
                ):
                    logger.info(
                        f"找到匹配的视频文件: [{storage_type}] {file_item.path}"
                    )
                    return file_item

        logger.debug(f"未找到匹配的视频文件: [{storage_type}] {base_path}")
        return None

    def _delete_storage_scrap_files(
        self, storage_type: str, storage_file_item: schemas.FileItem
    ) -> int:
        """
        删除网盘中的刮削文件
        返回删除的文件数量
        """
        if not self._delete_scrap_infos:
            return 0

        deleted_count = 0
        try:
            # 获取父目录
            parent_path = str(Path(storage_file_item.path).parent)
            parent_item = schemas.FileItem(
                storage=storage_type,
                path=parent_path if parent_path.endswith("/") else parent_path + "/",
                type="dir",
            )

            # 检查父目录是否存在
            if not self._storagechain.exists(parent_item):
                logger.debug(f"网盘父目录不存在: [{storage_type}] {parent_path}")
                return 0

            # 列出父目录中的文件
            files = self._storagechain.list_files(parent_item, recursion=False)
            if not files:
                logger.debug(f"网盘父目录为空: [{storage_type}] {parent_path}")
                return 0

            # 获取视频文件的基础名称（不含扩展名）
            base_name = Path(storage_file_item.path).stem

            # 查找并删除刮削文件
            for file_item in files:
                if file_item.type == "file":
                    file_stem = Path(file_item.name).stem
                    file_ext = Path(file_item.name).suffix.lower()

                    # 检查是否为相关的刮削文件
                    if (
                        file_stem.startswith(base_name)
                        and file_ext in self.SCRAP_EXTENSIONS
                    ) or (
                        file_item.name.lower()
                        in [
                            "poster.jpg",
                            "backdrop.jpg",
                            "fanart.jpg",
                            "banner.jpg",
                            "logo.png",
                        ]
                    ):

                        # 删除刮削文件
                        if self._storagechain.delete_file(file_item):
                            logger.info(
                                f"删除网盘刮削文件: [{storage_type}] {file_item.path}"
                            )
                            deleted_count += 1
                        else:
                            logger.warning(
                                f"删除网盘刮削文件失败: [{storage_type}] {file_item.path}"
                            )

            logger.info(
                f"网盘刮削文件清理完成: [{storage_type}] {parent_path}，删除了 {deleted_count} 个文件"
            )

        except Exception as e:
            logger.error(
                f"清理网盘刮削文件失败: [{storage_type}] {storage_file_item.path} - {str(e)}"
            )

        return deleted_count

    def _call_api_delete_dir(self, dir_path: str) -> bool:
        """
        使用 AList API 删除空目录
        """
        try:
            p = Path(dir_path)
            parent_dir = str(p.parent)
            dir_name = p.name

            payload = {
                "dir": parent_dir,
                "names": [dir_name]
            }
            data = json.dumps(payload).encode("utf-8")

            # 构建 API URL
            # self._api_delete_url should be like http://127.0.0.1:5244
            api_url = f"{self._api_delete_url.rstrip('/')}/api/fs/remove"

            headers = {
                "Content-Type": "application/json",
                "Authorization": self._api_delete_token,
                "User-Agent": "MoviePilot-RemoveLink-Plugin",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            logger.debug(f"Calling API to delete directory: {api_url} with payload: {payload}")

            with urllib.request.urlopen(req, timeout=10) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    try:
                        # AList API success response
                        # {"code":200,"message":"success","data":null}
                        response_data = json.loads(response_body)
                        if response_data.get("code") == 200:
                            logger.info(f"API successfuly deleted directory: {dir_path}")
                            return True
                        else:
                            logger.warning(f"API reported failure for {dir_path}: {response_data.get('message')}")
                            return False
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode API response: {response_body}")
                        return False
                else:
                    logger.warning(f"API returned non-200 status code {response_code} for {dir_path}: {response_body}")
                    return False

        except urllib.error.URLError as e:
            logger.error(f"API call to delete {dir_path} failed (URLError): {e}")
            return False
        except Exception as e:
            logger.error(f"Error calling API to delete {dir_path}: {e} - {traceback.format_exc()}")
            return False

    def _get_mapped_local_details_from_storage_path(
        self, storage_type: str, storage_path: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        根据云盘路径查找对应的本地(alistlocal)存储详情
        返回 (local_storage_type, local_storage_path) 或 (None, None)
        """
        mappings = self._parse_strm_path_mappings()

        # 遍历所有映射配置
        for strm_prefix, (
            mapped_storage_type,
            mapped_storage_prefix,
            local_storage_type,
            local_storage_prefix,
        ) in mappings.items():
            # 检查云盘存储类型和路径前缀是否匹配
            if (
                mapped_storage_type == storage_type and
                storage_path.startswith(mapped_storage_prefix)
            ):
                # 计算相对路径
                relative_path = storage_path[len(mapped_storage_prefix):].lstrip("/")

                # 如果配置了本地映射且类型为 alistlocal，则构建本地路径
                if local_storage_type and local_storage_prefix and local_storage_type == "alistlocal":
                    local_path = local_storage_prefix.rstrip("/") + "/" + relative_path
                    return local_storage_type, local_path

        return None, None

    def _delete_storage_empty_folders(
        self, storage_type: str, storage_file_item: schemas.FileItem
    ) -> int:
        """
        删除网盘中的空目录
        返回删除的目录数量
        """
        deleted_count = 0
        try:
            # 获取父目录
            parent_path = str(Path(storage_file_item.path).parent)
            current_path = parent_path

            # 检查是否使用 API 删除
            use_api_delete = (
                self._api_delete_empty_dirs
                and self._api_delete_url
                and self._api_delete_token
                and storage_type == "alist"
            )

            # 逐级向上检查并删除空目录
            while current_path and current_path != "/" and current_path != "\\":
                # 获取当前目录的正确 FileItem（包含 fileid）
                current_item = self._get_storage_dir_item(storage_type, current_path)
                if not current_item:
                    logger.debug(f"网盘目录不存在: [{storage_type}] {current_path}")
                    break

                # 列出目录中的文件
                files = self._storagechain.list_files(current_item, recursion=False)

                if not files:
                    # 目录为空，删除它
                    deleted_successfully = False
                    if use_api_delete:
                        deleted_successfully = self._call_api_delete_dir(current_path)
                    else:
                        deleted_successfully = self._storagechain.delete_file(current_item)

                    if deleted_successfully:
                        logger.info(f"删除网盘空目录: [{storage_type}] {current_path}")
                        deleted_count += 1

                        # --- 新增逻辑开始 ---
                        # 同步删除对应的本地目录
                        local_storage_type, local_storage_path = self._get_mapped_local_details_from_storage_path(
                            storage_type, current_path
                        )
                        if local_storage_type and local_storage_path:
                            # 如果本地存储类型是 alistlocal，使用 AList API 删除
                            if local_storage_type == "alistlocal":
                                # 直接调用 AList API 删除本地目录
                                if self._call_api_delete_dir(local_storage_path):
                                    logger.info(f"同步删除本地目录 (通过 AList API): [{local_storage_type}] {local_storage_path}")
                                else:
                                    logger.warning(f"同步删除本地目录失败 (通过 AList API): [{local_storage_type}] {local_storage_path}")
                            else:
                                # 对于其他本地存储类型，使用 StorageChain
                                local_dir_item = schemas.FileItem(
                                    storage="local",
                                    path=local_storage_path if local_storage_path.endswith("/") else local_storage_path + "/",
                                    type="dir"
                                )
                                # 直接删除本地目录，无论是否存在文件
                                if self._storagechain.delete_file(local_dir_item):
                                    logger.info(f"同步删除本地目录: [{local_storage_type}] {local_storage_path}")
                                else:
                                    logger.warning(f"同步删除本地目录失败: [{local_storage_type}] {local_storage_path}")
                        # --- 新增逻辑结束 ---

                        # 继续检查上级目录
                        current_path = str(Path(current_path).parent)
                        if current_path == current_path.replace(
                            str(Path(current_path).name), ""
                        ).rstrip("/\\"):
                            # 已到达根目录
                            break
                    else:
                        logger.warning(
                            f"删除网盘空目录失败: [{storage_type}] {current_path}"
                        )
                        break
                else:
                    # 目录不为空，检查是否只包含刮削文件
                    only_scrap_files = True
                    for file_item in files:
                        if file_item.type == "file":
                            file_ext = Path(file_item.name).suffix.lower()
                            if file_ext not in self.SCRAP_EXTENSIONS:
                                only_scrap_files = False
                                break
                        else:
                            # 包含子目录，不删除
                            only_scrap_files = False
                            break

                    if only_scrap_files and files:
                        # 目录只包含刮削文件，删除所有文件
                        for file_item in files:
                            if file_item.type == "file":
                                if self._storagechain.delete_file(file_item):
                                    logger.info(
                                        f"删除网盘刮削文件: [{storage_type}] {file_item.path}"
                                    )
                                else:
                                    logger.warning(
                                        f"删除网盘刮削文件失败: [{storage_type}] {file_item.path}"
                                    )

                        # 重新获取目录信息并检查是否为空
                        current_item = self._get_storage_dir_item(
                            storage_type, current_path
                        )
                        if current_item:
                            files = self._storagechain.list_files(
                                current_item, recursion=False
                            )
                            if not files:
                                # 现在目录为空，删除它
                                deleted_successfully = False
                                if use_api_delete:
                                    deleted_successfully = self._call_api_delete_dir(current_path)
                                else:
                                    deleted_successfully = self._storagechain.delete_file(current_item)

                                if deleted_successfully:
                                    logger.info(
                                        f"删除网盘空目录: [{storage_type}] {current_path}"
                                    )
                                    deleted_count += 1

                                    # --- 新增逻辑开始 ---
                                    # 同步删除对应的本地目录
                                    local_storage_type, local_storage_path = self._get_mapped_local_details_from_storage_path(
                                        storage_type, current_path
                                    )
                                    if local_storage_type and local_storage_path:
                                        # 如果本地存储类型是 alistlocal，使用 AList API 删除
                                        if local_storage_type == "alistlocal":
                                            # 直接调用 AList API 删除本地目录
                                            if self._call_api_delete_dir(local_storage_path):
                                                logger.info(f"同步删除本地目录 (通过 AList API): [{local_storage_type}] {local_storage_path}")
                                            else:
                                                logger.warning(f"同步删除本地目录失败 (通过 AList API): [{local_storage_type}] {local_storage_path}")
                                        else:
                                            # 对于其他本地存储类型，使用 StorageChain
                                            local_dir_item = schemas.FileItem(
                                                storage="local",
                                                path=local_storage_path if local_storage_path.endswith("/") else local_storage_path + "/",
                                                type="dir"
                                            )
                                            # 直接删除本地目录，无论是否存在文件
                                            if self._storagechain.delete_file(local_dir_item):
                                                logger.info(f"同步删除本地目录: [{local_storage_type}] {local_storage_path}")
                                            else:
                                                logger.warning(f"同步删除本地目录失败: [{local_storage_type}] {local_storage_path}")
                                    # --- 新增逻辑结束 ---

                                    # 继续检查上级目录
                                    current_path = str(Path(current_path).parent)
                                    if current_path == current_path.replace(
                                        str(Path(current_path).name), ""
                                    ).rstrip("/\\"):
                                        break
                                else:
                                    break
                            else:
                                break
                        else:
                            break
                    else:
                        # 目录包含非刮削文件或子目录，停止向上检查
                        break

            if deleted_count > 0:
                logger.info(
                    f"网盘空目录清理完成: [{storage_type}] 删除了 {deleted_count} 个目录"
                )

        except Exception as e:
            logger.error(
                f"清理网盘空目录失败: [{storage_type}] {storage_file_item.path} - {str(e)}"
            )

        return deleted_count

    def _get_storage_dir_item(
        self, storage_type: str, dir_path: str
    ) -> schemas.FileItem:
        """
        获取网盘目录的正确 FileItem（包含 fileid）
        """
        try:
            # 获取父目录
            parent_path = str(Path(dir_path).parent)
            if parent_path == dir_path:
                # 已经是根目录
                return None

            parent_item = schemas.FileItem(
                storage=storage_type,
                path=parent_path if parent_path.endswith("/") else parent_path + "/",
                type="dir",
            )

            # 检查父目录是否存在
            if not self._storagechain.exists(parent_item):
                return None

            # 列出父目录中的文件，查找目标目录
            files = self._storagechain.list_files(parent_item, recursion=False)
            if not files:
                return None

            # 查找目标目录
            target_name = Path(dir_path).name
            for file_item in files:
                if file_item.type == "dir" and file_item.name == target_name:
                    return file_item

            return None

        except Exception as e:
            logger.debug(
                f"获取网盘目录信息失败: [{storage_type}] {dir_path} - {str(e)}"
            )
            return None

    def _execute_strm_deletion(self, strm_file_path: Path, send_notify: bool = True) -> Optional[DeletionResult]:
        """
        执行 strm 文件的实际删除逻辑（用于立即删除或延迟删除）
        返回 DeletionResult 对象，如果 send_notify=True 则同时发送通知
        """
        result = DeletionResult(
            file_path=strm_file_path,
            task_type="strm",
            success=False
        )
        try:
            # 获取对应的网盘文件路径和本地文件路径
            (
                storage_type,
                storage_path,
                local_storage_type,
                local_storage_path,
            ) = self._get_storage_path_from_strm(strm_file_path)

            if not storage_type or not storage_path:
                logger.warning(
                    f"无法找到 strm 文件 {strm_file_path} 对应的网盘路径映射"
                )
                return result

            # 查找网盘中的视频文件
            storage_file_item = self._find_storage_media_file(
                storage_type, storage_path
            )

            if not storage_file_item:
                logger.info(
                    f"网盘中未找到对应的视频文件: [{storage_type}] {storage_path}"
                )
                return result

            logger.info(f"准备删除网盘文件: [{storage_type}] {storage_file_item.path}")

            # 删除网盘文件
            if self._storagechain.delete_file(storage_file_item):
                logger.info(
                    f"成功删除网盘文件: [{storage_type}] {storage_file_item.path}"
                )
                result.success = True
                result.storage_type = storage_type
                result.storage_path = storage_file_item.path

                # 清理网盘上的刮削文件
                if self._delete_scrap_infos:
                    result.scrap_deleted = self._delete_storage_scrap_files(
                        storage_type, storage_file_item
                    )
                    # 清理网盘空目录
                    result.dirs_deleted = self._delete_storage_empty_folders(
                        storage_type, storage_file_item
                    )

                # 删除转移记录（通过网盘文件路径查询）
                if self._delete_history:
                    result.history_deleted = self.delete_history_by_dest(
                        storage_file_item.path
                    )

                # 发送通知（仅立即删除模式）
                if send_notify and self._notify:
                    self._send_single_strm_notification(result)
            else:
                logger.error(
                    f"删除网盘文件失败: [{storage_type}] {storage_file_item.path}"
                )

        except Exception as e:
            logger.error(
                f"处理 strm 文件删除失败: {strm_file_path} - {str(e)} - {traceback.format_exc()}"
            )

        return result

    def _send_single_strm_notification(self, result: DeletionResult):
        """发送单个 STRM 删除通知（用于立即删除模式）"""
        notification_parts = [f"🗂️ STRM 文件：{result.file_path}"]
        notification_parts.append(
            f"🗑️ 已删除网盘文件：[{result.storage_type}] {result.storage_path}"
        )

        if self._delete_history:
            if result.history_deleted:
                notification_parts.append("📝 已清理转移记录")
            else:
                notification_parts.append("📝 无转移记录")
        if self._delete_scrap_infos:
            if result.scrap_deleted > 0:
                scrap_msg = f"🖼️ 已清理网盘刮削文件（{result.scrap_deleted} 个）"
            else:
                scrap_msg = "🖼️ 无刮削文件需要清理"

            if result.dirs_deleted > 0:
                scrap_msg += f"，清理空目录 {result.dirs_deleted} 个"

            if (
                self._api_delete_empty_dirs
                and self._api_delete_url
                and self._api_delete_token
                and result.storage_type == "alist"
                and result.dirs_deleted > 0
            ):
                scrap_msg += " (使用 AList API)"

            notification_parts.append(scrap_msg)

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="🧹 媒体文件清理",
            text="⚡ 立即删除完成 (STRM)\n\n" + "\n".join(notification_parts),
        )

    def handle_strm_deleted(self, strm_file_path: Path):
        """
        处理 strm 文件删除事件
        """
        logger.info(f"处理 strm 文件删除: {strm_file_path}")

        # 检查文件是否属于已删除的文件夹（如果是则跳过，由文件夹删除统一处理）
        for deleted_folder in self.deleted_strm_folders.copy():
            if str(strm_file_path).startswith(str(deleted_folder) + os.sep):
                logger.debug(f"文件 {strm_file_path} 属于已删除文件夹 {deleted_folder}，跳过")
                return

        # 根据配置选择立即删除或延迟删除
        if self._delayed_deletion:
            # 延迟删除模式
            logger.info(
                f"STRM 文件 {strm_file_path.name} 加入延迟删除队列，延迟 {self._delay_seconds} 秒"
            )
            task = DeletionTask(
                file_path=strm_file_path,
                timestamp=datetime.now(),
                task_type="strm"
                # deleted_inode is not needed
            )

            with deletion_queue_lock:
                self.deletion_queue.append(task)
                # 滑动窗口延迟：每次有新任务时重置定时器，合并连续删除
                if self._deletion_timer:
                    self._deletion_timer.cancel()
                self._start_deletion_timer()
                logger.debug(f"延迟删除定时器已重置，当前队列 {len(self.deletion_queue)} 个任务")
        else:
            # 立即删除模式
            logger.debug(f"STRM 文件 {strm_file_path.name} 立即删除")
            self._execute_strm_deletion(strm_file_path)

    def handle_strm_folder_deleted(self, folder_path: Path):
        """
        处理 strm 文件夹删除事件
        """
        logger.info(f"处理 strm 文件夹删除: {folder_path}")

        # 记录已删除的文件夹路径
        self.deleted_strm_folders.add(str(folder_path))

        # 根据配置选择立即删除或延迟删除
        if self._delayed_deletion:
            logger.info(
                f"STRM 文件夹 {folder_path.name} 加入延迟删除队列，延迟 {self._delay_seconds} 秒"
            )
            task = DeletionTask(
                file_path=folder_path,
                timestamp=datetime.now(),
                task_type="strm_folder"
            )

            with deletion_queue_lock:
                self.deletion_queue.append(task)
                if self._deletion_timer:
                    self._deletion_timer.cancel()
                self._start_deletion_timer()
                logger.debug(f"延迟删除定时器已重置，当前队列 {len(self.deletion_queue)} 个任务")
        else:
            # 立即删除模式
            logger.debug(f"STRM 文件夹 {folder_path.name} 立即删除")
            self._execute_strm_folder_deletion_immediate(folder_path)

    def _get_storage_folder_path_from_strm(
        self, strm_folder_path: Path
    ) -> Tuple[str, str, Optional[str], Optional[str]]:
        """
        根据 strm 文件夹路径获取对应的网盘存储文件夹路径
        返回 (storage_type, storage_path, local_storage_type, local_storage_path) 或 (None, None, None, None)
        """
        mappings = self._parse_strm_path_mappings()
        strm_path_str = str(strm_folder_path)

        for strm_prefix, (
            storage_type,
            storage_prefix,
            local_storage_type,
            local_storage_prefix,
        ) in mappings.items():
            if strm_path_str.startswith(strm_prefix):
                # 计算相对路径
                relative_path = strm_path_str[len(strm_prefix):].lstrip("/\\")

                # 构建网盘路径
                storage_folder_path = storage_prefix.rstrip("/") + "/" + relative_path if relative_path else storage_prefix

                # 构建本地路径
                local_folder_path = None
                if local_storage_type and local_storage_prefix:
                    local_folder_path = (
                        local_storage_prefix.rstrip("/") + "/" + relative_path if relative_path else local_storage_prefix
                    )

                return storage_type, storage_folder_path, local_storage_type, local_folder_path

        return None, None, None, None

    def _execute_strm_folder_deletion_immediate(self, folder_path: Path) -> Optional[DeletionResult]:
        """
        立即执行 STRM 文件夹删除
        """
        return self._execute_strm_folder_deletion(folder_path, send_notify=True)

    def _execute_strm_folder_deletion(self, folder_path: Path, send_notify: bool = True) -> Optional[DeletionResult]:
        """
        执行 STRM 文件夹删除逻辑
        """
        result = DeletionResult(
            file_path=folder_path,
            task_type="strm_folder",
            success=False
        )

        try:
            # 获取对应的网盘文件夹路径
            (
                storage_type,
                storage_path,
                local_storage_type,
                local_storage_path,
            ) = self._get_storage_folder_path_from_strm(folder_path)

            if not storage_type or not storage_path:
                logger.warning(f"无法找到 strm 文件夹 {folder_path} 对应的网盘路径映射")
                return result

            logger.info(f"准备删除网盘文件夹: [{storage_type}] {storage_path}")

            # 使用 AList API 删除整个文件夹
            deleted = False
            if storage_type == "alist" and self._api_delete_url and self._api_delete_token:
                deleted = self._call_api_delete_dir(storage_path)
            else:
                # 使用 StorageChain 删除
                folder_item = schemas.FileItem(
                    storage=storage_type,
                    path=storage_path if storage_path.endswith("/") else storage_path + "/",
                    type="dir"
                )
                deleted = self._storagechain.delete_file(folder_item)

            if deleted:
                logger.info(f"成功删除网盘文件夹: [{storage_type}] {storage_path}")
                result.success = True
                result.storage_type = storage_type
                result.storage_path = storage_path
                result.dirs_deleted = 1

                # 删除本地对应文件夹
                if local_storage_type and local_storage_path:
                    if local_storage_type == "alistlocal" and self._api_delete_url and self._api_delete_token:
                        if self._call_api_delete_dir(local_storage_path):
                            logger.info(f"成功删除本地文件夹 (AList API): {local_storage_path}")
                        else:
                            logger.warning(f"删除本地文件夹失败 (AList API): {local_storage_path}")
                    else:
                        local_item = schemas.FileItem(
                            storage="local",
                            path=local_storage_path if local_storage_path.endswith("/") else local_storage_path + "/",
                            type="dir"
                        )
                        if self._storagechain.delete_file(local_item):
                            logger.info(f"成功删除本地文件夹: {local_storage_path}")
                        else:
                            logger.warning(f"删除本地文件夹失败: {local_storage_path}")

                # 发送通知
                if send_notify and self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="🧹 媒体文件清理",
                        text=f"📁 文件夹删除完成\n\n🗂️ STRM 文件夹：{folder_path}\n🗑️ 已删除网盘文件夹：[{storage_type}] {storage_path}",
                    )
            else:
                logger.error(f"删除网盘文件夹失败: [{storage_type}] {storage_path}")

        except Exception as e:
            logger.error(f"处理 strm 文件夹删除失败: {folder_path} - {str(e)} - {traceback.format_exc()}")

        return result

    def delete_history_by_dest(self, dest_path: str) -> bool:
        """
        通过目标路径删除转移记录
        返回是否成功删除了转移记录
        """
        if not self._delete_history:
            return False
        # 查找转移记录
        transfer_history = self._transferhistory.get_by_dest(dest_path)
        if transfer_history:
            # 删除转移记录
            self._transferhistory.delete(transfer_history.id)
            logger.info(f"删除转移记录：{transfer_history.id} - {dest_path}")
            return True
        else:
            logger.debug(f"未找到转移记录：{dest_path}")
            return False
