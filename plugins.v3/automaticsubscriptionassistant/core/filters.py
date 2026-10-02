"""两阶段媒体过滤器。

阶段：``pre``（识别前，仅有 RankMediaItem）、``post``（识别后，附 MediaInfo）。
每个过滤器阈值为 0 / None 时视为未启用，直接放行。

同一个 ``filter_id`` 可以有 pre / post 两套实现，按来源择一：见
``PROVIDER_FILTER_OVERRIDES``。典型是 ``vote``——豆瓣榜单 RSS 自带豆瓣评分，识别后由
``SourceVoteFilter`` 按这份榜单评分判定；其它来源没有榜单评分，只有识别后由宿主提供的
TMDB 评分，走 post 的 ``VoteFilter``。两者判定时机相同（都在识别后），差别只在评分来源：
前者读榜单自带的豆瓣评分，后者读宿主识别出的 TMDB 评分。配置项的 key 不变，故用户已保存
的阈值在两种口径间无缝沿用。

``season_exclude``（「排除第 2 季及以后」，post，**默认关闭**）同样按来源分派两套实现：
``DoubanSeasonExcludeFilter`` 从**条目标题**解析「第X季」（豆瓣不提供季号）；``NetflixSeasonExcludeFilter``
取**榜单自带的 ``item.season``**（数据源 ``season_title`` 的 ``Season N``）。二者只作用于
**剧集**（``mediainfo.type == MediaType.TV``）——电影、综艺及无季号的条目一律放行，故不会
误伤电影续集。它排在 post 过滤链末尾（评分过滤之后），被排除的条目记 ``FILTERED`` 且不标记
已处理，与其它过滤器一致。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, List, Optional, Type

from app.schemas.types import MediaType

from .models import FilterVerdict, RankMediaItem

if TYPE_CHECKING:
    from app.sdk.media import MediaInfo


def _to_int(value) -> int:
    """安全转 int，失败返回 0。"""
    if value is None:
        return 0
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def _to_float(value) -> float:
    """安全转 float，失败返回 0.0。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _is_truthy(value) -> bool:
    """开关值解析：bool 直接取；字符串接受 true/on/yes/1（大小写不敏感）；其余为假。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "on", "yes", "1")
    return bool(value)


# 「第X季」季号正则：同时接受中文数字（``第六季``）与阿拉伯数字（``第2季``）。
_SEASON_CN_PATTERN = re.compile(r"第\s*([一二三四五六七八九十百零两\d]+)\s*季")
# 中文数字查表（仅用于季号解析，覆盖到「百」足够）。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100}


def _cn_number(text: str) -> Optional[int]:
    """把捕获到的数字串转成 int：纯阿拉伯数字直接转，中文数字按位累加。

    ``二十``→20、``六``→6、``一百零五``→105、``12``→12；含未知字符返回 None。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    section, number = 0, 0
    for ch in raw:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            # 「十二」的十位省略了数字，按 1 计。
            section += (number or 1) * _CN_UNITS[ch]
            number = 0
        else:
            return None
    return section + number


def _parse_title_season(title: str) -> Optional[int]:
    """从标题解析「第X季」的季号；无匹配或解析不出数字返回 None（= 不作判定）。"""
    match = _SEASON_CN_PATTERN.search(str(title or ""))
    if not match:
        return None
    return _cn_number(match.group(1))


class MediaFilter(ABC):
    """过滤器基类。"""

    filter_id: str = ""
    stage: str = "pre"  # "pre" | "post"

    @abstractmethod
    def accept(self, item: RankMediaItem, mediainfo: Optional["MediaInfo"], config: dict) -> FilterVerdict:
        """裁决单条条目是否通过。"""
        raise NotImplementedError


class VoteFilter(MediaFilter):
    """评分过滤（post）：用 mediainfo.vote_average >= 阈值。"""

    filter_id = "vote"
    stage = "post"

    def accept(self, item, mediainfo, config):
        threshold = _to_float((config or {}).get("vote"))
        if threshold <= 0:
            return FilterVerdict.accept()
        vote = _to_float(getattr(mediainfo, "vote_average", None))
        if vote >= threshold:
            return FilterVerdict.accept()
        return FilterVerdict.reject(self.filter_id, f"评分 {vote} < {threshold}")


class SourceVoteFilter(MediaFilter):
    """评分过滤（post）：用榜单自带的豆瓣评分 ``item.source_vote`` >= 阈值。

    与 ``VoteFilter`` 复用同一个 ``filter_id``（配置 key 不变），仅在豆瓣来源上覆写
    （见 ``PROVIDER_FILTER_OVERRIDES``）。**判定时机与 ``VoteFilter`` 相同（都在识别后）**，
    区别只在评分来源：本实现读榜单自带的豆瓣评分，``VoteFilter`` 读识别后的 TMDB 评分。

    本来源选择「先识别、再用榜单豆瓣评分判」：被过滤的条目也已经过识别，历史里因此带上封面
    与媒体身份（``media_source/media_id``）；代价是每条都要发识别请求（不再靠提前拦截省掉
    不达标条目的识别）。识别失败的条目落「未识别」，不进入本判定（与 ``VoteFilter`` 一致）。

    判定位于 post（executor 第 5 步），仍**早于**「媒体库查重」「订阅查重」：故「已在媒体库
    且豆瓣分不达标」的条目会记为「已过滤」而不是「媒体库已存在」（与原始代码的步骤顺序一致）。
    ``mediainfo`` 在这里只表示识别已完成，**不参与判定**——豆瓣口径单一，不拿 TMDB 评分兜底。

    无评分的条目（暂无评分 / 值为 0.0）视为不达标——被拦下的条目只记历史、不标记已处理，
    下一轮榜单若给出评分可再被处理。
    """

    filter_id = "vote"
    stage = "post"

    # 无可用评分时的统一原因（暂无评分与 0.0 同义）。
    NO_VOTE_REASON = "榜单未给评分（暂无/0.0）"

    def accept(self, item, mediainfo, config):
        threshold = _to_float((config or {}).get("vote"))
        if threshold <= 0:
            return FilterVerdict.accept()
        # post 阶段 mediainfo 必非 None，但本口径只看榜单自带评分，忽略识别结果。
        vote = item.source_vote if item is not None else None
        if vote is None:
            return FilterVerdict.reject(self.filter_id, self.NO_VOTE_REASON)
        vote = _to_float(vote)
        if vote <= 0:
            return FilterVerdict.reject(self.filter_id, self.NO_VOTE_REASON)
        if vote >= threshold:
            return FilterVerdict.accept()
        return FilterVerdict.reject(self.filter_id, f"评分 {vote} < {threshold}")


class YearFilter(MediaFilter):
    """年份过滤（pre）：item.year（或 mediainfo.year）>= 阈值年份。"""

    filter_id = "year"
    stage = "pre"

    def accept(self, item, mediainfo, config):
        threshold = _to_int((config or {}).get("year"))
        if threshold <= 0:
            return FilterVerdict.accept()
        raw_year = (item.year if item and item.year else None) \
            or (getattr(mediainfo, "year", None) if mediainfo else None)
        year = _to_int(raw_year)
        # 无有效年份信息时放行，避免误伤缺字段条目。
        if year <= 0:
            return FilterVerdict.accept()
        if year >= threshold:
            return FilterVerdict.accept()
        return FilterVerdict.reject(self.filter_id, f"年份 {year} < {threshold}")


class PopularityFilter(MediaFilter):
    """热度过滤（pre）：item.source_meta['count'] >= 阈值。"""

    filter_id = "popularity"
    stage = "pre"

    def accept(self, item, mediainfo, config):
        threshold = _to_int((config or {}).get("popularity"))
        if threshold <= 0:
            return FilterVerdict.accept()
        count = _to_int((item.source_meta or {}).get("count")) if item else 0
        if count >= threshold:
            return FilterVerdict.accept()
        return FilterVerdict.reject(self.filter_id, f"订阅人次 {count} < {threshold}")


class MediaTypeFilter(MediaFilter):
    """类型过滤（post）：config['media_type'] ∈ {all, movie, tv}。"""

    filter_id = "media_type"
    stage = "post"

    def accept(self, item, mediainfo, config):
        wanted = str((config or {}).get("media_type") or "all").strip().lower()
        if wanted not in ("movie", "tv"):
            return FilterVerdict.accept()
        mtype = getattr(mediainfo, "type", None) if mediainfo else None
        if wanted == "movie" and mtype != MediaType.MOVIE:
            return FilterVerdict.reject(self.filter_id, "非电影")
        if wanted == "tv" and mtype != MediaType.TV:
            return FilterVerdict.reject(self.filter_id, "非电视剧")
        return FilterVerdict.accept()


class _SeasonExcludeFilter(MediaFilter):
    """「排除第 2 季及以后」过滤（post）的公共判定骨架，季号来源由子类给出。

    只作用于剧集：``mediainfo is None``（未识别）或 ``mediainfo.type != MediaType.TV``
    一律放行——不做反向猜测，避免误伤电影续集。开关未开 / 未设置 / 值非真也一律放行。
    排在 post 过滤链末尾（评分过滤之后），命中的条目记 ``FILTERED`` 且不标记已处理。
    """

    filter_id = "season_exclude"
    stage = "post"

    def accept(self, item, mediainfo, config):
        if not _is_truthy((config or {}).get(self.filter_id)):
            return FilterVerdict.accept()
        if mediainfo is None or getattr(mediainfo, "type", None) != MediaType.TV:
            return FilterVerdict.accept()
        season = self._season_of(item)
        if season is not None and season >= 2:
            return FilterVerdict.reject(self.filter_id, f"第 {season} 季（仅保留第 1 季）")
        return FilterVerdict.accept()

    def _season_of(self, item) -> Optional[int]:
        """返回条目的季号；无法判定返回 None（= 放行）。"""
        raise NotImplementedError


class DoubanSeasonExcludeFilter(_SeasonExcludeFilter):
    """豆瓣口径：季号从**条目标题**解析（``流人 第六季`` / ``某某 第2季``）。

    豆瓣 provider 不提供 ``item.season``（恒为 None），故只能靠标题；只认「第X季」，
    不认「部/辑/期」。标题里的年份或尾部数字（``说唱巅峰对决2026``、``一饭封神2``）
    都不带「第…季」，不会被误判。
    """

    def _season_of(self, item) -> Optional[int]:
        return _parse_title_season(getattr(item, "title", ""))


class NetflixSeasonExcludeFilter(_SeasonExcludeFilter):
    """奈飞口径：季号取榜单自带的 ``item.season``（数据源 ``season_title`` 的 ``Season N``）。

    ``item.season`` 为 None（电影 / ``Collection N`` / ``Limited Series`` 等）时放行，
    **不回退**标题正则——奈飞条目的季号只在榜单数据里，识别结果与英文标题都拿不到。
    """

    def _season_of(self, item) -> Optional[int]:
        season = getattr(item, "season", None)
        return season if isinstance(season, int) else None


class FilterChain:
    """按阶段顺序执行过滤器，短路于首个拒绝。"""

    def __init__(self, filters: List[MediaFilter]):
        self._filters = filters or []

    def run(self, stage: str, item, mediainfo, config) -> FilterVerdict:
        """执行指定阶段的所有过滤器；任一拒绝立即返回该裁决。"""
        for f in self._filters:
            if f.stage != stage:
                continue
            verdict = f.accept(item, mediainfo, config)
            if not verdict.accepted:
                return verdict
        return FilterVerdict.accept()


# 内置过滤器注册表：filter_id -> 过滤器类。
BUILTIN_FILTERS: Dict[str, Type[MediaFilter]] = {
    VoteFilter.filter_id: VoteFilter,
    YearFilter.filter_id: YearFilter,
    PopularityFilter.filter_id: PopularityFilter,
    MediaTypeFilter.filter_id: MediaTypeFilter,
}


# 来源级过滤器覆写：provider_id -> {filter_id -> 过滤器类}（优先于 BUILTIN_FILTERS）。
# 只覆写「该来源有更好的判定口径」的那一项，其余过滤器仍走通用实现。
# 注意：``season_exclude`` **不注册**进 BUILTIN_FILTERS——只有豆瓣/奈飞两个来源声明它，
# 且两套实现季号来源不同，故只在此按来源分派。
PROVIDER_FILTER_OVERRIDES: Dict[str, Dict[str, Type[MediaFilter]]] = {
    # 豆瓣榜单 RSS 自带豆瓣评分 -> 识别后按这份榜单评分判定（不用识别后的 TMDB 评分）；
    # 季号则只能从条目标题解析（豆瓣不提供 item.season）。
    "douban": {
        SourceVoteFilter.filter_id: SourceVoteFilter,
        "season_exclude": DoubanSeasonExcludeFilter,
    },
    # 奈飞季号来自榜单数据自带的 item.season（见 providers/netflix.py）。
    "netflix": {"season_exclude": NetflixSeasonExcludeFilter},
}


def build_filter_chain(filter_ids: List[str], provider_id: Optional[str] = None) -> FilterChain:
    """按 spec 声明的 filter_ids 挑选实例，未知 id 跳过。

    先查来源覆写（``PROVIDER_FILTER_OVERRIDES``），未覆写再落回内置注册表；
    ``provider_id`` 缺省（None）时等价于全部走内置实现。
    """
    overrides = PROVIDER_FILTER_OVERRIDES.get(provider_id or "", {})
    filters: List[MediaFilter] = []
    for fid in filter_ids or []:
        cls = overrides.get(fid) or BUILTIN_FILTERS.get(fid)
        if cls is not None:
            filters.append(cls())
    return FilterChain(filters)
