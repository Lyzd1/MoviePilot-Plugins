"""豆瓣榜单来源：解析 RSSHub 豆瓣 RSS，产出标准化条目。

数据来源为 RSSHub 的豆瓣榜单 RSS（内置榜单路由 + 用户自定义地址）。RSSHub 基址可自定义，
部分地区 rsshub.app 被 SNI 黑名单封锁时，可对接用户自建的 RSSHub 实例（``rsshub_base`` 选项）。
抓取逻辑移植自参考插件 ``doubanrankplus``，纯函数化后由统一落地管线消费。
原插件的 70 分钟限流退避窗口本次未移植（见 README）。

**评分口径**：豆瓣榜单 RSS 的 ``description`` 自带豆瓣评分（热度/口碑类为
``评分：7.3分``，TOP250 类为裸数字 ``<p>9.7</p>``），解析后写入 ``item.source_vote``；
``评分：0.0分`` 与解析不到一律视为无评分（None）。豆瓣的「评分≥」由
``SourceVoteFilter`` 在**识别后**按这份榜单评分判定（不达标者不进入订阅流程），
**不使用**识别后的 TMDB 评分（``item.year`` / ``media_type`` 过滤不受影响）。
先识别再判定意味着每条都要发识别请求，换来的是被过滤条目同样带封面与媒体身份。

**排除第 2 季及以后**：``filters_schema`` 的 ``season_exclude`` 开关（默认关）开启后，
按条目标题里的「第X季」（中文数字/阿拉伯数字均可）排除第 2 季及以后的**剧集**；豆瓣不提供
季号（``item.season`` 恒为 None），故只能从标题解析，且只认「季」（不认「部/辑/期」）。
电影、综艺与无季号条目不受影响。
"""
from __future__ import annotations

import re
import xml.dom.minidom
from typing import Iterator, List, Optional

from app.schemas.types import MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.utilities import DomUtils

from ..core.models import FieldSpec, ProviderSpec, RankMediaItem
from ..core.provider import ProviderContext, RankProvider
from ..core.registry import register

# 默认 RSSHub 基址；部分地区 rsshub.app 被 SNI 黑名单封锁，可在配置里改为自建实例。
DEFAULT_RSSHUB_BASE = "https://rsshub.app"

# 单个 RSS 文档允许的最大字符数。榜单 RSS 通常只有几十 KB。
_MAX_RSS_BYTES = 8 * 1024 * 1024

# XML 实体声明。榜单 RSS 用不到，出现即视为不可信内容。
_ENTITY_DECL = re.compile(r"<!ENTITY", re.IGNORECASE)

# 内置榜单路由：key 为 select 选项值，value 为 RSSHub 相对路由（与基址拼接成完整地址）。
DOUBAN_ADDRESS = {
    "movie-ustop": "/douban/movie/ustop",
    "movie-weekly": "/douban/movie/weekly",
    "movie-real-time": "/douban/movie/weekly/movie_real_time_hotest",
    "show-domestic": "/douban/movie/weekly/show_domestic",
    "movie-hot-gaia": "/douban/movie/weekly/movie_hot_gaia",
    "tv-hot": "/douban/movie/weekly/tv_hot",
    "movie-top250": "/douban/list/movie_top250",
}

# 内置榜单路由 -> 媒体类型（RSS 的 <type> 标签在所有榜单里均为空，故用路由兜底约束类型，
# 减少同名电影/剧集识别错位）。自定义 RSS 地址无法判断类型，不兜底（None）。
DOUBAN_RANK_MEDIA_TYPES = {
    "movie-ustop": MediaType.MOVIE,
    "movie-weekly": MediaType.MOVIE,
    "movie-real-time": MediaType.MOVIE,
    "movie-hot-gaia": MediaType.MOVIE,
    "movie-top250": MediaType.MOVIE,
    "tv-hot": MediaType.TV,
    "show-domestic": MediaType.TV,
}

# 榜单选项的中文标签。
DOUBAN_RANK_LABELS = {
    "movie-ustop": "电影北美票房榜",
    "movie-weekly": "一周口碑电影榜",
    "movie-real-time": "实时热门电影",
    "show-domestic": "热门综艺",
    "movie-hot-gaia": "热门电影",
    "tv-hot": "热门电视剧",
    "movie-top250": "电影TOP250",
}

# HTTP 请求超时（秒），对齐参考插件。
_REQUEST_TIMEOUT = 240
# 年份正则：匹配 1900-2099 的四位独立数字。
_YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")
# 豆瓣ID正则：从详情链接提取数字段。
_DOUBAN_ID_PATTERN = re.compile(r"/(\d+)/")
# 榜单评分正则（热度/口碑类形态）：`评分：7.3分`（全/半角冒号、数字与“分”间可有空格）。
_VOTE_LABEL_PATTERN = re.compile(r"评分[：:]\s*(\d+(?:\.\d+)?)\s*分")
# 榜单评分正则（TOP250 形态）：只匹配「独占一个 <p> 的一位整数 + . + 一位小数」的裸数字，
# 这样既拿到 9.7，又不会误取 <img src> 里的长数字、海报尺寸或“评价数”。
_VOTE_BARE_PATTERN = re.compile(r"<p>\s*(\d\.\d)\s*</p>")
# 评分的合法区间（豆瓣 10 分制）。
_VOTE_MIN, _VOTE_MAX = 0.0, 10.0
# 每榜取前 N 的默认值（0 = 不限，向后兼容）。
_DEFAULT_LIMIT = 0


@register
class DoubanRankProvider(RankProvider):
    """豆瓣榜单来源：解析 RSS item 为标准化 ``RankMediaItem``。"""

    provider_id = "douban"
    provider_name = "豆瓣榜单"

    def get_spec(self) -> ProviderSpec:
        """返回本来源的元描述（选项与过滤器 schema）。"""
        rank_options = [
            {"title": DOUBAN_RANK_LABELS[key], "value": key}
            for key in DOUBAN_ADDRESS
        ]
        media_type_options = [
            {"title": "全部", "value": "all"},
            {"title": "电影", "value": "movie"},
            {"title": "电视剧", "value": "tv"},
        ]
        return ProviderSpec(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            default_cron="0 8 * * *",
            options_schema=[
                FieldSpec(
                    key="ranks",
                    label="热门榜单",
                    kind="multi-select",
                    default=["movie-hot-gaia", "tv-hot"],
                    options=rank_options,
                ),
                FieldSpec(
                    key="limit",
                    label="每榜取前N",
                    kind="number",
                    default=_DEFAULT_LIMIT,
                    hint="每个榜单只看前 N 条（TOP250 等长榜建议设小；0 = 不限）",
                ),
                FieldSpec(
                    key="rsshub_base",
                    label="RSSHub 地址",
                    kind="text",
                    default=DEFAULT_RSSHUB_BASE,
                    hint="内置榜单的 RSSHub 基址；rsshub.app 被墙/SNI 封锁时可改为自建实例（如 https://rsshub.你的域名）",
                    advanced=True,
                ),
                FieldSpec(
                    key="rss_addrs",
                    label="自定义RSS地址",
                    kind="textarea",
                    default="",
                    hint="每行一个完整 RSS 地址（覆盖上面的基址，可对接任意源）",
                    advanced=True,
                ),
                FieldSpec(
                    key="proxy",
                    label="使用代理服务器",
                    kind="switch",
                    default=False,
                ),
            ],
            filters_schema=[
                FieldSpec(
                    key="vote",
                    label="评分≥",
                    kind="float",
                    default=0,
                    hint="按榜单自带的豆瓣评分过滤（识别后判定）；暂无评分/0.0 视为不达标",
                ),
                FieldSpec(key="year", label="年份≥", kind="number", default=0),
                FieldSpec(
                    key="media_type",
                    label="媒体类型",
                    kind="select",
                    default="all",
                    options=media_type_options,
                ),
                # 排在最后：post 链上位于评分过滤之后（取前 N → 识别 → 评分过滤 → 本项）。
                FieldSpec(
                    key="season_exclude",
                    label="排除第2季及以后",
                    kind="switch",
                    default=False,
                    hint="开启后不订阅「第2季及以后」的剧集（按条目标题里的「第X季」判定，"
                         "电影与无季号条目不受影响）；默认关闭",
                ),
            ],
        )

    def has_listening(self, options: dict) -> bool:
        """选了任一内置榜单，或填了任一自定义 RSS 地址。"""
        options = options or {}
        ranks = [r for r in self._as_list(options.get("ranks")) if r in DOUBAN_ADDRESS]
        custom = [ln for ln in str(options.get("rss_addrs") or "").splitlines() if ln.strip()]
        return bool(ranks) or bool(custom)

    def fetch(self, options: dict, context: ProviderContext) -> Iterator[RankMediaItem]:
        """抓取并解析豆瓣 RSS，逐条产出 ``RankMediaItem``。

        每个地址（每个内置榜单、以及每行自定义 RSS 地址）各算一个榜，各取前 ``limit`` 条
        （``limit<=0`` / 非法值 = 不限）；顺序即榜单顺序，不排序。单条 item 解析失败内部
        try/except continue；请求/解析级异常向上抛出，由 runner 捕获。
        """
        options = options or {}
        ranks = self._as_list(options.get("ranks"))
        custom_addrs = [
            line.strip()
            for line in str(options.get("rss_addrs") or "").splitlines()
            if line.strip()
        ]
        proxy = bool(options.get("proxy"))
        limit = self._parse_limit(options.get("limit"))
        base = self._normalize_base(options.get("rsshub_base"))
        # (地址, 路由兜底类型)：自定义地址无从判断类型 -> None。
        addr_list = [(addr, None) for addr in custom_addrs] + [
            (f"{base}{DOUBAN_ADDRESS[rank]}", DOUBAN_RANK_MEDIA_TYPES.get(rank))
            for rank in ranks if rank in DOUBAN_ADDRESS
        ]
        if not addr_list:
            logger.warn(f"{self.provider_name}：未配置任何榜单地址")
            return
        for addr, route_type in addr_list:
            yield from self._fetch_addr(addr, proxy, limit, route_type)

    @staticmethod
    def _normalize_base(raw) -> str:
        """规整 RSSHub 基址：空则用默认，去尾部斜杠，无 scheme 时补 https://。"""
        base = str(raw or "").strip()
        if not base:
            return DEFAULT_RSSHUB_BASE
        base = base.rstrip("/")
        if not re.match(r"^https?://", base):
            base = f"https://{base}"
        return base

    def _safe_xml(self, text: str, addr: str) -> Optional[str]:
        """校验外部 RSS 文本可安全解析，不合格返回 None。

        RSS 地址可由用户自定义、也可能是被劫持的公共实例，即外部内容不可信。标准库
        的解析器不解析外部实体，但不限制内部实体展开：几 KB 的嵌套实体声明就能膨胀到
        撑爆内存，而插件与宿主同进程，炸的是整个 MoviePilot。榜单 RSS 用不到实体声明，
        因此见到就拒绝；同时对整体体积设限，挡住超大文档。

        :param text: 响应正文
        :param addr: 来源地址，仅用于日志
        :return: 可安全解析的文本，或 None
        """
        if not text:
            return None
        if len(text) > _MAX_RSS_BYTES:
            logger.warn(f"{self.provider_name}：RSS 文档超过 {_MAX_RSS_BYTES} 字节，跳过：{addr}")
            return None
        if _ENTITY_DECL.search(text):
            logger.warn(f"{self.provider_name}：RSS 文档含实体声明，出于安全考虑跳过：{addr}")
            return None
        return text

    def _fetch_addr(self, addr: str, proxy: bool, limit: int = 0,
                    route_type: MediaType | None = None) -> Iterator[RankMediaItem]:
        """抓取单个 RSS 地址并解析其中的 item（``limit>0`` 时只取榜单前 N 条）。"""
        proxies = settings.PROXY if proxy else None
        ret = RequestUtils(timeout=_REQUEST_TIMEOUT, proxies=proxies).get_res(addr)
        if not ret:
            logger.warn(f"{self.provider_name}：RSS 地址无返回，跳过：{addr}")
            return
        text = self._safe_xml(ret.text, addr)
        if text is None:
            return
        root = xml.dom.minidom.parseString(text).documentElement
        if root is None:
            return
        items = root.getElementsByTagName("item")
        logger.info(f"{self.provider_name}：{addr} 共 {len(items)} 条数据")
        if limit > 0:
            # RSS 顺序即榜单顺序（第 1 条=榜首），直接切前 N 条、不排序。
            items = items[:limit]
            logger.info(f"{self.provider_name}：{addr} 已按每榜取前 {limit} 条")
        for item in items:
            try:
                media_item = self._parse_item(item, route_type)
            except Exception as err:  # noqa: BLE001 - 单条解析失败不影响其余条目
                logger.error(f"{self.provider_name}：解析 RSS 条目失败：{err}")
                continue
            if media_item is not None:
                yield media_item

    def _parse_item(self, item, route_type: MediaType | None = None) -> RankMediaItem | None:
        """将单个 RSS item DOM 节点解析为 ``RankMediaItem``。

        ``route_type`` 为按榜单路由推断的媒体类型兜底：RSS 的 ``<type>`` 标签在所有榜单里
        都为空，故仅在标签解析不出结果时用它约束类型。
        """
        title = DomUtils.tag_value(item, "title", default="")
        link = DomUtils.tag_value(item, "link", default="")
        if not title and not link:
            return None

        douban_id = self._parse_douban_id(str(link or ""))
        year = self._parse_year(item)
        type_hint = self._parse_type(item) or route_type

        return RankMediaItem(
            title=str(title),
            year=year,
            type_hint=type_hint,
            douban_id=douban_id,
            media_source=MediaSource.Douban if douban_id else None,
            media_id=douban_id,
            poster=self._parse_poster(item),
            source_meta={"link": str(link)},
            unique_seed=f"{title}_{year}_(DB:{douban_id})",
            source_vote=self._parse_vote(item),
        )

    @staticmethod
    def _parse_douban_id(link: str) -> str | None:
        """从详情链接提取豆瓣ID（需为纯数字）。"""
        found = _DOUBAN_ID_PATTERN.findall(link)
        if found and str(found[0]).isdigit():
            return str(found[0])
        return None

    @staticmethod
    def _parse_year(item) -> str | None:
        """优先取 year 标签，缺失则从 description 中回退解析四位年份。"""
        year = DomUtils.tag_value(item, "year", default="")
        if year:
            return str(year)
        description = str(DomUtils.tag_value(item, "description", default="") or "")
        # 移除“评价数...”片段与 <img> 标签，避免误匹配其中的数字。
        description = re.sub(r"评价数.*?<br>", "", description)
        description = re.sub(r"<img.*?>", "", description)
        found_year = _YEAR_PATTERN.findall(description)
        return found_year[0] if found_year else None

    @staticmethod
    def _parse_vote(item) -> float | None:
        """从 description 解析榜单自带的豆瓣评分，解析不到或为 0.0 返回 None。

        两种形态：热度/口碑类 ``评分：7.3分``；TOP250 类裸数字 ``<p>9.7</p>``。只接受
        0.0~10.0，且 ``0.0``（RSS 的「暂无评分」）视为无评分。正则足够窄，不会把 <img src>
        里的长数字或“评价数”当评分。
        """
        description = str(DomUtils.tag_value(item, "description", default="") or "")
        if not description:
            return None
        match = _VOTE_LABEL_PATTERN.search(description) or _VOTE_BARE_PATTERN.search(description)
        if not match:
            return None
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            return None
        if not (_VOTE_MIN < value <= _VOTE_MAX):
            return None
        return value

    @staticmethod
    def _parse_limit(raw) -> int:
        """解析「每榜取前N」：非法值 / <=0 一律返回 0（= 不限，向后兼容）。"""
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    @staticmethod
    def _parse_type(item) -> MediaType | None:
        """解析类型标签：movie->MOVIE，其它非空->TV，空->None。"""
        type_str = str(DomUtils.tag_value(item, "type", default="") or "")
        if type_str == "movie":
            return MediaType.MOVIE
        if type_str:
            return MediaType.TV
        return None

    @staticmethod
    def _parse_poster(item) -> str | None:
        """尝试从 description 的 <img src=...> 提取海报地址（可选）。"""
        description = str(DomUtils.tag_value(item, "description", default="") or "")
        found = re.findall(r"<img[^>]+src=\"([^\"]+)\"", description)
        return found[0] if found else None

    @staticmethod
    def _as_list(value) -> List[str]:
        """把多选值统一成字符串列表（兼容逗号分隔字符串）。"""
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return []
