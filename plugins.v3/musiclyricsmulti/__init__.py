"""
多源歌词插件（MoviePilot v3）。

聚合网易云、QQ 音乐、酷狗三个歌词源：
- ``music_lyrics`` 返回最可信的一条，用于短路内置 LRCLIB；
- ``music_lyrics_candidates`` 每个源返回一条候选，交给宿主 LyricsChain 择优。

同步实现（宿主插件接口是同步的）。

接管歌词刮削（1.1.0）：
- 插件启用时把宿主系统配置 ``ScrapingSwitchs.music_lyrics`` 改为 ``skip``，
  关闭宿主自己的歌词刮削（原值备份到插件 data，插件停用时恢复）；
- 不再依赖宿主生成旁挂歌词：监听整理完成事件（影视 ``TransferComplete``、
  音频 ``AudioTransferComplete``，payload 结构一致），在后台延迟几秒处理
  本次整理到本地的 FLAC/OGG/MP3/M4A/WAV/AAC 音轨；
- 原文件已有内嵌歌词、或同目录已有 .lrc / .txt / .lyricsfile.yaml 时跳过；
- 否则自行从标签（缺失时用文件名）取 title/artist/album/duration，调用
  三源取词并择优；开启 ``embed_lyrics`` 的 FLAC/OGG 直接写内嵌标签
  （有时间轴时三写 LYRICS/UNSYNCEDLYRICS/SYNCEDLYRICS，否则只写 LYRICS），
  不生成旁挂；其它情况在同目录写 .lrc（有时间轴）或 .txt（仅纯文本）。
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import mutagen

from app.domain.context import MusicLyrics  # type: ignore
from app.schemas.types import EventType  # type: ignore
from app.sdk.events import Event, eventmanager  # type: ignore
from app.sdk.logging import logger  # type: ignore
from app.sdk.media import MetaMusic  # type: ignore
from app.sdk.plugin import _PluginBase  # type: ignore

from .cache import TTLCache
from .embed import (
    SUPPORTED_EXTENSIONS,
    has_embedded_lyrics,
    lrc_to_plain,
    write_lyrics_direct,
)
from .httpclient import DeadlineExceeded, HttpClient
from .lrc import TRANSLATION_MODES, build_lyrics
from .match import iter_artist_names, to_optional_int
from .models import LyricsQuery, ProviderLyrics
from .providers import DEFAULT_SOURCES, PROVIDER_CLASSES, BaseProvider

# 单首歌全部源的总预算（秒）
_TOTAL_BUDGET_SECONDS = 25.0
# 歌词缓存：容量 500，TTL 1 小时
_LYRIC_CACHE_SIZE = 500
_LYRIC_CACHE_TTL = 60 * 60
# 查询结果缓存：宿主会先后调用 music_lyrics 与 music_lyrics_candidates，
# 用短 TTL 缓存避免同一首歌重复请求三源。
_QUERY_CACHE_SIZE = 64
_QUERY_CACHE_TTL = 60
# 整理完成事件后延迟多少秒再处理歌词，避免与刮削/外部搬运竞态
_EMBED_DELAY_SECONDS = 3.0
# 插件接管歌词时写入的系统配置键与值
_SCRAPING_SETTING_KEY = "ScrapingSwitchs"
_MUSIC_LYRICS_SETTING = "music_lyrics"
_SKIP_VALUE = "skip"
# 插件 data 中保存原值的键
_BACKUP_DATA_KEY = "lyrics_scraping_backup"
# 视为音频、需要处理歌词的后缀
_AUDIO_EXTENSIONS = (".flac", ".ogg", ".mp3", ".m4a", ".wav", ".aac")
# 已有旁挂歌词时跳过的后缀
_SIDECAR_EXTENSIONS = (".lrc", ".txt", ".lyricsfile.yaml")

_VALID_SOURCES = tuple(DEFAULT_SOURCES)
_SOURCE_LABELS = {
    "netease": "网易云音乐",
    "qqmusic": "QQ 音乐",
    "kugou": "酷狗音乐",
}
_TRANSLATION_LABELS = {
    "merge": "合并为一行",
    "two_lines": "同一时间戳两行",
    "original_only": "仅原文",
}


class MusicLyricsMulti(_PluginBase):
    """
    多源歌词插件主类。

    每个源实现 ``search -> 打分 -> fetch``，插件层负责串行调度、限速、
    失败隔离、缓存与配置。
    """

    plugin_name = "多源歌词"
    plugin_desc = "聚合网易云、QQ 音乐、酷狗歌词，严格匹配并支持双语合并"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.1.0"
    plugin_author = "Lyzd1"
    author_url = "https://github.com/Lyzd1"
    plugin_config_prefix = "musiclyricsmulti_"
    plugin_order = 26
    auth_level = 1
    plugin_label = "音乐"

    def __init__(self) -> None:
        """初始化运行期状态；不在构造阶段发起任何网络请求。"""
        super().__init__()
        self._enabled = True
        self._sources: List[str] = list(DEFAULT_SOURCES)
        self._translation_mode = "merge"
        self._request_interval = 0.3
        self._timeout = 8
        self._max_candidates = 4
        self._embed_lyrics = False

        self._lock = threading.Lock()
        self._http: Optional[HttpClient] = None
        self._providers: List[BaseProvider] = []
        self._lyric_cache = TTLCache(_LYRIC_CACHE_SIZE, _LYRIC_CACHE_TTL)
        self._query_cache = TTLCache(_QUERY_CACHE_SIZE, _QUERY_CACHE_TTL)

    # ------------------------------------------------------------------ #
    # 生命周期 / 配置
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """生效配置：重建 HTTP 客户端与各源 Provider，并清空查询缓存。"""
        config = config or {}
        enabled_value = config.get("enabled", True)
        if isinstance(enabled_value, str):
            self._enabled = enabled_value.strip().lower() not in ("", "false", "0", "no")
        else:
            self._enabled = enabled_value is not False

        self._sources = self._normalize_sources(config.get("enabled_sources"))
        mode = str(config.get("translation_mode") or "merge").strip()
        self._translation_mode = mode if mode in TRANSLATION_MODES else "merge"
        self._request_interval = self._normalize_float(config.get("request_interval"), 0.3, 0.0, 10.0)
        self._timeout = self._normalize_int(config.get("timeout"), 8, 1, 60)
        self._max_candidates = self._normalize_int(config.get("max_candidates"), 4, 1, 10)
        self._embed_lyrics = self._normalize_bool(config.get("embed_lyrics"), False)

        with self._lock:
            old_http = self._http
            self._http = HttpClient(timeout=self._timeout, interval=self._request_interval)
            self._lyric_cache = TTLCache(_LYRIC_CACHE_SIZE, _LYRIC_CACHE_TTL)
            self._query_cache = TTLCache(_QUERY_CACHE_SIZE, _QUERY_CACHE_TTL)
            self._providers = [
                PROVIDER_CLASSES[name](self._http, self._lyric_cache)
                for name in DEFAULT_SOURCES
                if name in self._sources
            ]
        if old_http is not None:
            old_http.close()

        if not self._providers:
            logger.warning("【多源歌词】未启用任何歌词源，插件将返回空结果")
        else:
            logger.debug(
                "【多源歌词】配置生效：源="
                + "/".join(self._sources)
                + f"，翻译模式={self._translation_mode}，"
                f"间隔={self._request_interval:g}s，超时={self._timeout}s，"
                f"最多候选={self._max_candidates}"
            )

        # 插件启用即接管宿主歌词刮削，与 embed_lyrics 开关无关。
        # 内部已捕获全部异常，绝不会导致插件加载失败。
        if self._enabled:
            self._takeover_lyrics_scraping()

    def get_state(self) -> bool:
        """返回插件总开关状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """恢复宿主歌词刮削配置并关闭 HTTP 连接池。"""
        self._restore_lyrics_scraping()
        with self._lock:
            http = self._http
            self._http = None
            self._providers = []
        if http is not None:
            http.close()
        self._query_cache.clear()

    # ------------------------------------------------------------------ #
    # 接管宿主歌词刮削
    # ------------------------------------------------------------------ #
    def _load_scraping_switches(self) -> Optional[Dict[str, Any]]:
        """读取宿主 ``ScrapingSwitchs`` 字典。

        优先走插件基类注入的系统配置访问器（真实宿主为数据库配置），
        不可用时退回运行期 ``settings``。读取异常返回 ``None``，调用方
        必须据此中止，绝不能把读取失败当成空配置写回而丢掉其它刮削项。
        """
        store = getattr(self, "systemconfig", None)
        if store is not None:
            try:
                value = store.get(_SCRAPING_SETTING_KEY)
            except Exception as error:
                logger.warning(f"【多源歌词】读取 ScrapingSwitchs 失败：{error}")
                return None
            if isinstance(value, dict):
                return dict(value)
        try:
            from app.runtime.config import settings as runtime_settings

            value = getattr(runtime_settings, _SCRAPING_SETTING_KEY, None)
            if isinstance(value, dict):
                return dict(value)
        except Exception as error:
            logger.debug(f"【多源歌词】读取运行期 ScrapingSwitchs 失败：{error}")
        return {}

    def _store_scraping_switches(self, value: Dict[str, Any]) -> bool:
        """写回宿主 ``ScrapingSwitchs``；返回是否写入成功。"""
        store = getattr(self, "systemconfig", None)
        if store is not None and hasattr(store, "set"):
            try:
                result = store.set(_SCRAPING_SETTING_KEY, value)
            except Exception as error:
                logger.warning(f"【多源歌词】写入 ScrapingSwitchs 失败：{error}")
                return False
            return result is not False
        try:
            from app.runtime.config import settings as runtime_settings

            update_setting = getattr(runtime_settings, "update_setting", None)
            if callable(update_setting):
                result = update_setting(_SCRAPING_SETTING_KEY, value)
                if isinstance(result, tuple):
                    result = result[0]
                return result is not False
        except Exception as error:
            logger.warning(f"【多源歌词】写入 ScrapingSwitchs 失败：{error}")
        return False

    @staticmethod
    def _notify_scraping_config_changed() -> None:
        """广播配置变更，让宿主 ScrapingChain 立即重载刮削策略。"""
        try:
            from app.schemas.event import ConfigChangeEventData

            eventmanager.send_event(
                etype=EventType.ConfigChanged,
                data=ConfigChangeEventData(
                    key={_SCRAPING_SETTING_KEY},
                    value=None,
                    change_type="update",
                ),
            )
        except Exception as error:
            # 广播失败不影响已成功的写入；下次配置变更/重启仍会生效
            logger.debug(f"【多源歌词】广播配置变更失败：{error}")

    def _takeover_lyrics_scraping(self) -> None:
        """把宿主 ``music_lyrics`` 刮削策略改为 skip，并备份原值。

        幂等：当前已经是 skip 时直接返回，不重复备份。
        """
        try:
            switches = self._load_scraping_switches()
            if switches is None:
                logger.warning("【多源歌词】无法读取宿主歌词刮削配置，本次不接管")
                return
            if switches.get(_MUSIC_LYRICS_SETTING) == _SKIP_VALUE:
                logger.debug("【多源歌词】宿主歌词刮削已是 skip，无需接管")
                return

            present = _MUSIC_LYRICS_SETTING in switches
            original = switches.get(_MUSIC_LYRICS_SETTING)
            self.save_data(
                _BACKUP_DATA_KEY,
                {"value": original, "present": present, "at": int(time.time())},
            )

            updated = dict(switches)
            updated[_MUSIC_LYRICS_SETTING] = _SKIP_VALUE
            if not self._store_scraping_switches(updated):
                # 写入失败时清除备份，避免停用时误恢复
                self.del_data(_BACKUP_DATA_KEY)
                logger.warning("【多源歌词】接管歌词刮削失败：无法写入 ScrapingSwitchs")
                return

            self._notify_scraping_config_changed()
            logger.info(
                f"【多源歌词】已接管歌词刮削："
                f"ScrapingSwitchs.{_MUSIC_LYRICS_SETTING} {original!r} → {_SKIP_VALUE!r}"
            )
        except Exception as error:
            logger.warning(
                f"【多源歌词】接管歌词刮削异常（{error.__class__.__name__}）：{error}"
            )

    def _restore_lyrics_scraping(self) -> None:
        """插件停用时恢复宿主 ``music_lyrics`` 原值。

        当前值已不是 skip（用户手动改过）时只清除备份，不覆盖用户配置。
        """
        try:
            backup = self.get_data(_BACKUP_DATA_KEY)
            if not isinstance(backup, dict) or "value" not in backup:
                return

            switches = self._load_scraping_switches()
            if switches is None:
                logger.warning("【多源歌词】无法读取宿主歌词刮削配置，保留备份稍后恢复")
                return
            current = switches.get(_MUSIC_LYRICS_SETTING)
            if current != _SKIP_VALUE:
                self.del_data(_BACKUP_DATA_KEY)
                logger.debug(
                    f"【多源歌词】宿主歌词刮削已被改为 {current!r}，"
                    "不恢复原值，仅清除备份"
                )
                return

            original = backup.get("value")
            present = bool(backup.get("present", original is not None))
            updated = dict(switches)
            if present and original is not None:
                updated[_MUSIC_LYRICS_SETTING] = original
            else:
                updated.pop(_MUSIC_LYRICS_SETTING, None)

            if not self._store_scraping_switches(updated):
                # 保留备份，等待下次停用或人工处理
                logger.warning("【多源歌词】恢复歌词刮削失败：无法写入 ScrapingSwitchs")
                return

            self.del_data(_BACKUP_DATA_KEY)
            self._notify_scraping_config_changed()
            logger.info(
                f"【多源歌词】已恢复宿主歌词刮削："
                f"ScrapingSwitchs.{_MUSIC_LYRICS_SETTING} → {original!r}"
            )
        except Exception as error:
            logger.warning(
                f"【多源歌词】恢复歌词刮削异常（{error.__class__.__name__}）：{error}"
            )

    @staticmethod
    def _normalize_sources(value: Any) -> List[str]:
        """把配置中的源列表规范为受支持、去重且按默认顺序排列的列表。"""
        if value is None:
            return list(DEFAULT_SOURCES)
        if isinstance(value, str):
            raw_items = value.split(",")
        elif isinstance(value, (list, tuple, set, frozenset)):
            raw_items = list(value)
        else:
            return list(DEFAULT_SOURCES)
        selected = {str(item).strip().lower() for item in raw_items}
        return [name for name in _VALID_SOURCES if name in selected]

    @staticmethod
    def _normalize_float(value: Any, default: float, minimum: float, maximum: float) -> float:
        """把配置转成限定范围内的浮点数。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return min(max(number, minimum), maximum)

    @staticmethod
    def _normalize_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        """把配置转成限定范围内的整数。"""
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            number = default
        return min(max(number, minimum), maximum)

    @staticmethod
    def _normalize_bool(value: Any, default: bool) -> bool:
        """把配置转成布尔值，兼容前端传来的字符串。"""
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ("", "false", "0", "no", "off")
        return bool(value)

    # ------------------------------------------------------------------ #
    # 内嵌歌词（整理完成事件）
    # ------------------------------------------------------------------ #
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event) -> None:
        """影视整理完成事件（``transfer.complete``）入口，转交统一处理。"""
        self._handle_transfer_event(event)

    @eventmanager.register(EventType.AudioTransferComplete)
    def on_audio_transfer_complete(self, event: Event) -> None:
        """音频整理完成事件（``transfer.audio.complete``）入口。

        宿主 ``_durable_transfer_event()`` 对音频文件发布的是
        ``EventType.AudioTransferComplete`` 而非 ``TransferComplete``，
        payload 结构与后者一致，因此同样转交统一处理。
        """
        self._handle_transfer_event(event)

    def _handle_transfer_event(self, event: Event) -> None:
        """整理完成事件的统一处理：插件接管后自行取词并落盘。

        只做路径筛选与线程投递，真正的取词、写标签/旁挂放后台 daemon 线程，
        延迟 ``_EMBED_DELAY_SECONDS`` 秒执行，避免与刮削/外部搬运竞态。
        插件总开关关闭时不处理；任何异常都在这里或工作线程内捕获，
        绝不抛回宿主事件总线。
        """
        try:
            if not self._enabled:
                return
            event_data = getattr(event, "event_data", None) or {}
            if not isinstance(event_data, dict):
                return
            transferinfo = event_data.get("transferinfo")
            if transferinfo is None:
                return
            if self._transfer_storage(transferinfo) != "local":
                return
            paths = self._extract_local_audio_paths(transferinfo)
            if not paths:
                return
            threading.Thread(
                target=self._process_audio_paths,
                args=(paths,),
                name="musiclyricsmulti-lyrics",
                daemon=True,
            ).start()
        except Exception as error:
            logger.warning(
                f"【多源歌词】整理完成事件处理异常（{error.__class__.__name__}）：{error}"
            )

    @staticmethod
    def _event_field(obj: Any, name: str) -> Any:
        """兼容字典与对象两种事件载荷形态读取字段。"""
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    @classmethod
    def _transfer_storage(cls, transferinfo: Any) -> str:
        """读取整理目标项的存储类型；只接受显式 local。"""
        for name in ("target_item", "target_diritem"):
            item = cls._event_field(transferinfo, name)
            if item is None:
                continue
            storage = cls._event_field(item, "storage")
            if storage:
                return str(storage).strip().lower()
        return ""

    @classmethod
    def _extract_local_audio_paths(cls, transferinfo: Any) -> List[Path]:
        """从整理结果中提取本机真实存在的常见音频路径。"""
        raw_paths: List[str] = []
        file_list = cls._event_field(transferinfo, "file_list_new") or []
        if isinstance(file_list, str):
            file_list = [file_list]
        for entry in file_list:
            if isinstance(entry, dict):
                value = (
                    entry.get("path")
                    or entry.get("target_path")
                    or entry.get("filepath")
                )
            else:
                value = entry
            if value:
                raw_paths.append(str(value))
        target_item = cls._event_field(transferinfo, "target_item")
        if target_item is not None:
            value = cls._event_field(target_item, "path")
            if value:
                raw_paths.append(str(value))

        paths: List[Path] = []
        seen = set()
        for raw_path in raw_paths:
            if raw_path in seen:
                continue
            seen.add(raw_path)
            path = Path(raw_path)
            if path.suffix.casefold() not in _AUDIO_EXTENSIONS:
                continue
            if not path.is_file():
                continue
            paths.append(path)
        return paths

    def _process_audio_paths(self, paths: List[Path]) -> None:
        """后台线程：延迟后逐个自行取词并落盘，异常只记 WARNING。"""
        try:
            time.sleep(_EMBED_DELAY_SECONDS)
            for path in paths:
                try:
                    self._process_one_audio(path)
                except Exception as error:
                    logger.warning(
                        f"【多源歌词】歌词处理异常：{path}（"
                        f"{error.__class__.__name__}）：{error}"
                    )
                    continue
        except Exception as error:
            logger.warning(
                f"【多源歌词】歌词后台任务异常（{error.__class__.__name__}）：{error}"
            )

    @staticmethod
    def _existing_sidecar(path: Path) -> Optional[Path]:
        """返回该音轨已存在的旁挂歌词路径（.lrc / .txt / .lyricsfile.yaml）。"""
        for suffix in _SIDECAR_EXTENSIONS:
            sidecar = path.with_suffix(suffix)
            if sidecar.is_file():
                return sidecar
        return None

    @staticmethod
    def _tag_text(tags: Any, *keys: str) -> str:
        """按候选键读取标签文本，兼容 FLAC/Vorbis、ID3 与 MP4 帧。"""
        for key in keys:
            try:
                value = tags.get(key)
            except Exception:
                value = None
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                value = next(
                    (str(item).strip() for item in value if str(item).strip()), ""
                )
            else:
                text = getattr(value, "text", value)
                if isinstance(text, (list, tuple)):
                    text = next(
                        (str(item).strip() for item in text if str(item).strip()), ""
                    )
                value = str(text).strip()
            if value:
                return str(value)
        return ""

    @classmethod
    def _read_audio_metadata(
        cls, path: Path
    ) -> Tuple[str, str, str, Optional[int]]:
        """读取音轨的 title/artist/album/duration，标签缺失时用文件名兜底。"""
        title = artist = album = ""
        duration: Optional[int] = None
        audio = None
        for easy in (True, False):
            try:
                audio = mutagen.File(path, easy=easy)
            except Exception:
                audio = None
            if audio is not None:
                break

        if audio is not None:
            tags = getattr(audio, "tags", None)
            if tags:
                title = cls._tag_text(tags, "title", "TIT2", "\xa9nam")
                artist = cls._tag_text(tags, "artist", "TPE1", "\xa9ART")
                album = cls._tag_text(tags, "album", "TALB", "\xa9alb")
            info = getattr(audio, "info", None)
            length = getattr(info, "length", None)
            if isinstance(length, (int, float)) and length > 0:
                duration = int(round(length))

        if not title or not artist:
            # 去掉曲目号后按「艺人 - 歌名」拆分；只填补缺失字段
            stem = re.sub(r"^\s*(?:\d+[\s._-]+)+", "", path.stem).strip()
            left, _, right = stem.partition(" - ")
            if right.strip():
                if not artist:
                    artist = left.strip()
                if not title:
                    title = right.strip()
            if not title:
                title = stem or path.stem

        return title, artist, album, duration

    @staticmethod
    def _write_sidecar_atomic(target: Path, content: str) -> None:
        """同目录临时文件写入后原子替换，避免播放器读到半截歌词。"""
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.stem[:40]}.",
            suffix=target.suffix,
            dir=str(target.parent),
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content.rstrip() + "\n")
            os.replace(temp_path, target)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _process_one_audio(self, path: Path) -> None:
        """单个音轨：跳过已有歌词 → 自行取词 → 内嵌或写旁挂。"""
        path = Path(path)
        try:
            if has_embedded_lyrics(path):
                logger.debug(f"【多源歌词】跳过：{path}（已有内嵌歌词）")
                return
        except Exception as error:
            logger.warning(
                f"【多源歌词】跳过：{path}（读取内嵌歌词失败："
                f"{error.__class__.__name__}）：{error}"
            )
            return

        sidecar = self._existing_sidecar(path)
        if sidecar is not None:
            logger.debug(f"【多源歌词】跳过：{path}（已有旁挂歌词 {sidecar.name}）")
            return

        title, artist, album, duration = self._read_audio_metadata(path)
        if not title:
            logger.debug(f"【多源歌词】跳过：{path}（无法确定歌名）")
            return

        try:
            query = MetaMusic(
                title=title,
                artists=[artist] if artist else [],
                album=album or None,
                album_artist=artist or None,
                duration=duration,
            )
        except Exception as error:
            logger.warning(
                f"【多源歌词】构造查询失败：{path}（{error.__class__.__name__}）：{error}"
            )
            return

        lyrics = self.music_lyrics(query)
        if lyrics is None:
            logger.debug(f"【多源歌词】跳过：{path}（取词失败：{title} / {artist}）")
            return

        synced = (lyrics.synced_lyrics or "").strip() or None
        plain = (lyrics.plain_lyrics or "").strip() or None
        if not synced and not plain:
            logger.debug(f"【多源歌词】跳过：{path}（歌词为空）")
            return

        provider = getattr(lyrics, "provider", "-")
        if self._embed_lyrics and path.suffix.casefold() in SUPPORTED_EXTENSIONS:
            outcome = write_lyrics_direct(path, synced, plain)
            if outcome.status == "embedded":
                logger.info(
                    f"【多源歌词】内嵌歌词成功：{path}（{provider}，"
                    f"时间轴={'有' if synced else '无'}）"
                )
            else:
                logger.warning(
                    f"【多源歌词】内嵌歌词失败：{path}；{outcome.detail}"
                )
            return

        target = path.with_suffix(".lrc" if synced else ".txt")
        self._write_sidecar_atomic(target, synced or plain or "")
        logger.info(
            f"【多源歌词】写入旁挂歌词成功：{target}（{provider}，"
            f"时间轴={'有' if synced else '无'}）"
        )

    # ------------------------------------------------------------------ #
    # 模块声明
    # ------------------------------------------------------------------ #
    def get_module(self) -> Dict[str, Any]:
        """向宿主模块调度器声明本插件实现的模块能力。

        MoviePilot 只调度 ``get_module()`` 返回映射中登记的方法；缺少这个声明时，
        即使插件加载成功、歌词方法可调用，刮削链也不会调用它。
        """
        return {
            "music_lyrics": self.music_lyrics,
            "music_lyrics_candidates": self.music_lyrics_candidates,
        }

    # ------------------------------------------------------------------ #
    # 宿主歌词接口
    # ------------------------------------------------------------------ #
    def music_lyrics(self, music: Any) -> Optional[MusicLyrics]:
        """
        返回当前歌曲最可信的一条歌词，用于短路内置 LRCLIB。

        :param music: MetaMusic 或 MusicInfo
        :return: 最佳候选；没有可信结果时返回 None
        """
        candidates = self.music_lyrics_candidates(music)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                item.quality_rank,
                item.match_score,
                item.provider_priority,
            ),
        )

    def music_lyrics_candidates(self, music: Any) -> List[MusicLyrics]:
        """
        逐源串行探测，每个命中源返回一条候选。

        单源异常只记录日志，不影响其它源；三源全空返回空列表。

        :param music: MetaMusic 或 MusicInfo
        :return: 按匹配分降序、源优先级升序排列的候选列表
        """
        if not self._enabled:
            return []
        if not self._providers:
            return []

        query = self._build_query(music)
        if not query.title:
            logger.debug("【多源歌词】缺少歌名，跳过")
            return []

        cache_key = (
            query.title,
            query.artists,
            query.album,
            query.album_artist,
            query.duration,
            self._translation_mode,
            tuple(provider.name for provider in self._providers),
            self._max_candidates,
        )
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            logger.debug(f"【多源歌词】查询缓存命中：{query.title}（{len(cached)} 条）")
            return list(cached)

        deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
        results: List[MusicLyrics] = []
        for provider in self._providers:
            if time.monotonic() >= deadline:
                logger.info(
                    f"【多源歌词】单歌预算 {_TOTAL_BUDGET_SECONDS:g}s 已耗尽，"
                    f"返回已有 {len(results)} 个候选"
                )
                break
            try:
                provider_result = provider.fetch(query, deadline)
            except DeadlineExceeded:
                logger.info(
                    f"【多源歌词】{provider.name} 因总预算耗尽中止，"
                    f"返回已有 {len(results)} 个候选"
                )
                break
            except Exception as error:  # 单源异常隔离，绝不影响其它源
                logger.warning(
                    f"【多源歌词】{provider.name} 异常（{error.__class__.__name__}）：{error}"
                )
                continue
            if not provider_result:
                continue
            lyrics = self._to_music_lyrics(provider_result)
            if lyrics is not None:
                results.append(lyrics)

        results.sort(key=lambda item: (item.match_score, item.provider_priority), reverse=True)
        results = results[: self._max_candidates]
        self._query_cache.set(cache_key, results)
        if results:
            logger.info(
                "【多源歌词】"
                + query.title
                + " 候选："
                + "；".join(
                    f"{item.provider}({item.match_score})" for item in results
                )
            )
        else:
            logger.info(f"【多源歌词】{query.title} 三源均无歌词")
        return list(results)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_query(music: Any) -> LyricsQuery:
        """从 MetaMusic / MusicInfo 提取并规范化查询条件。"""
        title = str(getattr(music, "title", None) or "").strip()
        album_artist = str(getattr(music, "album_artist", None) or "").strip()
        artists = iter_artist_names(getattr(music, "artists", None))
        if not artists and album_artist:
            artists = (album_artist,)
        duration = to_optional_int(getattr(music, "duration", None))
        if duration is not None and duration <= 0:
            duration = None
        return LyricsQuery(
            title=title,
            artists=tuple(artists),
            album=str(getattr(music, "album", None) or "").strip(),
            album_artist=album_artist,
            duration=duration,
        )

    def _to_music_lyrics(self, result: ProviderLyrics) -> Optional[MusicLyrics]:
        """把 Provider 结果按当前翻译模式转换为 MusicLyrics。"""
        payload = result.payload
        synced, plain = build_lyrics(
            payload.original_lrc,
            payload.translation_lrc,
            self._translation_mode,
        )
        if not synced and not plain:
            return None
        return MusicLyrics(
            provider=payload.provider,
            provider_id=payload.provider_id,
            synced_lyrics=synced,
            plain_lyrics=plain,
            match_score=int(result.match_score),
            provider_priority=int(result.provider_priority),
        )

    # ------------------------------------------------------------------ #
    # 宿主页面 / API
    # ------------------------------------------------------------------ #
    def get_api(self) -> List[Dict[str, Any]]:
        """不注册独立 API。"""
        return []

    def get_page(self) -> Optional[List[Dict[str, Any]]]:
        """不提供详情页。"""
        return []

    def get_form(self) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        """返回 Vuetify 配置表单与默认值。"""
        source_items = [
            {"title": _SOURCE_LABELS[name], "value": name} for name in _VALID_SOURCES
        ]
        translation_items = [
            {"title": _TRANSLATION_LABELS[mode], "value": mode}
            for mode in TRANSLATION_MODES
        ]
        form = [
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
                                            "class": "mb-2",
                                            "style": "white-space: pre-line;",
                                            "text": (
                                                "插件启用后会自动把宿主系统配置 "
                                                "ScrapingSwitchs.music_lyrics 改为 skip，"
                                                "接管歌词刮削；停用插件时恢复原值。\n"
                                                "整理完成事件后，插件自行读取音频标签取词："
                                                "已有内嵌歌词或同目录旁挂 .lrc / .txt / "
                                                ".lyricsfile.yaml 时跳过，否则开启「内嵌歌词」的 "
                                                "FLAC/OGG 直接写内嵌标签，其它情况写 .lrc/.txt。\n"
                                                "匹配要求标题命中且艺人对上（有艺人信息时），"
                                                "时长差超过 10 秒的候选直接淘汰。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "hint": "总开关",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "enabled_sources",
                                            "label": "启用的歌词源",
                                            "items": source_items,
                                            "multiple": True,
                                            "chips": True,
                                            "hint": "三源逐首串行探测；单源失败不影响其它源",
                                            "persistent-hint": True,
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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "translation_mode",
                                            "label": "翻译模式",
                                            "items": translation_items,
                                            "hint": "仅网易云 tlyric / QQ trans 生效；酷狗无独立翻译轨",
                                            "persistent-hint": True,
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
                                            "model": "request_interval",
                                            "label": "请求间隔（秒）",
                                            "type": "number",
                                            "step": 0.1,
                                            "placeholder": "0.3",
                                            "hint": "同一域名相邻请求的最小间隔",
                                            "persistent-hint": True,
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
                                            "model": "timeout",
                                            "label": "单请求超时（秒）",
                                            "type": "number",
                                            "placeholder": "8",
                                            "hint": "单首总预算固定 25 秒",
                                            "persistent-hint": True,
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_candidates",
                                            "label": "最多候选数",
                                            "type": "number",
                                            "placeholder": "4",
                                            "hint": "每个源最多一条，超出时按匹配分截断",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
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
                                            "model": "embed_lyrics",
                                            "label": "内嵌歌词",
                                            "hint": (
                                                "开启后，对整理完成的本地 FLAC/OGG 音轨，"
                                                "把插件自行取到的歌词直接写入音频标签（有时间轴时同时写 "
                                                "LYRICS/UNSYNCEDLYRICS/SYNCEDLYRICS，否则只写 LYRICS），"
                                                "不生成旁挂文件；原文件已有内嵌歌词或已有旁挂时跳过。"
                                                "关闭时（以及 MP3/M4A/WAV/AAC 等格式）改为写 .lrc/.txt。"
                                            ),
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        defaults = {
            "enabled": True,
            "enabled_sources": list(DEFAULT_SOURCES),
            "translation_mode": "merge",
            "request_interval": 0.3,
            "timeout": 8,
            "max_candidates": 4,
            "embed_lyrics": False,
        }
        return form, defaults

    @staticmethod
    def get_command() -> Optional[List[Dict[str, Any]]]:
        """不注册远程命令。"""
        return []
