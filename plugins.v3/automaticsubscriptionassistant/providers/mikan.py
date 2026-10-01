"""Mikan(蜜柑计划) 季度新番来源：抓取蜜柑季度番剧列表，产出标准化条目。

数据来源为蜜柑计划（mikanani.me，备用 mikanime.tv）季度新番页面，
API 移植自 ``mikan_flutter`` 的 ``mikan.ts``。季度番剧列表页
``/Home/BangumiCoverFlowByDayOfWeek?year={year}&seasonStr={季}`` 返回按星期分组的
HTML（``div.sk-bangumi li``），每部番剧含 Mikan bangumi id、中文标题与封面。

详情页 ``/Home/Bangumi/{mikan_id}`` 的信息区实测为多个 ``p.bangumi-info``，
每条形如 ``key：value``（全角冒号），移植自 ``mikan.ts`` 的 ``parseBangumi``：实测
key 恒为 放送日期/放送开始/官方网站/Bangumi番组计划链接。据此一次详情请求解析出
``{bgm_id, year, air_date, original_title, aliases}``：

* ``bgm_id``：在 ``.bangumi-info`` 文本域内匹配 ``bgm.tv/bangumi.tv/subject/{id}``，
  缺该容器时回退整页匹配。
* ``year``：从「放送开始」值里正则抽 4 位真实放送年（覆盖配置/当前年）。
* ``air_date``：「放送开始」原值。
* ``original_title`` / ``aliases``：实测 Mikan 信息区并无「原名/别名」字段，故
  ``original_title`` 回退取详情页 ``p.bangumi-title`` 全名（通常比列表标题更完整），
  ``aliases`` 取「别名/又名」类 key（无则 ``[]``）。二者仅存入 ``source_meta``
  供历史展示/未来使用；**executor 识别仍只用主标题**（真正用别名做识别需改
  executor，属另一范畴，本次不动 executor）。

产出 ``bangumi_id``（bgm.tv subject id）时 executor 走宿主通用
``media_source=bangumi, media_id=...`` 识别；抓不到 bgm id 时退化为 title+year 名称识别。
封面 ``cover`` 同时落到 ``RankMediaItem.poster`` 与 ``source_meta``。
冷门番若 TMDB 名称匹配不到需注意（见 README 已知限制）。

**两阶段流程（默认开启）**：本来源默认「先评估整季、再产出入选者」，而不是逐条抓完就
订阅。顺序为：

1. 抓蜜柑当季列表（去重后的全部候选）；
2. 逐条取详情，拿到 ``bgm_id`` 与「放送开始」年；
3. **年份过滤**：首播年 < ``min_year``（0 = 跟随配置/当前年）→ 剔除（跨年老番如名侦探柯南
   1996 在这里出局，不必浪费后面的热度请求与识别请求）；
4. 取 **Bangumi 热度**（``collection.doing`` 在看人数 / ``rating.total`` 打分人数 /
   ``rating.score`` 评分），走宿主自带的 ``app.chain.bangumi.BangumiChain``（自带代理与
   缓存），本模块不新写 HTTP 客户端；取数失败的番剧本轮跳过；
5. **样本门槛**：在看人数 < ``min_doing`` 或打分人数 < ``min_votes`` → 剔除（避免刚开播、
   样本太少时评分不可信；下周样本涨上来会自动重新评估）；
6. 按 **在看人数降序** 排序，取前 ``top_n`` 部（0 = 不限）；
7. 只有入选的条目 yield 进既有管线（识别 → 「评分≥」按识别出的 Bangumi 评分过滤 →
   查重 → 订阅），**被粗筛剔除的番剧不产出、不记历史**。

即：整季粗筛在 provider 内完成，评分过滤仍复用现成的 ``VoteFilter``（读
``mediainfo.vote_average``，即识别到的 Bangumi 评分），不新增过滤器类，也不改动
executor/filters 的行为。

``resolve_bangumi_id`` 关闭时拿不到 bgm id、也就取不到热度：此时若配置了热度相关的
选项（``top_n``/``min_doing``/``min_votes``/``min_year`` 任一非 0），记一条 warn 日志
并退化为旧的「逐条抓详情 → 逐条产出整季」行为（不排序、不设门槛），不抛异常。
"""
from __future__ import annotations

import re
from datetime import datetime
from time import sleep
from typing import Iterator, List, Optional, Tuple
from urllib.parse import quote

from bs4 import BeautifulSoup

from app.schemas.types import MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

from ..core.models import FieldSpec, ProviderSpec, RankMediaItem
from ..core.provider import ProviderContext, RankProvider
from ..core.registry import register

# 蜜柑计划基址（主 + 备），逐个尝试。
MIKAN_URLS = ["https://mikanani.me", "https://mikanime.tv"]
# 蜜柑要求的 User-Agent。
MIKAN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36 MikanProject/1.0.0"
)

# 季度 seasonStr 真实取值（实测确认为中文季名，春/夏/秋/冬 均返回 HTTP 200）。
MIKAN_SEASONS = ["春", "夏", "秋", "冬"]
# “当前”自动项的哨兵值：fetch 时按当前月份推导实际季度。
SEASON_AUTO = "当前"

# HTTP 超时（秒）。
_REQUEST_TIMEOUT = 30
# 逐条抓详情时的礼貌间隔（秒），避免压站。
_DETAIL_SLEEP = 0.6
# 逐条取 Bangumi 热度时的礼貌间隔（秒）；宿主链自带缓存，命中缓存时同样短暂让出。
_HEAT_SLEEP = 0.2

# bgm.tv / bangumi.tv subject id 正则（详情页 .bangumi-info 内链接）。
_BGM_ID_PATTERN = re.compile(r"b(?:gm|angumi)\.tv/subject/(\d+)")
# 4 位年份正则（从「放送开始」等日期值里抽真实放送年）。
_YEAR_PATTERN = re.compile(r"(\d{4})")
# 详情页信息区里表示「放送开始日期」的 key（实测恒为此名，含真实放送年）。
_AIR_START_KEY = "放送开始"
# 用于回退扫描年份的日期类 key 关键词。
_DATE_KEY_HINTS = ("放送", "开播", "首播", "播出")
# 原名/译名类 key（实测 Mikan 无此字段，保留以增强健壮性/未来兼容）。
_ORIGINAL_TITLE_KEYS = ("原名", "日文名", "日语名", "罗马音", "译名")
# 别名类 key（实测 Mikan 无此字段，保留以增强健壮性/未来兼容）。
_ALIAS_KEYS = ("别名", "又名", "别称")
# 别名值的常见分隔符。
_ALIAS_SPLIT_PATTERN = re.compile(r"[、,，/／|｜]")


def _to_int(value) -> int:
    """安全转 int，失败返回 0。"""
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


class MikanApi:
    """蜜柑计划轻客户端：季度番剧列表 + 详情页信息（bgm id / 放送年 / 名称）提取。"""

    def __init__(self, proxies: Optional[dict] = None) -> None:
        """``proxies`` 由调用方（``MikanRankProvider``）传入，本实例请求全程携带。"""
        self._proxies = proxies

    def _get(self, path: str) -> Optional[Tuple[str, str]]:
        """按主/备基址依次 GET ``path``，返回 ``(HTML 文本, 命中的基址)``。

        命中的基址随文本一并返回，供解析相对封面 URL 时拼对站点（走备用站也拼备用站）；
        全部基址失败返回 None。
        """
        last_err: Optional[Exception] = None
        for base in MIKAN_URLS:
            url = f"{base}{path}"
            try:
                ret = RequestUtils(ua=MIKAN_UA, timeout=_REQUEST_TIMEOUT,
                                   proxies=self._proxies).get_res(url)
            except Exception as err:  # noqa: BLE001 - 单个基址失败则尝试备用
                last_err = err
                continue
            if ret is not None and getattr(ret, "text", None):
                return ret.text, base
        if last_err is not None:
            logger.warn(f"Mikan 请求失败：{path}：{last_err}")
        return None

    def season(self, year, season_str: str) -> List[dict]:
        """GET 季度新番列表并解析，产出 ``[{mikan_id, title, cover, week}]``。"""
        path = (
            f"/Home/BangumiCoverFlowByDayOfWeek"
            f"?year={year}&seasonStr={quote(str(season_str))}"
        )
        ret = self._get(path)
        if not ret:
            return []
        html, base = ret
        return self._parse_season(html, base)

    def bangumi_detail(self, mikan_id: str) -> dict:
        """GET 详情页，一次解析 ``{bgm_id, year, air_date, original_title, aliases}``。

        详情页缺失（全部基址失败）返回 ``{}``；各字段解析不到为 None/``[]``。
        """
        ret = self._get(f"/Home/Bangumi/{mikan_id}")
        if not ret:
            return {}
        html, _base = ret
        return self._parse_detail(html)

    @staticmethod
    def _parse_season(html: str, base: str) -> List[dict]:
        """解析季度页 HTML：``div.sk-bangumi`` 内按星期分组的 ``li`` 番剧项。

        相对封面按实际命中的 ``base`` 拼绝对 URL（走备用站也拼备用站）。
        """
        soup = BeautifulSoup(html, "lxml")
        results: List[dict] = []
        seen: set = set()
        for group in soup.select("div.sk-bangumi"):
            row = group.select_one("div.row")
            week = row.get_text(strip=True) if row else str(group.get("data-dayofweek") or "")
            for li in group.select("li"):
                span = li.select_one("span[data-bangumiid]")
                if span is None:
                    continue
                mikan_id = str(span.get("data-bangumiid") or "").strip()
                if not mikan_id or mikan_id in seen:
                    continue
                anchor = li.select_one("a.an-text")
                title = ""
                if anchor is not None:
                    title = str(anchor.get("title") or anchor.get_text(strip=True) or "").strip()
                if not title:
                    continue
                seen.add(mikan_id)
                cover = str(span.get("data-src") or "").strip()
                if cover.startswith("/"):
                    cover = f"{base}{cover}"
                results.append(
                    {"mikan_id": mikan_id, "title": title, "cover": cover, "week": week}
                )
        return results

    @classmethod
    def _parse_detail(cls, html: str) -> dict:
        """解析详情页信息区，返回 ``{bgm_id, year, air_date, original_title, aliases}``。

        实测信息区为多个 ``p.bangumi-info``，每条 ``key：value``（全角冒号），逐条
        partition 成 ``more`` 字典（移植自 ``mikan.ts`` parseBangumi 的 more）：
        bgm id 在信息区文本内匹配（缺容器回退整页）；真实放送年从「放送开始」抽 4 位；
        原名/别名走可选 key（无则回退 ``p.bangumi-title`` / ``[]``）。全部字段可选。
        """
        soup = BeautifulSoup(html, "lxml")
        nodes = soup.select("p.bangumi-info") or soup.select(".bangumi-info")

        # 逐条 key：value（全角冒号）解析为字典。
        more: dict = {}
        for node in nodes:
            text = node.get_text(" ", strip=True)
            if "：" not in text:
                continue
            key, _sep, value = text.partition("：")
            key, value = key.strip(), value.strip()
            if key and value:
                more[key] = value

        # bgm/bangumi.tv subject id：优先 .bangumi-info 文本域，缺该容器时回退整页。
        if nodes:
            search_text = "\n".join(n.get_text(" ", strip=True) for n in nodes)
        else:
            search_text = html
        bgm_id: Optional[int] = None
        match = _BGM_ID_PATTERN.search(search_text)
        if match:
            bgm_id = _to_int(match.group(1)) or None

        air_date = more.get(_AIR_START_KEY) or None
        return {
            "bgm_id": bgm_id,
            "year": cls._extract_year(more, air_date),
            "air_date": air_date,
            "original_title": cls._extract_original_title(soup, more),
            "aliases": cls._extract_aliases(more),
        }

    @staticmethod
    def _extract_year(more: dict, air_date: Optional[str]) -> Optional[str]:
        """从「放送开始」等日期类值里抽 4 位真实放送年（1900-2100 合法域）。"""
        candidates: List[str] = []
        if air_date:
            candidates.append(air_date)
        for key, value in more.items():
            if value and any(hint in key for hint in _DATE_KEY_HINTS):
                candidates.append(value)
        for candidate in candidates:
            ym = _YEAR_PATTERN.search(candidate)
            if ym and 1900 <= int(ym.group(1)) <= 2100:
                return ym.group(1)
        return None

    @staticmethod
    def _extract_original_title(soup: BeautifulSoup, more: dict) -> Optional[str]:
        """原名：优先原名类 key，回退详情页 ``p.bangumi-title`` 全名；无则 None。"""
        for key in _ORIGINAL_TITLE_KEYS:
            value = more.get(key)
            if value:
                return value
        title_node = soup.select_one("p.bangumi-title")
        if title_node is not None:
            text = title_node.get_text(strip=True)
            if text:
                return text
        return None

    @staticmethod
    def _extract_aliases(more: dict) -> List[str]:
        """别名：取别名类 key 的值按常见分隔符切分；无则 ``[]``。"""
        for key in _ALIAS_KEYS:
            value = more.get(key)
            if value:
                return [a.strip() for a in _ALIAS_SPLIT_PATTERN.split(value) if a.strip()]
        return []


@register
class MikanRankProvider(RankProvider):
    """Mikan 季度新番来源：解析蜜柑季度番剧列表为标准化 ``RankMediaItem``。

    ``resolve_bangumi_id`` 为 True 时逐条抓详情，一次请求拿齐 bgm subject id +
    真实放送年 + 原名/别名：产出 ``bangumi_id`` 时 executor 走宿主通用媒体身份识别，
    抓不到 bgm id 时退化为 title+year 名称识别；
    真实放送年（解析到才）覆盖配置/当前年；``original_title``/``aliases`` 仅存
    ``source_meta``（executor 识别仍用主标题，未接入别名识别）。封面 ``cover`` 同时落到
    ``poster`` 与 ``source_meta``。番剧统一按 ``MediaType.TV`` 处理。

    默认走「两阶段」流程（详见模块 docstring）：先聚合整季候选 + 热度，按
    「年份下限 → 在看/打分门槛 → 在看人数降序取前 N」粗筛，再 yield 入选条目；只有入选
    的前 N 部会进入识别/评分过滤/订阅并记历史。热度取自宿主 ``BangumiChain``。
    ``resolve_bangumi_id`` 关闭时无 bgm id、取不到热度，自动退化为旧的逐条产出行为。
    """

    provider_id = "mikan"
    provider_name = "Mikan 季度新番"

    def get_spec(self) -> ProviderSpec:
        """返回本来源的元描述（选项与过滤器 schema）。"""
        season_options = [{"title": "当前季度（自动）", "value": SEASON_AUTO}] + [
            {"title": f"{s}季", "value": s} for s in MIKAN_SEASONS
        ]
        return ProviderSpec(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            # 季番每周更新，默认每周一早上抓一次。
            default_cron="0 10 * * 1",
            options_schema=[
                FieldSpec(key="year", label="年份(0=当前年)", kind="number", default=0),
                FieldSpec(
                    key="season",
                    label="季度",
                    kind="select",
                    default=SEASON_AUTO,
                    options=season_options,
                ),
                FieldSpec(
                    key="resolve_bangumi_id",
                    label="抓详情补 Bangumi ID/放送年(更准但更慢)",
                    kind="switch",
                    default=True,
                ),
                FieldSpec(
                    key="min_year",
                    label="首播年份下限(0=跟随目标年)",
                    kind="number",
                    default=0,
                    hint="首播年份早于该值的番剧直接跳过，用于排除名侦探柯南这类跨年老番；"
                         "0=自动跟随上方填的年份（抓 2026 夏即 2026）",
                ),
                FieldSpec(
                    key="min_doing",
                    label="最少在看人数(0=不限)",
                    kind="number",
                    default=0,
                    hint="在看人数不足的番剧本周跳过，下周人数涨上来会自动重新评估",
                ),
                FieldSpec(
                    key="min_votes",
                    label="最少打分人数(0=不限)",
                    kind="number",
                    default=0,
                    hint="打分人数不足的番剧本周跳过，避免刚开播、样本太少导致评分不可信",
                ),
                FieldSpec(
                    key="top_n",
                    label="按热度取前N部(0=不限)",
                    kind="number",
                    default=10,
                    hint="按 Bangumi 在看人数降序排序后只取前 N 部进入订阅；0=不限",
                ),
                FieldSpec(key="proxy", label="使用代理访问", kind="switch", default=False),
            ],
            filters_schema=[
                FieldSpec(key="year", label="年份≥", kind="number", default=0),
                FieldSpec(
                    key="vote",
                    label="评分≥",
                    kind="float",
                    default=0,
                    hint="按识别后的 Bangumi 评分过滤；识别失败的条目不会进入评分判定",
                ),
            ],
        )

    def fetch(self, options: dict, context: ProviderContext) -> Iterator[RankMediaItem]:
        """抓取蜜柑季度番剧列表，产出 ``RankMediaItem``（默认走两阶段粗筛）。

        两阶段（``resolve_bangumi_id`` 且需粗筛时）：先聚合整季候选——逐条抓详情补
        bgm subject id + 真实放送年，按 ``min_year`` 剔除跨年老番，逐条取 Bangumi 热度
        并按 ``min_doing``/``min_votes`` 门槛剔除，再按在看人数降序取前 ``top_n``（0=不限），
        最后只 yield 入选条目；被剔除者不产出、不记历史。每条详情/热度之间短暂 sleep
        避免压站，两个阶段都响应 ``context.event`` 退出信号。

        ``resolve_bangumi_id`` 为 False（无 bgm id、取不到热度）而配置了热度相关选项时，
        记一条 warn 并退化为旧的逐条产出行为；热度相关选项全为 0 时同样走旧行为。
        单条失败 try/except continue，整源抓取失败向上抛出（由 runner 捕获）。
        """
        options = options or {}
        year = self._resolve_year(options.get("year"))
        season_str = self._resolve_season(options.get("season"))
        resolve_bgm = bool(options.get("resolve_bangumi_id", True))
        # 可选代理：开启则本次抓取的 HTTP 请求（季度列表 + 详情）走系统代理。
        proxies = settings.PROXY if bool(options.get("proxy")) else None

        min_year = self._resolve_min_year(options.get("min_year"), year)
        min_doing = _to_int(options.get("min_doing"))
        min_votes = _to_int(options.get("min_votes"))
        top_n = _to_int(options.get("top_n"))
        # 是否需要「先聚合整季再产出」：年份过滤要先把整季详情看完才能统计剔除，
        # 热度排序/门槛还要先把热度取齐；任一开启即走两阶段。
        # ``min_year`` 为 0 时解析成目标年（> 0），故默认配置恒走两阶段粗筛。
        need_heat = (top_n > 0) or (min_doing > 0) or (min_votes > 0) or (min_year > 0)

        api = MikanApi(proxies=proxies)
        entries = api.season(year, season_str)
        logger.info(
            f"{self.provider_name}：{year} 年 {season_str} 季 共 {len(entries)} 部番剧"
        )
        config_year = str(year)

        if not resolve_bgm or not need_heat:
            if not resolve_bgm and need_heat:
                logger.warn(
                    f"{self.provider_name}：已关闭「抓详情补 Bangumi ID/放送年」，"
                    f"取不到 Bangumi 热度，本次退化为逐条订阅整季"
                    f"（不排序、不设在看/打分/年份门槛）"
                )
            # 旧行为：逐条抓详情（仅 resolve_bgm 时）→ 逐条产出。
            for entry in entries:
                if self._stopped(context):
                    break
                try:
                    detail: dict = {}
                    if resolve_bgm:
                        detail = self._safe_detail(api, entry)
                        sleep(_DETAIL_SLEEP)
                    yield self._build_item(entry, config_year, detail)
                except Exception as err:  # noqa: BLE001 - 单条兜底，不影响其余番剧
                    logger.error(f"{self.provider_name}：解析番剧条目失败：{err}")
                    continue
            return

        # ---- 阶段一：聚合整季候选并粗筛（详情 → 年份 → 热度 → 门槛 → 排序 → 取前 N）----
        candidates: List[Tuple[dict, dict, dict]] = []
        year_dropped = heat_failed = doing_short = votes_short = 0
        for entry in entries:
            if self._stopped(context):
                break
            try:
                detail = self._safe_detail(api, entry)
                sleep(_DETAIL_SLEEP)
                # 年份过滤：年份解析不出来按「未知」处理，不因年份剔除（它同时也没有
                # bgm id，会在下一步取热度时自然出局）。
                detail_year = _to_int(detail.get("year"))
                if detail_year and detail_year < min_year:
                    year_dropped += 1
                    continue
                heat = self._safe_heat(detail.get("bgm_id")) if detail.get("bgm_id") else None
                sleep(_HEAT_SLEEP)
                if heat is None:
                    heat_failed += 1
                    continue
                if min_doing > 0 and _to_int(heat.get("doing")) < min_doing:
                    doing_short += 1
                    continue
                if min_votes > 0 and _to_int(heat.get("votes")) < min_votes:
                    votes_short += 1
                    continue
                candidates.append((entry, detail, heat))
            except Exception as err:  # noqa: BLE001 - 单条兜底，不影响其余番剧
                logger.error(f"{self.provider_name}：解析番剧条目失败：{err}")
                continue

        # 按在看人数降序（稳定排序：同为 0 或相同人数时保持季度列表原序）。
        candidates.sort(key=lambda item: _to_int(item[2].get("doing")), reverse=True)
        selected = candidates[:top_n] if top_n > 0 else candidates
        logger.info(
            f"Mikan 季度新番：{year} {season_str} 共 {len(entries)} 部；"
            f"年份过滤剔除 {year_dropped}；热度取数失败 {heat_failed}；"
            f"在看不足 {doing_short}；打分不足 {votes_short}；"
            f"入选 {len(selected)}/{len(candidates)}"
        )

        # ---- 阶段二：只产出入选条目（进入识别 → 评分过滤 → 查重 → 订阅）----
        for entry, detail, heat in selected:
            if self._stopped(context):
                break
            try:
                yield self._build_item(entry, config_year, detail, heat)
            except Exception as err:  # noqa: BLE001 - 单条兜底，不影响其余番剧
                logger.error(f"{self.provider_name}：解析番剧条目失败：{err}")
                continue

    @staticmethod
    def _stopped(context) -> bool:
        """退出信号是否已置位（``context``/``event`` 允许缺省，便于测试直接调用）。"""
        event = getattr(context, "event", None) if context is not None else None
        return event is not None and event.is_set()

    def _safe_heat(self, bgm_id) -> Optional[dict]:
        """取单部番的 Bangumi 热度：``{'doing': int, 'votes': int, 'score': float|None}``。

        走宿主自带的 ``BangumiChain``（自带代理与缓存），本模块不新写 HTTP 客户端；
        延迟 import 便于单元测试注入桩。任何异常（含 import 失败、id 非法）都只记
        warn 并返回 ``None``，由调用方按「热度取数失败」跳过该番。
        """
        try:
            from app.chain.bangumi import BangumiChain

            info = BangumiChain().bangumi_info(int(bgm_id))
        except Exception as err:  # noqa: BLE001 - 单条热度失败不影响主流程
            logger.warn(f"{self.provider_name}：获取 Bangumi 热度失败（bgm {bgm_id}）：{err}")
            return None
        if not info:
            logger.warn(f"{self.provider_name}：获取 Bangumi 热度为空（bgm {bgm_id}）")
            return None
        collection = info.get("collection") or {}
        rating = info.get("rating") or {}
        score = rating.get("score")
        return {
            "doing": _to_int(collection.get("doing")),
            "votes": _to_int(rating.get("total")),
            "score": float(score) if score else None,
        }

    def _safe_detail(self, api: "MikanApi", entry: dict) -> dict:
        """抓详情补 bgm id + 放送年 + 名称，失败仅告警并返回 ``{}``（退化名称识别）。"""
        try:
            return api.bangumi_detail(entry["mikan_id"])
        except Exception as err:  # noqa: BLE001 - 单条详情失败不影响主流程
            logger.warn(
                f"{self.provider_name}：抓取详情失败"
                f"（{entry.get('title')}）：{err}"
            )
            return {}

    @staticmethod
    def _build_item(entry: dict, config_year: str, detail: dict,
                    heat: Optional[dict] = None) -> RankMediaItem:
        """把单部番剧 dict + 详情 dict（+ 热度 dict）构造为 ``RankMediaItem``。

        真实放送年（``detail['year']``，解析到才）覆盖配置/当前年；封面同时落到
        ``poster`` 与 ``source_meta``；``original_title``/``aliases``/``air_date``
        存入 ``source_meta``（供历史展示/未来用，识别仍用主标题）。
        ``heat`` 为 ``_safe_heat`` 的结果（缺省 None，兼容未取热度的旧调用路径），
        其 ``doing``/``votes``/``score`` 一并存入 ``source_meta`` 供展示与排查。
        """
        detail = detail or {}
        heat = heat or {}
        cover = entry.get("cover")
        year = detail.get("year") or config_year
        return RankMediaItem(
            title=entry["title"],
            year=year,
            type_hint=MediaType.TV,
            bangumi_id=detail.get("bgm_id"),
            media_source=MediaSource.Bangumi if detail.get("bgm_id") else None,
            media_id=str(detail["bgm_id"]) if detail.get("bgm_id") else None,
            poster=cover,
            source_meta={
                "mikan_id": entry["mikan_id"],
                "week": entry.get("week"),
                "cover": cover,
                "original_title": detail.get("original_title"),
                "aliases": detail.get("aliases") or [],
                "air_date": detail.get("air_date"),
                # Bangumi 热度（未取热度/取数失败时为 None）。
                "doing": heat.get("doing"),
                "votes": heat.get("votes"),
                "score": heat.get("score"),
            },
            unique_seed=entry["mikan_id"],
        )

    @staticmethod
    def _resolve_min_year(raw, target_year: int) -> int:
        """解析首播年份下限：``0``（跟随目标年）/非法 -> 本次抓取的目标年。"""
        value = _to_int(raw)
        if value <= 0:
            return _to_int(target_year)
        return value

    @staticmethod
    def _resolve_year(raw) -> int:
        """解析年份：``0`` 或非法 -> 当前年。"""
        year = _to_int(raw)
        if year <= 0:
            return datetime.now().year
        return year

    @classmethod
    def _resolve_season(cls, raw) -> str:
        """解析季度：实测季名直用，``当前``/未知 -> 按当前月推导。"""
        value = str(raw or "").strip()
        if value in MIKAN_SEASONS:
            return value
        return cls._season_by_month(datetime.now().month)

    @staticmethod
    def _season_by_month(month: int) -> str:
        """按月份推导季度：1-3->冬，4-6->春，7-9->夏，10-12->秋。"""
        if month in (1, 2, 3):
            return "冬"
        if month in (4, 5, 6):
            return "春"
        if month in (7, 8, 9):
            return "夏"
        return "秋"
