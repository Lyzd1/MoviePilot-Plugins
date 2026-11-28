"""
多源歌词插件（MoviePilot v3）。

聚合网易云、QQ 音乐、酷狗三个歌词源：
- ``music_lyrics`` 返回最可信的一条，用于短路内置 LRCLIB；
- ``music_lyrics_candidates`` 每个源返回一条候选，交给宿主 LyricsChain 择优。

同步实现（宿主插件接口是同步的），只读网络请求，不写任何文件。

内嵌歌词（1.0.1 新增，默认关闭）：
- 开启 ``embed_lyrics`` 后，监听整理完成事件：影视用 ``TransferComplete``
  （``transfer.complete``），音频用 ``AudioTransferComplete``
  （``transfer.audio.complete``，宿主对音频文件实际发布的事件），两者
  payload 结构一致；在后台延迟几秒处理本次整理到本地的 FLAC/OGG 音轨；
- 原文件已有内嵌歌词（LYRICS / SYNCEDLYRICS / UNSYNCEDLYRICS / lyricsfile）
  时完全跳过；
- 否则读取宿主刚生成的旁挂歌词（.lrc > .txt > .lyricsfile.yaml），用
  mutagen 原子写入内嵌标签：有时间轴时三写——``LYRICS`` = LRC 全文（主流
  播放器读这个键，必须带时间轴）、``UNSYNCEDLYRICS`` = 纯文本、
  ``SYNCEDLYRICS`` = LRC 全文（兼容 MusicBee 等）；没有时间轴时只写
  ``LYRICS`` = 纯文本。成功后删除该音轨的旁挂歌词文件；其它格式与开关
  关闭时行为保持不变。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.domain.context import MusicLyrics  # type: ignore
from app.schemas.types import EventType  # type: ignore
from app.sdk.events import Event, eventmanager  # type: ignore
from app.sdk.logging import logger  # type: ignore
from app.sdk.plugin import _PluginBase  # type: ignore

from .cache import TTLCache
from .embed import SUPPORTED_EXTENSIONS, embed_sidecar_lyrics
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
# 整理完成事件后延迟多少秒再处理内嵌歌词，避免与刮削/外部搬运竞态
_EMBED_DELAY_SECONDS = 3.0

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
    plugin_version = "1.0.2"
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

    def get_state(self) -> bool:
        """返回插件总开关状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """关闭 HTTP 连接池。"""
        with self._lock:
            http = self._http
            self._http = None
            self._providers = []
        if http is not None:
            http.close()
        self._query_cache.clear()

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
        """整理完成事件的统一处理：开关开启时把本地 FLAC/OGG 的旁挂歌词内嵌。

        只做路径筛选与线程投递，真正的写标签、删旁挂放后台 daemon 线程，
        延迟 ``_EMBED_DELAY_SECONDS`` 秒执行，避免与刮削/外部搬运竞态。
        任何异常都在这里或工作线程内捕获，绝不抛回宿主事件总线。
        """
        try:
            if not self._embed_lyrics:
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
                target=self._embed_audio_paths,
                args=(paths,),
                name="musiclyricsmulti-embed",
                daemon=True,
            ).start()
        except Exception as error:
            logger.warning(
                f"【多源歌词】内嵌歌词事件处理异常（{error.__class__.__name__}）：{error}"
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
        """从整理结果中提取本机真实存在的 FLAC/OGG 音轨路径。"""
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
            if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
                continue
            if not path.is_file():
                continue
            paths.append(path)
        return paths

    def _embed_audio_paths(self, paths: List[Path]) -> None:
        """后台线程：延迟后逐个写入内嵌歌词，异常只记 WARNING。"""
        try:
            time.sleep(_EMBED_DELAY_SECONDS)
            for path in paths:
                try:
                    outcome = embed_sidecar_lyrics(path)
                except Exception as error:
                    logger.warning(
                        f"【多源歌词】内嵌歌词处理异常：{path}（"
                        f"{error.__class__.__name__}）：{error}"
                    )
                    continue
                if outcome.status == "embedded":
                    deleted = "、".join(outcome.deleted) or "无"
                    logger.info(
                        f"【多源歌词】内嵌歌词成功：{path}；已删除旁挂：{deleted}"
                    )
                    if outcome.failed_deletes:
                        logger.warning(
                            f"【多源歌词】部分旁挂删除失败："
                            f"{'、'.join(outcome.failed_deletes)}"
                        )
                elif outcome.status == "error":
                    logger.warning(
                        f"【多源歌词】内嵌歌词失败：{path}；{outcome.detail}"
                    )
                else:
                    logger.debug(
                        f"【多源歌词】跳过内嵌：{path}（{outcome.status}：{outcome.detail}）"
                    )
        except Exception as error:
            logger.warning(
                f"【多源歌词】内嵌歌词后台任务异常（{error.__class__.__name__}）：{error}"
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
                                                "插件只读请求网易云 / QQ 音乐 / 酷狗歌词，"
                                                "不写文件、不改音频。\n"
                                                "匹配要求标题命中且艺人对上（有艺人信息时），"
                                                "时长差超过 10 秒的候选直接淘汰。\n"
                                                "网易云与 QQ 的翻译轨会按时间戳最近邻合并，"
                                                "配不上的译文丢弃，避免时间轴错位。"
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
                                                "开启后，仅对整理完成的本地 FLAC/OGG 音轨写入内嵌歌词："
                                                "把旁挂歌词写入音频标签（有时间轴时同时写 "
                                                "LYRICS/UNSYNCEDLYRICS/SYNCEDLYRICS，否则只写 LYRICS），"
                                                "成功后删除该音轨的 .lrc / .txt / .lyricsfile.yaml；"
                                                "原文件已有内嵌歌词时跳过。其它格式仍按原逻辑生成旁挂歌词。"
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
