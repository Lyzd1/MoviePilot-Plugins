"""
多源歌词插件（MoviePilot v3）。

聚合网易云、QQ 音乐、酷狗三个歌词源：
- ``music_lyrics`` 返回最可信的一条，用于短路内置 LRCLIB；
- ``music_lyrics_candidates`` 每个源返回一条候选，交给宿主 LyricsChain 择优。

同步实现（宿主插件接口是同步的），只读网络请求，不写任何文件。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.domain.context import MusicLyrics  # type: ignore
from app.sdk.logging import logger  # type: ignore
from app.sdk.plugin import _PluginBase  # type: ignore

from .cache import TTLCache
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
    plugin_version = "1.2.0"
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
        """关闭 HTTP 连接池并清理缓存。"""
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
                                                "聚合网易云、QQ 音乐、酷狗三个歌词源，"
                                                "逐源串行探测并向宿主提供歌词候选，"
                                                "不写入任何文件。\n"
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
        }
        return form, defaults

    @staticmethod
    def get_command() -> Optional[List[Dict[str, Any]]]:
        """不注册远程命令。"""
        return []
