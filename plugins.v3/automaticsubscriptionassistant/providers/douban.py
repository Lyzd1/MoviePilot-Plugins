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
按条目标题里的「第X季/第X期/第X部（含第X部分）」（中文数字/阿拉伯数字均可）排除第 2 季
及以后的**剧集**；豆瓣不提供季号（``item.season`` 恒为 None），故只能从标题解析。口径与
Mikan 完全一致（两来源共用 ``core.filters.TitleSeasonExcludeFilter``），故「地狱老师
第2部分」「某某 第2期」这类按「部/期」记季的续作同样会被排除。电影、综艺与无季号条目
不受影响。

**扩量地区 / 其它扩量地区 / 扩量取数**（仅本来源有）：豆瓣榜单前几名多半是国产剧，常见配置是
「每榜取前 1」；但想额外多捞某些国家/地区的剧时，靠放大「每榜取前N」会连带收进一堆国产剧，
所以这里有独立的扩量通道。语义是**叠加式**的，默认行为一行不变：

- 每榜的**取数深度** = ``max(「每榜取前N」, 「扩量取数」)``；
- 排名 ≤ 「每榜取前N」的条目**照旧全部保留，不看国家/地区**；
- 排名在 「每榜取前N」+1 ~ 深度之间的条目，**只有命中「扩量地区」/「其它扩量地区」才保留**；
- 两个地区字段（多选 + 文本框）取**并集**；条目的出品国家/地区可能有多个
  （如 ``法国 日本 德国 比利时``），**命中其中任一个即算命中**；
- 三个字段留空/为 ``0``、或「每榜取前N」≤ 0（不限）时，**行为与没有这组配置时完全一致**；
- 解析不出地区（:meth:`DoubanRankProvider._parse_regions` 返回 None）的条目：浅位照旧保留，
  深位**不保留**（无法证明命中）；
- 判定在**抓取阶段**完成（取数深度只能在取数时决定），不走 ``core/filters.py`` 的过滤器框架。
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

# 豆瓣正文里的「信息行」：``<p>标签：2026 / 美国 / 剧情 悬疑 / 导演 / 主演</p>``（内置榜，带「标签：」前缀）
# 或 ``<p>2026 / 韩国 / 剧情 喜剧 / 朴丹熙 / 任时完 薛仁雅</p>``（列表榜，无前缀）。两种格式的
# **第 2 段都是出品国家/地区**，故只锚定「以年份开头 + 紧跟斜杠」，标题行与评分行不会命中。
_REGION_INFO_PATTERN = re.compile(r"<p>\s*(?:标签[：:]\s*)?((?:19|20)\d{2}\s*/[^<]*)</p>")
# 地区段内的多地区分隔符：空白 / 顿号 / 全半角逗号 / 全角斜杠（如「西班牙 法国」「德国,法国」）。
_REGION_SEPARATOR_PATTERN = re.compile(r"[\s,，、／]+")

# 「扩量地区」多选的预置选项：title 与 value 都用中文地区名，与榜单正文的写法保持一致
# （i18n 的 _country_title 只认 iso2，中文地区名会原样透传，故无需译文）。
DOUBAN_REGION_OPTIONS = [
    {"title": name, "value": name}
    for name in (
        "中国大陆", "中国香港", "中国澳门", "中国台湾",
        "美国", "日本", "韩国", "英国", "法国", "德国", "意大利", "西班牙", "葡萄牙",
        "俄罗斯", "乌克兰", "印度", "泰国", "新加坡", "马来西亚", "印度尼西亚", "菲律宾",
        "越南", "加拿大", "澳大利亚", "新西兰", "巴西", "墨西哥", "阿根廷", "智利",
        "哥伦比亚", "瑞典", "挪威", "丹麦", "芬兰", "冰岛", "荷兰", "比利时", "瑞士",
        "奥地利", "波兰", "捷克", "匈牙利", "希腊", "土耳其", "伊朗", "以色列", "埃及",
        "南非", "爱尔兰", "罗马尼亚",
    )
]


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
                    key="extra_regions",
                    label="扩量地区",
                    kind="multi-select",
                    default=[],
                    options=DOUBAN_REGION_OPTIONS,
                    hint="给「榜单前几名多半是国产剧、但想多捞其他国家/地区」用："
                         "排名在「每榜取前N」之内的条目照旧不看国家、全部保留，"
                         "只有更靠后的名次（见下面的「扩量取数」）里命中这些地区的条目才会被收进来。"
                         "与「其它扩量地区」取并集；条目的出品地区可能有多个，命中任一即可。"
                         "留空 = 不扩量，行为与现在完全一致。",
                ),
                FieldSpec(
                    key="extra_regions_text",
                    label="其它扩量地区",
                    kind="textarea",
                    default="",
                    hint="「扩量地区」里没有的国家/地区写这里，每行一个（也支持逗号、顿号分隔），"
                         "比如 中国大陆、中国香港、美国、韩国；写法要与榜单一致。"
                         "与上面的多选取并集，留空 = 不生效。",
                ),
                FieldSpec(
                    key="extra_limit",
                    label="扩量取数",
                    kind="number",
                    default=0,
                    hint="每榜实际向下多看几名：取数深度 = max(「每榜取前N」, 本项)。"
                         "排名 ≤「每榜取前N」的照旧全收（不看国家），"
                         "第「每榜取前N」+1 ~ 本项之间只收命中扩量地区的条目。"
                         "例：「每榜取前N」=1、本项=3、扩量地区=韩国 → 每榜前 1 条照旧全收，"
                         "第 2~3 条只收韩国剧。0 = 不扩量（默认）；"
                         "「每榜取前N」=0（不限）时本组配置不生效。",
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
                    hint="开启后不订阅「第2季及以后」的剧集（按条目标题里的"
                         "「第X季/第X期/第X部」判定，与 Mikan 口径一致；"
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

        配了「扩量地区」时取数深度放宽到 ``max(limit, extra_limit)``，其中浅位（前 ``limit``
        条）照旧全收、深位只收地区命中的条目（见模块 docstring）；未配置时行为与从前一致。
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
        extra_limit = self._parse_limit(options.get("extra_limit"))
        extra_regions = self._parse_extra_regions(options)
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
            yield from self._fetch_addr(addr, proxy, limit, route_type,
                                        extra_limit=extra_limit, extra_regions=extra_regions)

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
                    route_type: MediaType | None = None, extra_limit: int = 0,
                    extra_regions: Optional[set] = None) -> Iterator[RankMediaItem]:
        """抓取单个 RSS 地址并解析其中的 item。

        ``limit>0`` 时只取榜单前 N 条；配了 ``extra_regions``（且 ``limit>0``）时改为先解析
        前 ``max(limit, extra_limit)`` 条，再由 :meth:`_apply_extra_regions` 决定保留哪些
        ——浅位全收、深位只收地区命中的。未配置扩量时走原来的「切前 N 条」快路径，
        不解析多余条目、也不打新日志。
        """
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
        # ``limit<=0``（不限）时扩量无意义：既然全部都要，谈不上「多看几名」。
        if limit > 0 and extra_regions:
            depth = max(limit, extra_limit)
            # 地区只在解析后才有，故深位必须真解析出来再筛。
            parsed = list(self._iter_parsed(items[:depth], route_type))
            final = self._apply_extra_regions(parsed, limit, extra_limit, extra_regions)
            logger.info(f"{self.provider_name}：{addr} 已按每榜取前 {limit} 条")
            if depth > limit:
                # 浅位实际保留数按解析成功的条目算（个别条目解析失败时不会算出负数）。
                added = len(final) - len(parsed[:limit])
                logger.info(
                    f"{self.provider_name}：{addr} 扩量取数 {depth}："
                    f"排名 {limit + 1}~{depth} 中地区命中的 {added} 条已并入")
            yield from final
            return
        if limit > 0:
            # RSS 顺序即榜单顺序（第 1 条=榜首），直接切前 N 条、不排序。
            items = items[:limit]
            logger.info(f"{self.provider_name}：{addr} 已按每榜取前 {limit} 条")
        yield from self._iter_parsed(items, route_type)

    def _iter_parsed(self, items, route_type: MediaType | None = None) -> Iterator[RankMediaItem]:
        """逐条解析 RSS item：单条失败只记日志、不影响其余条目。"""
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
            regions=self._parse_regions(item),
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
    def _parse_regions(item) -> Optional[List[str]]:
        """从 description 的信息行解析出品国家/地区列表，解析不到返回 None。

        正文里的信息行有两种形态（**第 2 段恒为出品地区**）：

        - 内置榜：``<p>标签：2026 / 美国 / 剧情 悬疑 惊悚 犯罪 / 迈克尔·肖沃特 / 安妮·海瑟薇</p>``
        - 列表榜：``<p>2026 / 韩国 / 剧情 喜剧 爱情 / 朴丹熙 / 任时完 薛仁雅</p>``（无「标签：」前缀）

        正则锚定「``<p>`` 后紧跟年份 + 斜杠」，故标题行、裸评分行（``<p>8.6</p>``）都不会命中；
        与 :meth:`_parse_year` 一样先剔除 ``<img>`` 与「评价数…」片段，避免海报地址里的数字
        被当成信息行。地区可能有多个（空格分隔，如 ``法国 日本 德国 比利时``），段数不固定
        （综艺条目可能没有主演），故只按「第 2 段」取，不依赖总段数。
        """
        description = str(DomUtils.tag_value(item, "description", default="") or "")
        if not description:
            return None
        # 与 _parse_year 同一套清理：<img src> 里可能带年份与斜杠（海报地址），"评价数" 同理。
        description = re.sub(r"评价数.*?<br>", "", description)
        description = re.sub(r"<img.*?>", "", description)
        match = _REGION_INFO_PATTERN.search(description)
        if not match:
            return None
        segments = match.group(1).split("/")
        if len(segments) < 2:
            return None
        regions: List[str] = []
        for token in _REGION_SEPARATOR_PATTERN.split(segments[1]):
            region = token.strip()
            if region and region not in regions:  # 去空 + 去重且保序
                regions.append(region)
        return regions or None

    @staticmethod
    def _parse_extra_regions(options) -> set:
        """合并「扩量地区」（多选）与「其它扩量地区」（文本框）为一个地区集合。

        多选值可能被存成列表，也可能是逗号分隔的字符串；文本框每行一个地区，行内也支持
        逗号 / 顿号 / 全角斜杠分隔。两侧去空、去重后取并集；都没填时返回空集合
        （= 不扩量，行为与从前一致）。
        """
        options = options or {}
        parts: List[str] = []
        selected = options.get("extra_regions")
        if isinstance(selected, list):
            parts.extend(str(value) for value in selected)
        elif selected:
            parts.append(str(selected))
        text = str(options.get("extra_regions_text") or "")
        parts.extend(text.splitlines())

        regions = set()
        for part in parts:
            for token in _REGION_SEPARATOR_PATTERN.split(str(part)):
                region = token.strip()
                if region:
                    regions.add(region)
        return regions

    @staticmethod
    def _apply_extra_regions(items: List[RankMediaItem], default_limit: int,
                             extra_limit: int, extra_regions) -> List[RankMediaItem]:
        """按「扩量」语义从已解析条目里挑出最终要产出的那些（纯函数，不改入参对象）。

        - ``extra_regions`` 为空 ⇒ 等价未配置：``default_limit > 0`` 取前 N 条，否则原样返回；
        - ``default_limit <= 0``（不限）⇒ 原样返回全部，扩量配置不生效；
        - 否则取数深度 ``max(default_limit, extra_limit)``：前 ``default_limit`` 条无论产地
          一律保留，其后的条目只有 ``regions`` 与 ``extra_regions`` 有交集才保留
          （``regions`` 为 None = 无法证明命中，深位不保留）；返回顺序与输入一致。
        """
        items = list(items)
        if not extra_regions:
            return items[:default_limit] if default_limit > 0 else items
        if default_limit <= 0:
            return items
        depth = max(default_limit, extra_limit)
        kept = items[:default_limit]
        for item in items[default_limit:depth]:
            if set(item.regions or ()) & set(extra_regions):
                kept.append(item)
        return kept

    @staticmethod
    def _parse_limit(raw) -> int:
        """解析「每榜取前N」/「扩量取数」：非法值 / <=0 一律返回 0（= 不限 / 不扩量）。"""
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
