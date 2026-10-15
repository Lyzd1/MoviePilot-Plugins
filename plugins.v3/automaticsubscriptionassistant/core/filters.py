"""两阶段媒体过滤器。

阶段：``pre``（识别前，仅有 RankMediaItem）、``post``（识别后，附 MediaInfo）。
每个过滤器阈值为 0 / None 时视为未启用，直接放行。

同一个 ``filter_id`` 可以有 pre / post 两套实现，按来源择一：见
``PROVIDER_FILTER_OVERRIDES``。典型是 ``vote``——豆瓣榜单 RSS 自带豆瓣评分，识别后由
``SourceVoteFilter`` 按这份榜单评分判定；其它来源没有榜单评分，只有识别后由宿主提供的
TMDB 评分，走 post 的 ``VoteFilter``。两者判定时机相同（都在识别后），差别只在评分来源：
前者读榜单自带的豆瓣评分，后者读宿主识别出的 TMDB 评分。配置项的 key 不变，故用户已保存
的阈值在两种口径间无缝沿用。

``imdb_vote`` / ``imdb_votes``（post，**默认 8.0 分 + 2 万人**，供奈飞来源使用）改用
**IMDb 的评分与评分人数**判定，取代 TMDB 评分口径：TMDB 上刚上线的新剧常常只有个位数评分
人数，分数噪声极大（实测某剧 TMDB 8.3 分/6 人，IMDb 5.2 分/1189 人）。两者都先取
``mediainfo.imdb_id``，拿不到再用标题搜索兜底（**分级匹配：精确 → 完整前缀 → 类型 + 年份
最接近**，详见 ``_pick_imdb_candidate``），并复用同一份进程内查询结果——同一条目被两个
过滤器各判一次也只查一次网络。判定语义：

- 阈值 ≤ 0 / 空 / 非法 → 未启用，放行，且**不发起任何 IMDb 查询**；
- 查询过程抛异常（含宿主没有 IMDb 客户端）→ **放行**（环境问题不该造成大面积漏订）并记 warn；
- 未匹配到 IMDb 条目 / 条目没有评分 → **拒绝**（刚上线、无人在 IMDb 打分的内容不该被订阅）。

``season_exclude``（「排除第 2 季及以后」，post，**默认关闭**）按来源分派**两套**实现：

- ``TitleSeasonExcludeFilter``（豆瓣 / Mikan 共用）：从**条目标题**解析季号，单位词认
  「季 / 期 / 部」（「部」天然覆盖「第X部分」）。两个来源都不提供 ``item.season``
  （恒为 None），故只能看标题；口径统一后同一条标题在两个来源下判定一致。
- ``NetflixSeasonExcludeFilter``：取**榜单自带的 ``item.season``**（数据源
  ``season_title`` 的 ``Season N``），``item.season`` 为空时不回退标题。

两者都只作用于**剧集**（``mediainfo.type == MediaType.TV``）——电影、综艺及无季号的条目
一律放行，故不会误伤电影续集。它排在 post 过滤链末尾（评分过滤之后），被排除的条目记
``FILTERED`` 且不标记已处理，与其它过滤器一致。
"""
from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, List, NamedTuple, Optional, Type

from app.schemas.types import MediaType
from app.sdk.logging import logger

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


# 标题季号正则：单位词认「季 / 期 / 部」（「部」天然覆盖「第2部分」），
# 同时接受中文数字（``第六季``）与阿拉伯数字（``第2季``）。
_SEASON_UNIT_PATTERN = re.compile(r"第\s*([一二三四五六七八九十百零两\d]+)\s*[季期部]")
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
    """从标题解析季号；无匹配或解析不出数字返回 None（= 不作判定）。

    单位词口径见 ``_SEASON_UNIT_PATTERN``：认「第X季」「第X期」「第X部」
    （``第2部分`` 由「部」命中）。
    """
    match = _SEASON_UNIT_PATTERN.search(str(title or ""))
    if not match:
        return None
    return _cn_number(match.group(1))


# ==================== IMDb 评分查询（imdb_vote / imdb_votes 共用）====================
# 宿主的 IMDb 客户端与配置入口都做防御性导入：拿不到（宿主版本不符 / 离线测试）即视为
# 「查询不可用」，由过滤器按「异常放行」处理——环境问题不该造成大面积漏订。
try:
    from app.modules.imdb.api import ImdbApi
    from app.sdk.config import settings
except Exception:  # noqa: BLE001 - 缺失时降级，不影响插件加载
    ImdbApi = None
    settings = None


class ImdbLookup(NamedTuple):
    """一次 IMDb 查询的结果：是否匹配到条目，以及评分 / 评分人数（都可能缺失）。"""

    matched: bool = False
    imdb_id: Optional[str] = None
    rating: Optional[float] = None
    votes: Optional[int] = None


# 识别类型 -> 允许的 IMDb ``titleType``（搜索兜底时挡下同名不同形态的候选）。
_IMDB_TYPES_BY_MEDIA_TYPE = {
    MediaType.TV: frozenset({"tvSeries", "tvMiniSeries"}),
    MediaType.MOVIE: frozenset({"movie", "tvMovie"}),
}
# 识别类型缺失或非影/剧时的兜底集合：仍只认影/剧，排除 short / videoGame / podcast 等。
_IMDB_TYPES_FALLBACK = frozenset({"movie", "tvMovie", "tvSeries", "tvMiniSeries"})

# 「完整前缀」匹配的长度比下限：短串 / 长串 低于该值就不认前缀命中，防止 `Monster` 这类
# 短名匹配到 `Monster Hunter` 之类的长标题。
_IMDB_PREFIX_MIN_RATIO = 0.6
# 例外：查询串不短于该长度且**是**候选标题的前缀时，不受上面的比值限制。奈飞榜单常给出
# 缩短名（`DAHMER` → IMDb `Dahmer – Monster: The Jeffrey Dahmer Story`，比值仅约 0.15），
# 这类「榜单用简称、IMDb 用全称」的情况必须能命中。
_IMDB_PREFIX_MIN_QUERY = 5

# 进程内查询结果缓存：同一条目被两个过滤器各判一次时只发一次网络（键见 ``_imdb_lookup``）。
_IMDB_CACHE: Dict[str, ImdbLookup] = {}
_IMDB_CACHE_LOCK = threading.Lock()
# 宿主 IMDb 客户端单例（自带跨调用请求缓存）；代理配置变了就重建。
_IMDB_API_LOCK = threading.Lock()
_IMDB_API_STATE: Dict[str, object] = {"proxy": None, "api": None}


def _imdb_api():
    """返回惰性构造并复用的宿主 IMDb 客户端；不可用时抛异常，由调用方按「放行」处理。"""
    if ImdbApi is None:
        raise RuntimeError("宿主未提供 IMDb 客户端（app.modules.imdb.api.ImdbApi 不可用）")
    proxy = (getattr(settings, "PROXY", None) or None) if settings is not None else None
    with _IMDB_API_LOCK:
        if _IMDB_API_STATE["api"] is None or _IMDB_API_STATE["proxy"] != proxy:
            _IMDB_API_STATE["api"] = ImdbApi(proxies=proxy)
            _IMDB_API_STATE["proxy"] = proxy
        return _IMDB_API_STATE["api"]


def _normalize_title(value) -> str:
    """标题归一化：转小写、非字母数字压成单空格、去首尾空格。

    用 Unicode 版 ``\\W``（把下划线一并算作分隔符）：中文字符属于 ``\\w``，故中文标题
    不会被清空——若被清空，两个空串相等会把不同作品误判为同一条目。
    """
    return re.sub(r"[\W_]+", " ", str(value or "").lower()).strip()


def _year_of(value) -> Optional[int]:
    """抽四位年份（``"2019"`` / ``"2019-01-01"`` 均可）；抽不到返回 None。"""
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group(0)) if match else None


def _imdb_query_of(mediainfo) -> str:
    """搜索兜底用的查询串：``en_title`` → ``original_title`` → ``title`` 的第一个非空值。"""
    for attr in ("en_title", "original_title", "title"):
        value = str(getattr(mediainfo, attr, None) or "").strip()
        if value:
            return value
    return ""


def _imdb_candidates(titles, mediainfo) -> list:
    """按识别类型筛出可用的 IMDb 候选（TV/MOVIE 各一套 ``titleType``）。"""
    allowed = _IMDB_TYPES_BY_MEDIA_TYPE.get(
        getattr(mediainfo, "type", None), _IMDB_TYPES_FALLBACK)
    return [t for t in (titles or []) if str(getattr(t, "type", "") or "") in allowed]


def _imdb_title_names(candidate) -> tuple:
    """候选的归一化标题（``primary_title`` / ``original_title``）。"""
    return (_normalize_title(getattr(candidate, "primary_title", None)),
            _normalize_title(getattr(candidate, "original_title", None)))


def _is_prefix_match(query: str, candidate: str) -> bool:
    """分级匹配第 2 级：一方是另一方的前缀，且长度比达标。

    比值低于 ``_IMDB_PREFIX_MIN_RATIO`` 时，只有「查询串不短于 ``_IMDB_PREFIX_MIN_QUERY``
    且**本身是候选标题的前缀**」才放行（榜单用简称、IMDb 用全称的场景）；反向（候选是查询
    的前缀）不享受该例外，避免 ``Monster`` 这类短候选名把 ``Monster: The Lizzie Borden
    Story`` 整个吃掉——那种情况交给第 3 级兜底，由年份（该剧按整剧口径记在 ``Monster`` 下）
    决定。
    """
    if not query or not candidate:
        return False
    if not (candidate.startswith(query) or query.startswith(candidate)):
        return False
    short, long = sorted((query, candidate), key=len)
    if len(short) / len(long) >= _IMDB_PREFIX_MIN_RATIO:
        return True
    return len(query) >= _IMDB_PREFIX_MIN_QUERY and candidate.startswith(query)


def _closest_by_year(candidates, year: Optional[int]):
    """按 ``start_year`` 与 ``year`` 最接近挑一条；``year`` 缺失时取年份最大的一条。

    无年份的候选一律排在有年份的之后（避免把「年份未知」当成精确命中）；同样接近时取
    年份更大的那条（新作更可能是榜单当下推的内容）。
    """
    if year is None:
        return max(candidates,
                   key=lambda t: _year_of(getattr(t, "start_year", None)) or 0)

    def _distance(candidate) -> tuple:
        """(无年份排最后, 年份差, 年份大的优先)。"""
        start_year = _year_of(getattr(candidate, "start_year", None))
        if start_year is None:
            return 1, 0, 0
        return 0, abs(start_year - year), -start_year

    return min(candidates, key=_distance)


def _pick_imdb_candidate(titles, query: str, mediainfo):
    """在 IMDb 搜索结果里挑出最可能对应的一条（分级匹配，命中即停）。

    奈飞榜单与 IMDb 记名口径常不一致，只按「归一化标题完全相等」匹配会大量漏订（实测
    70 个片名里 8 个匹配失败），故分三级放宽：

    1. **精确**：归一化后与候选的 ``primary_title`` / ``original_title`` 相等；
    2. **完整前缀**：一方是另一方的前缀且长度比 ≥ ``_IMDB_PREFIX_MIN_RATIO``；查询串不短于
       ``_IMDB_PREFIX_MIN_QUERY`` 且是候选前缀时不受比值限制（见 ``_is_prefix_match``）；
    3. **兜底**：类型对得上但标题写法完全不同时，取 ``start_year`` 与 ``mediainfo.year``
       最接近的一条（年份缺失取年份最大的一条），并记一条 debug 日志便于回溯。

    每一级内部若有多条命中，仍按 ``_closest_by_year`` 用年份定夺（同名重拍/多版本常见）。
    类型过滤始终生效：没有同类型候选即返回 None（= 未匹配，调用方按拒绝处理）。第 3 级只
    放宽「候选选择」，不放宽「无同类型候选 → 拒绝」。
    """
    wanted = _normalize_title(query)
    candidates = _imdb_candidates(titles, mediainfo)
    if not wanted or not candidates:
        return None
    year = _year_of(getattr(mediainfo, "year", None))

    # 1. 精确匹配。
    exact = [c for c in candidates if wanted in _imdb_title_names(c)]
    if exact:
        return _closest_by_year(exact, year)

    # 2. 完整前缀匹配。
    prefix = [
        c for c in candidates
        if any(_is_prefix_match(wanted, name) for name in _imdb_title_names(c) if name)
    ]
    if prefix:
        return _closest_by_year(prefix, year)

    # 3. 兜底：类型匹配但标题不同，取年份最接近的一条，并记日志便于回溯。
    picked = _closest_by_year(candidates, year)
    logger.debug(
        f"IMDb 非精确匹配：查询「{query}」按类型+年份选中候选"
        f"「{getattr(picked, 'primary_title', None) or getattr(picked, 'original_title', None)}」"
        f"（{getattr(picked, 'id', None)}，{getattr(picked, 'start_year', None)}）"
    )
    return picked


def _fetch_imdb_lookup(mediainfo, imdb_id: str, query: str) -> ImdbLookup:
    """实际发起 IMDb 查询（测试替换此函数即可避免真实网络）。

    先定 ID：``imdb_id`` 非空直接用，否则按标题搜索兜底（``search_titles(query, limit=8)``
    的候选自带类型/年份可供筛选）；再按 ID 取详情读 ``rating.aggregate_rating`` 与
    ``rating.vote_count``——**搜索结果自身的 ``rating`` 恒为 None，评分只能另查详情**。
    网络/解析异常一律向上抛，由过滤器按「放行」处理。
    """
    api = _imdb_api()
    if not imdb_id:
        candidate = _pick_imdb_candidate(
            api.search_titles(query, limit=8) if query else [], query, mediainfo)
        if candidate is None:
            return ImdbLookup(matched=False)
        imdb_id = str(getattr(candidate, "id", "") or "").strip()
        if not imdb_id:
            return ImdbLookup(matched=False)
    title = api.get_title(imdb_id)
    rating = getattr(title, "rating", None) if title is not None else None
    return ImdbLookup(
        matched=True,
        imdb_id=imdb_id,
        rating=getattr(rating, "aggregate_rating", None) if rating is not None else None,
        votes=getattr(rating, "vote_count", None) if rating is not None else None,
    )


def _imdb_lookup(mediainfo) -> ImdbLookup:
    """查一条 MediaInfo 的 IMDb 评分与评分人数（带进程内缓存，同一条目只查一次网络）。

    缓存键优先用 ``mediainfo.imdb_id``；无 id（需按标题搜索）时用「识别类型 + 归一化查询串」
    ——带上类型是因为同名电影与剧集的候选集合不同，只按标题串缓存会让两者互相串味。
    **查询抛异常时不写缓存**，下一个条目/下一轮仍可再试（避免一次网络抖动污染整个进程）。
    """
    imdb_id = str(getattr(mediainfo, "imdb_id", None) or "").strip()
    query = _imdb_query_of(mediainfo)
    if imdb_id:
        key = f"id:{imdb_id}"
    else:
        mtype = getattr(getattr(mediainfo, "type", None), "value", None)
        key = f"q:{mtype}:{_normalize_title(query)}"
    with _IMDB_CACHE_LOCK:
        hit = _IMDB_CACHE.get(key)
    if hit is not None:
        return hit
    result = _fetch_imdb_lookup(mediainfo, imdb_id, query)
    with _IMDB_CACHE_LOCK:
        _IMDB_CACHE[key] = result
    return result


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


class _ImdbFilterBase(MediaFilter):
    """IMDb 口径过滤器（post）的公共骨架：阈值判定 → 查询 → 未匹配/异常处理。

    阈值 ≤ 0 / 空 / 非法视为未启用：**先返回放行、不发起任何 IMDb 查询**。查询过程抛异常
    （含宿主没有 IMDb 客户端）一律放行并记一条 warn——这是环境问题，拦下来会造成大面积漏订。
    查询到「未匹配到 IMDb 条目」才会拒绝；匹配到条目后由子类按各自字段（评分 / 评分人数）
    裁决。两个子类共用 ``_imdb_lookup`` 的进程内缓存，故同一条目只查一次网络。
    """

    stage = "post"

    def accept(self, item, mediainfo, config):
        threshold = self._threshold(config)
        if threshold <= 0:
            return FilterVerdict.accept()
        try:
            lookup = _imdb_lookup(mediainfo)
        except Exception as err:  # noqa: BLE001 - 查询环境问题放行，避免大面积漏订
            logger.warning(f"IMDb 查询失败，{self.filter_id} 本次放行（不拦截该条目）：{err}")
            return FilterVerdict.accept()
        if not lookup.matched:
            return FilterVerdict.reject(self.filter_id, "IMDb 未匹配到条目")
        return self._judge(lookup, threshold)

    def _threshold(self, config):
        """本过滤器的阈值（≤ 0 即未启用）；子类给出各自的取值方式。"""
        raise NotImplementedError

    def _judge(self, lookup: ImdbLookup, threshold) -> FilterVerdict:
        """按本过滤器的口径裁决一条已匹配到的 IMDb 条目。"""
        raise NotImplementedError


class ImdbVoteFilter(_ImdbFilterBase):
    """IMDb 评分过滤（post）：``rating.aggregate_rating`` >= 阈值。

    供奈飞来源取代 TMDB 评分口径：TMDB 上刚上线的新剧评分人数常只有个位数、分数噪声极大，
    IMDb 的评分人数门槛（``ImdbVoteCountFilter``）才挡得住这类高分开局。**无评分的条目**
    （``aggregate_rating`` 为 None 或 ≤ 0）一律不达标——评分缺失时无法判断质量。
    """

    filter_id = "imdb_vote"

    def _threshold(self, config):
        return _to_float((config or {}).get(self.filter_id))

    def _judge(self, lookup, threshold):
        # _to_float 把 None / 非数字统一成 0.0，与「无评分」同义。
        rating = _to_float(lookup.rating)
        if rating <= 0:
            return FilterVerdict.reject(self.filter_id, "IMDb 暂无评分")
        if rating < threshold:
            return FilterVerdict.reject(
                self.filter_id, f"IMDb 评分 {rating} < {threshold}")
        return FilterVerdict.accept()


class ImdbVoteCountFilter(_ImdbFilterBase):
    """IMDb 评分人数过滤（post）：``rating.vote_count`` >= 阈值。

    用于挡掉刚上线、样本太少的高分噪声（如 TMDB 6 人 8.3 分、IMDb 1189 人 5.2 分）。
    评分缺失的条目由 ``ImdbVoteFilter`` 负责拦；本过滤器只认人数，「无人数」同样按不达标处理
    （``_to_int`` 把 None 归一成 0）。
    """

    filter_id = "imdb_votes"

    def _threshold(self, config):
        return _to_int((config or {}).get(self.filter_id))

    def _judge(self, lookup, threshold):
        votes = _to_int(lookup.votes)
        if votes < threshold:
            return FilterVerdict.reject(
                self.filter_id, f"IMDb 评分人数 {votes} < {threshold}")
        return FilterVerdict.accept()


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


class TitleSeasonExcludeFilter(_SeasonExcludeFilter):
    """标题口径（豆瓣 / Mikan 共用）：季号从**条目标题**解析。

    两个来源的 provider 都不提供 ``item.season``（恒为 None），故只能靠标题。单位词认
    「第X季」「第X期」「第X部」（``第2部分`` / ``石纪元 科学与未来 第3部分`` 由「部」命中
    解析出 3）——用于挡「关于我转生变成史莱姆这档事 第四季」这类**当年开播、年份下限挡不住**
    的续作，也覆盖豆瓣侧「地狱老师 第2部分」「某某 第2期」这类按「部/期」记季的条目。

    标题里的年份或尾部数字（``说唱巅峰对决2026``、``一饭封神2``）都不带「第…季/期/部」，
    不会被误判；无季号的剧场版等条目（``剧场版 … 苍海之泪篇``、``花样少年少女``）同理放行。
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
    # IMDb 评分 / 评分人数（奈飞来源改用这套口径，见 providers/netflix.py）。
    ImdbVoteFilter.filter_id: ImdbVoteFilter,
    ImdbVoteCountFilter.filter_id: ImdbVoteCountFilter,
    YearFilter.filter_id: YearFilter,
    PopularityFilter.filter_id: PopularityFilter,
    MediaTypeFilter.filter_id: MediaTypeFilter,
}


# 来源级过滤器覆写：provider_id -> {filter_id -> 过滤器类}（优先于 BUILTIN_FILTERS）。
# 只覆写「该来源有更好的判定口径」的那一项，其余过滤器仍走通用实现。
# 注意：``season_exclude`` **不注册**进 BUILTIN_FILTERS——只有豆瓣/Mikan/奈飞三个来源声明它，
# 且季号来源分两套（标题 / 榜单字段），故只在此按来源分派。
PROVIDER_FILTER_OVERRIDES: Dict[str, Dict[str, Type[MediaFilter]]] = {
    # 豆瓣榜单 RSS 自带豆瓣评分 -> 识别后按这份榜单评分判定（不用识别后的 TMDB 评分）；
    # 季号则只能从条目标题解析（豆瓣不提供 item.season），单位词认「季 / 期 / 部」。
    "douban": {
        SourceVoteFilter.filter_id: SourceVoteFilter,
        "season_exclude": TitleSeasonExcludeFilter,
    },
    # Mikan 同样不提供 item.season，季号从标题解析，与豆瓣共用同一口径（见 providers/mikan.py）。
    "mikan": {"season_exclude": TitleSeasonExcludeFilter},
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
