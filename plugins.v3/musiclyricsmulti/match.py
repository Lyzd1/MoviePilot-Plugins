"""
歌曲候选归一化、打分与排序。

设计要点：
- 归一化只用于生成匹配键，绝不回写歌词正文或源返回的展示文本。
- 标题必须命中（完全相等或互相包含）；艺人存在时必须至少有一人命中，
  避免同名翻唱 / DJ 版仅靠“包含歌名”蒙混过关。
- 时长缺失只降分不淘汰；时长差超过 10 秒直接淘汰。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .models import Candidate, LyricsQuery

# 方括号 / 圆括号 / 中文书名号内的版本注释，如 (Live)、(原唱 周杰伦)、【伴奏】
_BRACKET_RE = re.compile(r"[\(（\[【][^\)）\]】]*[\)）\]】]")
# 归一化时去掉所有空白和标点，只保留文字、数字与下划线
_NON_WORD_RE = re.compile(r"[^\w]+", flags=re.UNICODE)
# 艺术家字段的常见分隔符
_ARTIST_SPLIT_RE = re.compile(r"[/&,，、;；]|\s+(?:feat\.?|ft\.?)\s+", flags=re.IGNORECASE)

# 时长容差（秒）
_DURATION_ELIMINATE = 10
_DURATION_STRONG = 2
_DURATION_WEAK = 5

# 标题 / 艺人 / 专辑 / 时长权重
_TITLE_EXACT = 45
_TITLE_CONTAINS = 25
_ARTIST_HIT = 30
_ARTIST_ALL_BONUS = 5
_ALBUM_HIT = 10
_DURATION_STRONG_BONUS = 15
_DURATION_WEAK_BONUS = 5
# 查询未要求的版本标记命中时扣分，避免选中 Live / Remix / 伴奏等版本
_VERSION_MARKERS = (
    "live", "remix", "instrumental", "伴奏", "纯音乐", "cover", "翻唱",
    "dj", "demo", "acoustic", "karaoke", "off vocal",
)
_VERSION_PENALTY = 20


def _host_to_simplified(value: str) -> str:
    """优先用宿主的繁简转换能力，其次 zhconv，最后原样返回。"""
    try:  # MoviePilot 宿主
        from app.foundation.text import convert  # type: ignore

        return str(convert(value, "zh-hans"))
    except Exception:
        pass
    try:
        import zhconv  # type: ignore

        return str(zhconv.convert(value, "zh-hans"))
    except Exception:
        return value


def normalize_text(value: object) -> str:
    """
    生成匹配键：去括号注释、去首尾空白、casefold、繁转简，再去标点与空白。

    该函数只服务于匹配，调用方不得把结果用于歌词正文或候选展示文本。

    :param value: 任意待归一化文本
    :return: 归一化后的匹配键
    """
    text = str(value or "")
    text = _BRACKET_RE.sub("", text)
    text = text.strip()
    text = _host_to_simplified(text.casefold())
    return _NON_WORD_RE.sub("", text)


def split_artists(value: object) -> tuple[str, ...]:
    """
    拆分艺术家字段，兼容 ``A/B``、``A & B``、``A、B`` 与 ``A feat. B``。

    :param value: 源返回的艺术家字段，可为字符串或可迭代对象
    :return: 去空后的艺术家名元组
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = [str(value)]
    artists: list[str] = []
    for raw in raw_items:
        for part in _ARTIST_SPLIT_RE.split(raw):
            name = part.strip()
            if name:
                artists.append(name)
    return tuple(artists)


def _compatible_text(expected: str, candidate: str) -> bool:
    """判断两个归一化文本是否可视为同一艺人/专辑（相等或互相包含）。"""
    if not expected or not candidate:
        return False
    return expected == candidate or expected in candidate or candidate in expected


@dataclass(frozen=True)
class ScoredCandidate:
    """带匹配分与淘汰原因的候选。"""

    candidate: Candidate
    score: int
    title_score: int
    artist_score: int
    album_score: int
    duration_score: int
    eligible: bool
    reason: str = ""


def _contains_marker(text: str, marker: str) -> bool:
    """判断版本标记是否作为独立词出现在标题中，避免 alive 误判为 live。"""
    if not text:
        return False
    if marker.isascii():
        return re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", text) is not None
    return marker in text


def score_candidate(query: LyricsQuery, candidate: Candidate) -> ScoredCandidate:
    """
    按规格给单条候选打分。

    - 标题完全相等 45 / 包含 25；
    - 艺人任一人命中 30，全部命中再 +5；
    - 专辑命中 10；
    - 时长差 ≤2s +15、≤5s +5、>10s 淘汰；时长缺失不淘汰。
    - 标题未命中，或查询有艺人但艺人未命中 → 直接不合适。

    :param query: 标准化查询条件
    :param candidate: 源返回的候选
    :return: 打分结果
    """
    expected_title = normalize_text(query.title)
    cand_title = normalize_text(candidate.title)

    # 双语标题（「中文（English）」）只要有一支写法对上就算命中
    expected_variants = {
        normalize_text(variant) for variant in getattr(query, "title_variants", ()) or ()
    }
    expected_variants.discard("")

    title_score = 0
    if expected_variants and cand_title:
        if cand_title in expected_variants:
            title_score = _TITLE_EXACT
        elif any(
            variant in cand_title or cand_title in variant
            for variant in expected_variants
        ):
            title_score = _TITLE_CONTAINS
    elif expected_title and cand_title:
        if expected_title == cand_title:
            title_score = _TITLE_EXACT
        elif expected_title in cand_title or cand_title in expected_title:
            title_score = _TITLE_CONTAINS

    expected_artists = tuple(a for a in (normalize_text(a) for a in query.artists) if a)
    cand_artists = tuple(a for a in (normalize_text(a) for a in candidate.artists) if a)
    artist_score = 0
    if expected_artists:
        matched = [
            expected
            for expected in expected_artists
            if any(_compatible_text(expected, cand) for cand in cand_artists)
        ]
        if matched:
            artist_score = _ARTIST_HIT
            if len(matched) == len(expected_artists):
                artist_score += _ARTIST_ALL_BONUS

    album_score = 0
    expected_album = normalize_text(query.album)
    if expected_album and _compatible_text(expected_album, normalize_text(candidate.album)):
        album_score = _ALBUM_HIT

    duration_score = 0
    duration_eliminated = False
    if query.duration and candidate.duration:
        diff = abs(int(query.duration) - int(candidate.duration))
        if diff > _DURATION_ELIMINATE:
            duration_eliminated = True
        elif diff <= _DURATION_STRONG:
            duration_score = _DURATION_STRONG_BONUS
        elif diff <= _DURATION_WEAK:
            duration_score = _DURATION_WEAK_BONUS

    eligible = True
    reason = ""
    if title_score == 0:
        eligible = False
        reason = "标题未命中"
    elif expected_artists and artist_score == 0:
        eligible = False
        reason = "艺人未命中"
    elif duration_eliminated:
        eligible = False
        reason = "时长相差超过 10 秒"

    # 版本判断必须看原始标题：归一化会去掉括号里的 (Live)/(Remix) 标记
    raw_cand_title = str(candidate.title or "").casefold()
    raw_expected_title = str(query.title or "").casefold()
    version_penalty = 0
    if raw_cand_title and any(
        _contains_marker(raw_cand_title, marker)
        and not _contains_marker(raw_expected_title, marker)
        for marker in _VERSION_MARKERS
    ):
        version_penalty = _VERSION_PENALTY

    total = title_score + artist_score + album_score + duration_score - version_penalty
    return ScoredCandidate(
        candidate=candidate,
        score=min(100, total),
        title_score=title_score,
        artist_score=artist_score,
        album_score=album_score,
        duration_score=duration_score,
        eligible=eligible,
        reason=reason,
    )


def rank_candidates(
    query: LyricsQuery,
    candidates: Iterable[Candidate],
) -> list[ScoredCandidate]:
    """
    过滤不合适候选并按匹配分降序排序；同分时保留源返回顺序。

    :param query: 标准化查询条件
    :param candidates: 源返回候选
    :return: 仅含合适候选的排序结果；没有命中时为空列表
    """
    scored: list[tuple[int, ScoredCandidate]] = []
    for index, candidate in enumerate(candidates):
        result = score_candidate(query, candidate)
        if result.eligible:
            scored.append((index, result))
    scored.sort(key=lambda pair: (-pair[1].score, pair[0]))
    return [result for _, result in scored]


def pick_best(
    query: LyricsQuery,
    candidates: Iterable[Candidate],
) -> Optional[ScoredCandidate]:
    """返回排序后的第一条合适候选，绝不直接取搜索首条。"""
    ranked = rank_candidates(query, candidates)
    return ranked[0] if ranked else None


def to_optional_int(value: object) -> Optional[int]:
    """
    把来源不一的时长字段安全转成整数秒。

    支持 ``"269"``、``269.0``、``None``；无法解析时返回 None。
    """
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def iter_artist_names(value: object) -> Sequence[str]:
    """把宿主的 artists 字段统一成字符串序列（保留顺序）。"""
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        names: list[str] = []
        for item in value:
            name = getattr(item, "name", item)
            text = str(name or "").strip()
            if text:
                names.append(text)
        return tuple(names)
    text = str(value).strip()
    return (text,) if text else ()
