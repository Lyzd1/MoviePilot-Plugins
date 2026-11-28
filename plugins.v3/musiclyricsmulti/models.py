"""
多源歌词插件的数据模型。

只放纯数据结构，不依赖 MoviePilot 运行时，便于离线单测直接导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class LyricsQuery:
    """
    一次歌词检索的标准化查询条件（来自 MetaMusic / MusicInfo）。

    :param title: 歌名（原始文本，不做繁简或去括号改写）
    :param artists: 歌曲艺术家列表，已去空
    :param album: 专辑名，未知时为空串
    :param album_artist: 专辑艺术家，未知时为空串
    :param duration: 时长（整数秒），未知时为 None
    """

    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    album_artist: str = ""
    duration: Optional[int] = None

    @property
    def title_variants(self) -> tuple[str, ...]:
        """返回检索用的歌名变体。

        MusicBrainz 会把曲名规范成「中文（English）」这类双语形式，而歌词站点通常只
        登记其中一种写法。这里同时给出原串、括号内别名与去掉括号的主体名，
        由各源逐个尝试，避免整首因名称写法不一致而查不到。
        """
        import re

        title = str(self.title or "").strip()
        if not title:
            return ()
        variants: list[str] = [title]
        for inner in re.findall(r"[\(（]([^\)）]+)[\)）]", title):
            inner = inner.strip()
            if inner and len(inner) > 1:
                variants.append(inner)
        outer = re.sub(r"[\(（][^\)）]*[\)）]", " ", title).strip()
        outer = re.sub(r"\s+", " ", outer)
        if outer and outer != title:
            variants.append(outer)
        return tuple(dict.fromkeys(variants))

    @property
    def primary_artist(self) -> str:
        """返回搜索时优先使用的艺术家名。"""
        if self.artists:
            return self.artists[0]
        return self.album_artist


@dataclass
class Candidate:
    """
    单个歌词源搜索返回的一条曲目候选。

    :param title: 源返回的歌名
    :param artists: 源返回的艺术家（可能已按分隔符拆开）
    :param album: 源返回的专辑名
    :param duration: 源返回的时长（整数秒），未知时为 None
    :param provider_id: 该源内的曲目标识（数字 ID / songmid / hash）
    :param extra: 该源取词所需的附加字段，由各 Provider 自行解释
    """

    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration: Optional[int] = None
    provider_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LyricPayload:
    """
    某源成功取得的歌词原文与翻译轨。

    :param provider: 源名称（netease / qqmusic / kugou）
    :param provider_id: 该源内的曲目标识
    :param title: 命中候选的歌名，仅用于日志
    :param original_lrc: 原文歌词，可能是带时间轴 LRC，也可能是纯文本
    :param translation_lrc: 独立翻译轨，源未提供时为 None
    """

    provider: str
    provider_id: str
    title: str
    original_lrc: str
    translation_lrc: Optional[str] = None


@dataclass
class ProviderLyrics:
    """
    Provider 返回给插件层的最终结果，附带本曲匹配分。

    :param payload: 歌词内容
    :param match_score: 0-100 的整数匹配分
    :param provider_priority: 源优先级，数值越大越优先（宿主取最大值）
    """

    payload: LyricPayload
    match_score: int
    provider_priority: int
