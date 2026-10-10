"""歌手作品订阅（MusicArtistSubscribe）——MoviePilot v3 单文件插件。

按用户配置的歌手名单，定期检查歌手在 MusicBrainz 上的新发行物
（专辑 / EP / 单曲），对落在「追新窗口」内的作品
自动创建 MoviePilot 订阅。只追新发行，不回补历史。

以下事实均已在宿主 moviepilot-v3 上核实，实现严格按此口径：

1. 搜歌手用 ``MediaChain().search_persons``（同步），返回 ``list[MusicArtistInfo]``。
   宿主对搜索结果做了「整名命中优先」的排序保护，精确同名者排在第一位。
2. 作品目录只有异步接口 ``MediaChain().async_get_music_artist_albums``；
   ``page`` 从 1 起、``count`` 上限 100。宿主只在「页内」按发行日期倒序
   （MusicBrainz 浏览接口本身不支持排序），因此高产歌手的新作不一定都在首页，
   本插件对每个类型最多翻 ``MAX_PAGES`` 页、每页 ``PAGE_SIZE`` 条来兜底。
3. 音乐条目由宿主统一表示为「专辑」实体：``music_type`` 恒为 ``album``，
   ``album_type`` 为首字母大写的主类型（``Album`` / ``Single`` / ``EP`` …），
   副类型（``Soundtrack`` / ``Live`` / ``Compilation`` …）另放在 ``secondary_types``；
   详情识别才有 ``total_tracks``，列表接口恒为 None。
4. ``MediaChain().media_exists`` 用于音乐时要先把 ``total_tracks`` 补成非空，
   宿主自身（``/api/endpoints/music.py`` 的 ``/library/status``）就是这么做的：
   无曲数时一首匹配曲目即足以判定「已入库」。本插件复制条目后再补 1，不污染原对象。
5. ``SubscribeChain().add`` 只接受 ``recording`` / ``album`` 两种实体，不支持直接
   订阅「艺术家」，所以 release-group 统一按 ``music_type="album"`` 订阅；宿主会在
   内部重新识别并校验，未发行的 release-group 可能因取不到曲目数而报
   「专辑总曲目数未知，无法累计专辑下载进度」——这属于正常情况，记日志跳过即可。

本插件在进程内运行，定时任务跑在 APScheduler 的后台线程里（没有运行中的事件循环），
因此可以直接用 ``asyncio.run(...)`` 调用宿主的异步作品目录接口。

@author: Lyzd1
"""

import asyncio
import copy
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.plugins import _PluginBase
from app.schemas.types import MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.logging import logger

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

# 每种订阅类型最多翻的页数（应对高产歌手新作不在首页）
MAX_PAGES = 3
# 每页条数，宿主上限为 100
PAGE_SIZE = 100

# 歌手名单的分隔符：换行、半角/全角逗号、中文顿号、半角/全角分号
ARTIST_SEPARATORS = r"[\r\n,，、;；]+"

# 历史条目状态
STATUS_SUBSCRIBED = "subscribed"
STATUS_DRY_RUN = "dry_run"
STATUS_FAILED = "failed"

# 状态 -> 中文标签
#
# 「预演」二字已足够区分（预演不会真正建订阅），不再缀「（未订阅）」——
# 窄屏下记录行本来就紧张，短标签能少一次折行。
STATUS_LABELS: Dict[str, str] = {
    STATUS_SUBSCRIBED: "已订阅",
    STATUS_DRY_RUN: "预演",
    STATUS_FAILED: "订阅失败",
}

# 详情页展示艺术家ID 时保留的前缀长度，超出部分用「…」省略。
# 完整 ID 仍留在链接地址（href 指向 MusicBrainz 详情页）里，需要时可复制。
ARTIST_ID_DISPLAY_LEN = 8

# 订阅展示里封面缩略图的边长（像素）。
# 专辑封面是正方形，比演员作品订阅插件那张 2/3 的竖版海报小一号：
# 一张专辑封面按 80×120 铺开会把单行撑得过高，一屏看不了几条。
COVER_SIZE = 60

# 封面缺失时的占位图标（灰底块 + 音乐图标），避免出现空白或裂图感
COVER_PLACEHOLDER_ICON = "mdi-music"

# 订阅类型多选（值需与 MusicBrainz 浏览接口的 type 参数一致）
#
# 只保留主类型三项，与宿主前端艺术家页（mpfront 的 resources.vue 按
# album / ep / single 拉取）保持一致。「原声带 / 现场 / 合辑」在 MusicBrainz 里
# 是**附属标签（secondary type）而非并列类型**：按 soundtrack 检索返回的条目，
# 其主类型仍是 EP / Single（实测 5 条全是），勾选 ep 时照样命中；compilation
# 返回的条目主类型全是 Album。它们与前三项重复，故移除。
ALBUM_TYPE_OPTIONS: List[Dict[str, str]] = [
    {"title": "专辑", "value": "album"},
    {"title": "EP", "value": "ep"},
    {"title": "单曲", "value": "single"},
]

# 歌手名单输入提示
_ARTISTS_HINT = (
    "一行一个歌手；也支持用「歌手名@MusicBrainz艺术家ID」锁定。\n"
    "换行、逗号、顿号、分号都能分隔；名称里含英文逗号时请用「名字@ID」写法。\n"
    "艺术家ID 获取方式：打开 musicbrainz.org 搜索歌手，详情页地址栏 "
    "artist/ 后面那一串 UUID 即是；或先只填歌手名跑一次，详情页会显示解析结果，"
    "确认取错时再用「名字@ID」锁定。\n"
    "插件会记住每位歌手的解析结果，配置不变时后续运行直接沿用（不再重复搜索）；"
    "改名字或补上「@ID」即重新识别。\n"
)

# 参数说明
_FILTER_HINT = (
    "追新窗口 = [今天 - 最近N天 , 今天 + 提前N天]。\n"
    "• 已发行的作品：只订最近 N 天内发行的；N=0 表示不订已发行的。\n"
    "• 尚未发行的作品：在发行前 N 天内开始订阅；N=0 表示不提前、等发行后再订。\n"
    "首次启用建议保持默认，避免把歌手的全部历史专辑一次性订完。"
)

# 「最近运行」等运行时数据的存储键
KEY_HISTORY = "history"
KEY_HANDLED = "already_handle"
KEY_ARTISTS_RESOLVED = "artists_resolved"
KEY_LAST_RUN = "last_run"
# 歌手解析缓存：{缓存键: 解析记录}，配置文本没变就直接沿用，不再重复搜索/拉详情
KEY_ARTISTS_CACHE = "artists_cache"


# --------------------------------------------------------------------------- #
# 纯函数：与宿主运行时无关，可脱离插件实例单独校验
# --------------------------------------------------------------------------- #

def field_of(item: Any, name: str, default: Any = None) -> Any:
    """读取条目的字段，同时兼容字典与宿主返回的 dataclass 对象。"""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def normalize_name(text: Any) -> str:
    """歌手名规范化：忽略大小写与全部空白字符，用于精确同名判定。"""
    return "".join(str(text or "").split()).lower()


def artist_entry_key(display_name: Any, pinned_id: Any) -> str:
    """
    构造一位歌手的缓存键：规范化后的名字 + ``@`` + 锁定的艺术家ID（小写、去空白）。

    键完全由「配置里那一条文本」决定，所以配置文本一变（改名字、补上或去掉 ``@ID``）
    键就跟着变，旧缓存自然命中不上——等同于该歌手重新识别，无需额外的开关。

    :param display_name: 配置里写的歌手名
    :param pinned_id: 配置里锁定的 MusicBrainz 艺术家 ID，没锁定时传 None 或空串
    :return: 该配置条目的稳定缓存键
    """
    return f"{normalize_name(display_name)}@{str(pinned_id or '').strip().lower()}"


def parse_artist_entries(raw: Any) -> List[Tuple[str, Optional[str]]]:
    """
    解析歌手名单文本。

    支持两种写法：``歌手名`` 与 ``歌手名@MusicBrainz艺术家ID``。
    换行、半角/全角逗号、中文顿号、分号都能作为分隔符，空行与重复项自动忽略。

    :param raw: 用户在配置里填写的多行文本
    :return: ``[(歌手名, 锁定的艺术家ID 或 None), ...]``
    """
    entries: List[Tuple[str, Optional[str]]] = []
    seen: Set[Tuple[str, str]] = set()
    for chunk in re.split(ARTIST_SEPARATORS, str(raw or "")):
        text = chunk.strip()
        if not text:
            continue
        name, _, pinned = text.partition("@")
        name = name.strip()
        pinned = pinned.strip() or None
        # 只填了「@ID」时用 ID 充当展示名，日志与详情页仍可读
        if not name and pinned:
            name = pinned
        if not name:
            continue
        dedupe_key = (normalize_name(name), (pinned or "").lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entries.append((name, pinned))
    return entries


def pick_artist_candidate(display_name: Any, candidates: Sequence[Any]) -> Dict[str, Any]:
    """
    在搜索结果里挑出要采用的艺术家候选。

    判定规则：把配置名与候选的 ``name`` / ``aliases`` 都做规范化（忽略大小写与空格）
    后比较，得到「精确同名候选」集合。

    - 恰好 1 个：直接采用；
    - 多个：采用第一个并提示可用「名字@ID」锁定；
    - 0 个：采用搜索结果的第一位，并提示无精确同名匹配。

    :param display_name: 配置里填写的歌手名
    :param candidates: ``search_persons`` 返回的候选列表
    :return: ``{"chosen", "exact_match", "candidate_count", "exact_count", "note", "multiple"}``
    """
    usable = [
        item for item in (candidates or [])
        if str(field_of(item, "media_id") or "").strip()
    ]
    if not usable:
        return {
            "chosen": None,
            "exact_match": False,
            "candidate_count": 0,
            "exact_count": 0,
            "note": "搜索无结果，已跳过",
            "multiple": False,
        }

    wanted = normalize_name(display_name)
    exact = [
        item for item in usable
        if any(
            normalize_name(alias) == wanted
            for alias in [field_of(item, "name"), *(field_of(item, "aliases") or [])]
        )
    ]

    if len(exact) == 1:
        return {
            "chosen": exact[0],
            "exact_match": True,
            "candidate_count": len(usable),
            "exact_count": 1,
            "note": "精确同名唯一命中",
            "multiple": False,
        }
    if len(exact) > 1:
        return {
            "chosen": exact[0],
            "exact_match": True,
            "candidate_count": len(usable),
            "exact_count": len(exact),
            "note": (
                f"存在 {len(exact)} 个同名候选，已取第一个，"
                f"如订错请改用「{display_name}@艺术家ID」锁定"
            ),
            "multiple": True,
        }
    return {
        "chosen": usable[0],
        "exact_match": False,
        "candidate_count": len(usable),
        "exact_count": 0,
        "note": "无精确同名匹配，已采用搜索首位的候选，如订错请改用「名字@艺术家ID」锁定",
        "multiple": False,
    }


def parse_release_date(value: Any) -> Optional[date]:
    """
    解析发行日期，支持 ``YYYY-MM-DD`` 与 ``YYYY-MM``（取当月 1 日）。

    宿主返回的 ``release_date`` 可能是 None、空串或只有年份；这些都按「无法解析」
    处理，由调用方跳过——宁可不订，也不用臆测的日期误订。

    :param value: 原始发行日期文本
    :return: 解析出的日期，无法解析时返回 None
    """
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def in_release_window(
        release: Optional[date],
        today: date,
        recent_days: int,
        preorder_days: int,
) -> bool:
    """
    判断发行日期是否落在 ``[今天 - recent_days, 今天 + preorder_days]`` 窗口内。

    - 尚未发行（发行日在今天之后）：要求 ``preorder_days > 0`` 且距今不超过它；
      ``preorder_days=0`` 表示不提前订阅。
    - 已发行（含今天）：要求 ``recent_days > 0`` 且距今不超过它；
      ``recent_days=0`` 表示不订已发行的作品。

    :param release: 发行日期，None 一律不通过
    :param today: 参照的「今天」
    :param recent_days: 追新窗口天数
    :param preorder_days: 提前订阅窗口天数
    :return: 是否在窗口内
    """
    if not release:
        return False
    delta = (release - today).days
    if delta > 0:
        return preorder_days > 0 and delta <= preorder_days
    return recent_days > 0 and -delta <= recent_days


def normalize_album_types(value: Any) -> List[str]:
    """把配置里的订阅类型规范成小写去重列表，非法项直接丢弃。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    known = {option["value"] for option in ALBUM_TYPE_OPTIONS}
    result: List[str] = []
    for item in value:
        text = str(item or "").strip().lower()
        if text and text in known and text not in result:
            result.append(text)
    return result


def album_type_tokens(item: Any) -> Set[str]:
    """
    取条目的类型标记（小写）：**只取主类型**，不再合并 ``secondary_types``。

    取舍说明：MusicBrainz 的副类型（``Soundtrack`` / ``Live`` / ``Compilation`` 等）
    是**附属标签而非并列类型**，取值里根本不含 album / ep / single，合并进来加不出
    任何命中；而「原声带 / 现场 / 合辑」这类条目本身的主类型就是 Album / EP / Single，
    勾选前三项时天然会被选中。删掉的是「把副类型当并列类型用」，不是排除这类条目——
    例如《在暴雪时分》是 ``EP`` + ``Soundtrack``，勾选 ``ep`` 时靠主类型照样命中。

    :param item: 音乐条目（dict 或宿主对象）
    :return: 小写的类型标记集合（主类型为空时是空集合）
    """
    primary = str(field_of(item, "album_type") or "").strip().lower()
    return {primary} if primary else set()


def matches_album_type(item: Any, allowed: Sequence[str]) -> bool:
    """条目的主类型命中配置集合即算通过（小写后单维比对）。"""
    if not allowed:
        return False
    return bool(album_type_tokens(item) & set(allowed))


def passes_min_year(release: Optional[date], min_year: Optional[int]) -> bool:
    """年份下限校验：``min_year`` 为空表示不限；日期无法解析时一律不通过。"""
    if not release:
        return False
    if min_year is None:
        return True
    return release.year >= min_year


def evaluate_album(
        item: Any,
        allowed_types: Sequence[str],
        min_year: Optional[int],
        recent_days: int,
        preorder_days: int,
        today: date,
) -> Tuple[bool, str]:
    """
    按既定顺序执行四条筛选规则（日期 -> 类型 -> 年份 -> 时间窗）。

    :param item: 音乐条目（dict 或宿主对象）
    :param allowed_types: 允许的订阅类型集合（小写）
    :param min_year: 发行年份下限，None 表示不限
    :param recent_days: 追新窗口天数
    :param preorder_days: 提前订阅窗口天数
    :param today: 参照的「今天」
    :return: ``(是否通过, 中文原因)``
    """
    release = parse_release_date(field_of(item, "release_date"))
    if not release:
        return False, "发行日期无法解析"
    if not matches_album_type(item, allowed_types):
        tokens = "/".join(sorted(album_type_tokens(item))) or "未知"
        return False, f"类型不在订阅范围内（条目类型：{tokens}）"
    if not passes_min_year(release, min_year):
        return False, f"发行年份 {release.year} 早于下限 {min_year}"
    if not in_release_window(release, today, recent_days, preorder_days):
        return False, f"不在追新/预购时间窗内（{release.isoformat()}）"
    return True, "通过"


def history_unique(title: Any, media_id: Any) -> str:
    """构造历史条目的稳定唯一键，与仓库内其它插件的历史键风格保持一致。"""
    source = str(getattr(MediaSource.MusicBrainz, "value", MediaSource.MusicBrainz))
    return f"musicartistsubscribe: {title or ''} ({source}:{media_id or ''})"


def parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """把配置值转成整数；空值或非法值返回 ``default``。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def clamp_days(value: Any, default: int) -> int:
    """把「天数」类配置规范成非负整数。"""
    parsed = parse_int(value, default)
    if parsed is None:
        parsed = default
    return max(0, parsed)


def short_artist_id(artist_id: Any, keep: int = ARTIST_ID_DISPLAY_LEN) -> str:
    """
    艺术家ID 的**展示**用法：只留前 ``keep`` 位，其余用「…」省略。

    整串 UUID 铺在详情页里又长又占宽，用户只需前几位就能分辨是哪位歌手；
    完整 ID 仍然保留在链接地址里，点开或复制链接即可拿到，故此处只截断显示。
    比 ``keep`` 还短的值原样返回，不画蛇添足地加省略号。

    :param artist_id: 完整的 MusicBrainz 艺术家ID
    :param keep: 保留的前缀长度
    :return: 截断后的展示文本
    """
    text = str(artist_id or "")
    if len(text) <= keep:
        return text
    return text[:keep] + "…"


class MusicArtistSubscribe(_PluginBase):
    """按歌手名单定时检查 MusicBrainz 新发行并创建订阅。"""

    # 插件名称
    plugin_name = "歌手作品订阅"
    # 插件描述
    plugin_desc = "按歌手自动订阅 MusicBrainz 上新增的专辑、EP 与单曲（只追新发行，不回补历史）"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/Lyzd1/MoviePilot-Plugins/main/icons/musicartistsubscribe.png"
    # 插件版本
    plugin_version = "1.0.6"
    # 插件作者
    plugin_author = "Lyzd1"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "musicartistsubscribe_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1
    # 插件标签
    plugin_label = "订阅"

    def __init__(self) -> None:
        """初始化运行期状态。"""
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._dry_run = True
        self._cron = ""
        self._artists = ""
        self._album_types: List[str] = ["album", "ep"]
        self._min_year: Optional[int] = None
        self._recent_days = 30
        self._preorder_days = 30
        self._scheduler: Optional[BackgroundScheduler] = None
        # 防止上一轮还没跑完就被下一次定时触发重入
        self._run_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: dict = None) -> None:
        """装载配置、处理一次性开关并把定时任务注册到后台调度器。"""
        self.stop_service()

        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._dry_run = bool(config.get("dry_run", True))
        self._cron = str(config.get("cron") or "").strip()
        self._artists = str(config.get("artists") or "")
        configured_types = config.get("album_types")
        self._album_types = normalize_album_types(
            configured_types if configured_types is not None else ["album", "ep"]
        )
        self._min_year = parse_int(config.get("min_year"), None)
        self._recent_days = clamp_days(config.get("recent_days"), 30)
        self._preorder_days = clamp_days(config.get("preorder_days"), 30)

        if bool(config.get("clear_history", False)):
            self.del_data(key=KEY_HISTORY)
            logger.info("歌手作品订阅：已清空订阅历史")
        if bool(config.get("clear_handled", False)):
            self.del_data(key=KEY_HANDLED)
            logger.info("歌手作品订阅：已清空已处理记录，历史作品可能被重新处理")
        if bool(config.get("clear_history", False)) or bool(config.get("clear_handled", False)):
            self.__update_config()

        if not (self._enabled or self._onlyonce):
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        if self._onlyonce:
            logger.info("歌手作品订阅：服务启动，立即运行一次")
            self._scheduler.add_job(
                self.__run_once,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="歌手作品订阅",
            )
            self._onlyonce = False
            self.__update_config()

        if self._cron:
            try:
                self._scheduler.add_job(
                    func=self.__run_once,
                    trigger=CronTrigger.from_crontab(self._cron),
                    name="歌手作品订阅",
                )
            except Exception as err:
                logger.error(f"歌手作品订阅：执行周期配置错误：{err}")
                self.systemmessage.put(f"歌手作品订阅：执行周期配置错误：{err}")

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def stop_service(self) -> None:
        """停止插件的后台定时任务并释放调度器。"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error(f"歌手作品订阅：退出插件失败：{err}")

    def __update_config(self) -> None:
        """回写配置，主要用于把一次性开关（立即运行/清空）复位。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "dry_run": self._dry_run,
                "cron": self._cron,
                "artists": self._artists,
                "album_types": self._album_types,
                "min_year": self._min_year,
                "recent_days": self._recent_days,
                "preorder_days": self._preorder_days,
                "clear_history": False,
                "clear_handled": False,
            }
        )

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def __run_once(self) -> None:
        """定时任务入口：加锁后执行一轮完整检查，异常不向外抛。"""
        if not self._run_lock.acquire(blocking=False):
            logger.warning("歌手作品订阅：上一轮仍在运行，本次跳过")
            return
        try:
            self.__run_once_inner()
        except Exception as err:
            logger.error(f"歌手作品订阅：本轮执行异常：{err}", exc_info=True)
            self.systemmessage.put(f"歌手作品订阅执行异常：{err}")
        finally:
            self._run_lock.release()

    def __run_once_inner(self) -> None:
        """一轮完整检查：解析歌手 -> 抓作品 -> 筛选 -> 查重 -> 建订阅。"""
        entries = parse_artist_entries(self._artists)
        if not entries:
            logger.warning("歌手作品订阅：未配置歌手名单，停止运行")
            return

        history: List[dict] = self.get_data(KEY_HISTORY) or []
        handled: List[str] = self.get_data(KEY_HANDLED) or []
        if not isinstance(history, list):
            history = []
        if not isinstance(handled, list):
            handled = []
        cache = self.get_data(KEY_ARTISTS_CACHE) or {}
        if not isinstance(cache, dict):
            cache = {}

        today = datetime.now().date()
        mode = "预演模式" if self._dry_run else "正式模式"
        logger.info(
            f"歌手作品订阅：开始检查 {len(entries)} 位歌手（{mode}，"
            f"类型={'/'.join(self._album_types) or '未选择'}，"
            f"追新 {self._recent_days} 天，提前 {self._preorder_days} 天）"
        )

        resolved_records: List[dict] = []
        summary: List[str] = []
        planned = 0
        reused_count = 0

        for display_name, pinned_id in entries:
            resolved, reused = self.__resolve_artist(display_name, pinned_id, cache)
            if reused:
                reused_count += 1
            resolved_records.append(resolved)
            artist_id = str(resolved.get("media_id") or "")
            if not artist_id:
                summary.append(f"歌手「{display_name}」：未解析到艺术家，已跳过")
                continue

            albums = self.__fetch_albums(artist_id)
            if not albums:
                if reused:
                    # 沿用缓存的 ID 却取不到作品，可能是该艺术家被合并/改名了：
                    # 清掉这条解析记录，下次运行重新识别
                    cache.pop(artist_entry_key(display_name, pinned_id), None)
                    logger.warning(
                        f"歌手作品订阅：{resolved.get('name') or display_name} 沿用上次解析的艺术家未取到作品，"
                        f"已清除该解析记录，下次运行重新识别"
                    )
                summary.append(f"歌手「{resolved.get('name') or display_name}」：未取到作品")
                continue

            if not reused:
                # 沿用时不回写，免得把「沿用」的说明写回缓存
                cache[artist_entry_key(display_name, pinned_id)] = {
                    k: v for k, v in resolved.items() if k != "from_cache"
                }

            artist_label = resolved.get("name") or display_name
            hit = 0
            created = 0
            for item in albums:
                passed, reason = evaluate_album(
                    item=item,
                    allowed_types=self._album_types,
                    min_year=self._min_year,
                    recent_days=self._recent_days,
                    preorder_days=self._preorder_days,
                    today=today,
                )
                title = field_of(item, "title") or ""
                if not passed:
                    logger.debug(f"歌手作品订阅：{artist_label} - {title} 跳过（{reason}）")
                    continue

                hit += 1
                media_id = str(field_of(item, "media_id") or "")
                if not media_id:
                    logger.debug(f"歌手作品订阅：{artist_label} - {title} 缺少媒体ID，跳过")
                    continue
                if media_id in handled:
                    logger.debug(f"歌手作品订阅：{artist_label} - {title} 本轮之前已处理，跳过")
                    continue

                if self.__subscribe_exists(item):
                    logger.info(f"歌手作品订阅：{artist_label} - {title} 订阅已存在")
                    handled.append(media_id)
                    self.__save_progress(history, handled)
                    continue

                if self.__media_exists(item):
                    logger.info(f"歌手作品订阅：{artist_label} - {title} 媒体库中已存在")
                    handled.append(media_id)
                    self.__save_progress(history, handled)
                    continue

                if self._dry_run:
                    planned += 1
                    self.__record(
                        history=history,
                        item=item,
                        artist=artist_label,
                        status=STATUS_DRY_RUN,
                    )
                    self.__save_progress(history, handled)
                    logger.info(f"歌手作品订阅：预演命中 {artist_label} - {title}")
                    continue

                if self.__subscribe(history, item, artist_label, title):
                    created += 1
                    handled.append(media_id)
                self.__save_progress(history, handled)

            summary.append(
                f"歌手「{artist_label}」：候选 {len(albums)} 条 / 命中 {hit} 条 / 新建订阅 {created} 条"
            )

        logger.info(
            f"歌手作品订阅：本轮解析 —— 沿用上次 {reused_count} 位 / 重新识别 {len(entries) - reused_count} 位"
        )
        self.save_data(KEY_ARTISTS_RESOLVED, resolved_records)
        self.save_data(KEY_LAST_RUN, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.save_data(KEY_HANDLED, handled)
        self.save_data(KEY_ARTISTS_CACHE, cache)
        logger.info("歌手作品订阅：本轮汇总 —— " + "；".join(summary))
        if self._dry_run and planned:
            logger.warning(f"歌手作品订阅：预演完成，将订阅 {planned} 条（预演模式不会真正创建订阅）")
        self.save_data(KEY_HISTORY, history)

    # ------------------------------------------------------------------ #
    # 歌手解析
    # ------------------------------------------------------------------ #
    def __resolve_artist(
            self, display_name: str, pinned_id: Optional[str], cache: dict
    ) -> Tuple[dict, bool]:
        """
        解析一位歌手，返回详情页展示用的解析记录与「本轮是否沿用缓存」。

        配置条目在 ``cache`` 里有可用记录（``media_id`` 非空）时直接沿用，本轮不再
        发任何识别请求；否则填了 ``@ID`` 就采用并顺带拉一次艺术家详情用于展示，
        没填就走宿主搜索，在候选里找「规范化同名」（忽略大小写与空格，比较 name 与 aliases）。
        搜索失败或没解析出 ID 属于「没定下来」，不写进缓存，下轮仍会重试。

        :param display_name: 配置里写的歌手名
        :param pinned_id: 配置里锁定的 MusicBrainz 艺术家 ID
        :param cache: 歌手解析缓存 ``{缓存键: 解析记录}``
        :return: ``(解析记录字典, 本轮是否沿用缓存)``
        """
        record = {
            "config_name": display_name,
            "name": "",
            "media_id": "",
            "country": "",
            "artist_type": "",
            "locked": False,
            "exact_match": False,
            "candidate_count": 0,
            "detail_link": "",
            "note": "",
            # 记录是否来自缓存：仅用于排查，落盘时会被剔除
            "from_cache": False,
        }

        cached = cache.get(artist_entry_key(display_name, pinned_id)) if isinstance(cache, dict) else None
        if isinstance(cached, dict) and str(cached.get("media_id") or ""):
            # 配置文本一字未改：沿用当初的解析结果，note 原样保留（异常提示继续显示）
            record = dict(cached)
            record["config_name"] = display_name
            record["from_cache"] = True
            if pinned_id:
                record["locked"] = True
            logger.info(
                f"歌手作品订阅：{display_name} 沿用上次解析 {record.get('name') or display_name}"
                f"（{record.get('country') or '未知'} / {record.get('artist_type') or '未知'}）"
                f"{record['media_id']}"
            )
            return record, True

        if pinned_id:
            record.update({
                "name": display_name,
                "media_id": pinned_id,
                "locked": True,
                "exact_match": True,
                "detail_link": self.__artist_link(pinned_id),
                "note": "已按配置的艺术家ID锁定，未做搜索",
            })
            info = self.__fetch_artist_info(pinned_id)
            if info:
                record["name"] = field_of(info, "name") or display_name
                record["country"] = str(field_of(info, "country") or "")
                record["artist_type"] = str(field_of(info, "artist_type") or "")
                record["detail_link"] = field_of(info, "detail_link") or record["detail_link"]
                logger.info(
                    f"歌手作品订阅：{display_name} 已锁定为 "
                    f"{record['name']}（{record['country'] or '未知'} / "
                    f"{record['artist_type'] or '未知'}）{pinned_id}"
                )
            else:
                logger.warning(
                    f"歌手作品订阅：{display_name} 锁定了艺术家ID {pinned_id}，但未取到艺术家详情"
                )
            return record, False

        try:
            candidates = MediaChain().search_persons(
                name=display_name,
                media_source=MediaSource.MusicBrainz,
            ) or []
        except Exception as err:
            logger.error(f"歌手作品订阅：搜索歌手 {display_name} 失败：{err}")
            record["note"] = f"搜索失败：{err}"
            return record, False

        picked = pick_artist_candidate(display_name, candidates)
        chosen = picked["chosen"]
        if chosen is None:
            logger.warning(f"歌手作品订阅：未搜索到歌手 {display_name}，跳过")
            record["note"] = picked["note"]
            return record, False
        if not picked["exact_match"] or picked["multiple"]:
            logger.warning(f"歌手作品订阅：{display_name} {picked['note']}")

        media_id = str(field_of(chosen, "media_id") or "")
        record.update({
            "name": str(field_of(chosen, "name") or display_name),
            "media_id": media_id,
            "country": str(field_of(chosen, "country") or ""),
            "artist_type": str(field_of(chosen, "artist_type") or ""),
            "exact_match": bool(picked["exact_match"]),
            "candidate_count": int(picked["candidate_count"]),
            # 原先漏写这一项，__artist_issue() 里的同名候选告警永远不亮
            "multiple": bool(picked["multiple"]),
            "detail_link": field_of(chosen, "detail_link") or self.__artist_link(media_id),
            "note": picked["note"],
        })
        logger.info(
            f"歌手作品订阅：{display_name} 解析为 {record['name']}"
            f"（{record['country'] or '未知'} / {record['artist_type'] or '未知'}）"
            f" {media_id} 候选 {record['candidate_count']} 个"
        )
        return record, False

    @staticmethod
    def __artist_link(artist_id: str) -> str:
        """构造 MusicBrainz 艺术家详情链接。"""
        return f"https://musicbrainz.org/artist/{artist_id}" if artist_id else ""

    def __music_page_link(self, entity: str, media_id: str, title: str) -> str:
        """
        构造 MoviePilot **自己的**音乐页地址（歌手页 / 专辑页共用这一个构造器）。

        ``entity`` 就是宿主前端自己的两个 hash 路由段：

        - ``"artist"``：歌手页 ``/music/artist``；
        - ``"album"``：专辑页 ``/music/album``（release-group 在 MP 里就是「专辑」实体）。

        两者认的都是 ``media_source`` / ``media_id`` / ``title`` 三个 query 参数，
        与前端自身拼链接的口径一致，故收敛成一个构造器，避免两份重复逻辑走偏。
        这里刻意返回**相对 hash 链接**：用户可能挂着反代或域名，写死主机名会失效，
        交给浏览器按当前站点解析即可。

        :param entity: 路由段，``"artist"``（歌手页）或 ``"album"``（专辑页）
        :param media_id: 完整的 MusicBrainz ID（艺术家ID 或 release-group ID）
        :param title: 展示标题，用作 ``title``（可为空，留空即可）
        :return: 形如 ``#/music/<entity>?media_source=...&media_id=...&title=...`` 的相对链接
        """
        source = str(getattr(MediaSource.MusicBrainz, "value", MediaSource.MusicBrainz))
        return (
            f"#/music/{entity}?media_source={quote(source, safe='')}"
            f"&media_id={quote(str(media_id or ''), safe='')}"
            f"&title={quote(str(title or ''), safe='')}"
        )

    def __fetch_artist_info(self, artist_id: str) -> Optional[Any]:
        """按 ID 拉取艺术家详情（仅用于展示），失败时返回 None。"""
        try:
            return asyncio.run(
                MediaChain().async_get_music_artist(
                    media_source=MediaSource.MusicBrainz,
                    media_id=artist_id,
                )
            )
        except Exception as err:
            logger.debug(f"歌手作品订阅：读取艺术家 {artist_id} 详情失败：{err}")
            return None

    # ------------------------------------------------------------------ #
    # 作品目录
    # ------------------------------------------------------------------ #
    def __fetch_albums(self, artist_id: str) -> List[Any]:
        """同步取回一位歌手的作品目录（内部走异步接口）。"""
        try:
            return asyncio.run(self.__async_fetch_albums(artist_id))
        except Exception as err:
            logger.error(f"歌手作品订阅：拉取艺术家 {artist_id} 作品失败：{err}")
            return []

    async def __async_fetch_albums(self, artist_id: str) -> List[Any]:
        """
        按每种订阅类型分页拉取作品，合并后按 ``media_id`` 去重。

        宿主只在页内排序，高产歌手的新作不一定落在首页，所以每类最多翻
        ``MAX_PAGES`` 页、每页 ``PAGE_SIZE`` 条；某页不满一页即认为已到底。
        """
        chain = MediaChain()
        albums: List[Any] = []
        seen: Set[str] = set()
        for album_type in self._album_types:
            for page in range(1, MAX_PAGES + 1):
                try:
                    chunk = await chain.async_get_music_artist_albums(
                        media_source=MediaSource.MusicBrainz,
                        media_id=artist_id,
                        page=page,
                        count=PAGE_SIZE,
                        album_type=album_type,
                    )
                except Exception as err:
                    logger.error(
                        f"歌手作品订阅：拉取作品失败（艺术家={artist_id} "
                        f"类型={album_type} 第{page}页）：{err}"
                    )
                    break
                if not chunk:
                    break
                for item in chunk:
                    media_id = str(field_of(item, "media_id") or "")
                    if not media_id or media_id in seen:
                        continue
                    seen.add(media_id)
                    albums.append(item)
                if len(chunk) < PAGE_SIZE:
                    break
        return albums

    # ------------------------------------------------------------------ #
    # 查重与订阅
    # ------------------------------------------------------------------ #
    @staticmethod
    def __subscribe_exists(item: Any) -> bool:
        """查订阅是否已存在（宿主内部按 media_source + media_id + music_type 判重）。"""
        try:
            return bool(SubscribeChain.exists(mediainfo=item))
        except Exception as err:
            logger.error(f"歌手作品订阅：查询订阅失败：{err}")
            return False

    @staticmethod
    def __media_exists(item: Any) -> bool:
        """
        查媒体库是否已有该作品。

        作品列表来自 Release Group，本身不带曲数；宿主的做法是把
        ``total_tracks`` 补成非空再查（无曲数时一首匹配曲目即足够判定已入库）。
        这里复制一份条目再补 1，避免污染原对象。
        """
        try:
            probe = copy.copy(item)
            try:
                probe.total_tracks = getattr(probe, "total_tracks", None) or 1
            except Exception:
                # 对象不支持写属性时退回原条目，仍能完成查询
                probe = item
            return MediaChain().media_exists(mediainfo=probe) is not None
        except Exception as err:
            logger.error(f"歌手作品订阅：查询媒体库失败：{err}")
            return False

    def __subscribe(self, history: List[dict], item: Any, artist: str, title: str) -> bool:
        """
        创建一条音乐订阅，返回是否成功。

        宿主会在 ``add`` 内部重新识别并校验：未发行或尚无实体发行的 release-group
        可能因取不到曲目数而报「专辑总曲目数未知，无法累计专辑下载进度」——这属于
        正常情况，记日志跳过即可，不中断整轮检查。失败条目不写入已处理记录，
        下一轮仍会重试。

        :param history: 本轮共用的历史列表（就地追加/覆盖）
        :param item: 待订阅的音乐条目
        :param artist: 歌手展示名，仅用于日志与历史
        :param title: 作品标题
        :return: 是否成功创建订阅
        """
        year = field_of(item, "year")
        try:
            subscribe_id, error = SubscribeChain().add(
                title=title,
                year=str(year or ""),
                mtype=MediaType.MUSIC,
                media_source=MediaSource.MusicBrainz,
                media_id=str(field_of(item, "media_id") or ""),
                # 宿主只接受 recording / album 两种实体，release-group 统一按专辑订阅
                music_type="album",
                exist_ok=True,
                username=self.plugin_name,
                # 用户明确要求本插件不发送订阅通知，固定关闭（不做成配置项）
                message=False,
            )
        except Exception as err:
            logger.error(f"歌手作品订阅：添加订阅异常 {artist} - {title}：{err}")
            self.__record(history, item, artist, STATUS_FAILED, str(err))
            return False

        if not subscribe_id:
            logger.error(f"歌手作品订阅：添加订阅失败 {artist} - {title}：{error or '未知错误'}")
            self.__record(history, item, artist, STATUS_FAILED, str(error or "未知错误"))
            return False

        logger.info(f"歌手作品订阅：已创建订阅 {artist} - {title}（订阅ID {subscribe_id}）")
        self.__record(history, item, artist, STATUS_SUBSCRIBED)
        return True

    def __record(
            self,
            history: List[dict],
            item: Any,
            artist: str,
            status: str,
            message: str = "",
    ) -> None:
        """写入一条历史记录（同 unique 覆盖，避免重复堆积）并立即落盘。"""
        if not isinstance(history, list):
            history = []
        title = str(field_of(item, "title") or "")
        media_id = str(field_of(item, "media_id") or "")
        entry = {
            "title": title,
            "type": MediaType.MUSIC.value,
            "album_type": str(field_of(item, "album_type") or ""),
            "artist": artist,
            "release_date": str(field_of(item, "release_date") or ""),
            "media_source": str(getattr(MediaSource.MusicBrainz, "value", MediaSource.MusicBrainz)),
            "media_id": media_id,
            "cover_url": field_of(item, "cover_url") or "",
            "detail_link": field_of(item, "detail_link") or "",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "message": message,
            "unique": history_unique(title, media_id),
        }
        for index, old in enumerate(history):
            if isinstance(old, dict) and old.get("unique") == entry["unique"]:
                history[index] = entry
                break
        else:
            history.append(entry)
        self.save_data(KEY_HISTORY, history)

    def __save_progress(self, history: List[dict], handled: List[str]) -> None:
        """每处理完一条就落盘进度，避免中途失败时丢掉本轮已完成的部分。"""
        self.save_data(KEY_HISTORY, history)
        self.save_data(KEY_HANDLED, handled)

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #
    def delete_history(self, key: str) -> schemas.Response:
        """按唯一键删除一条历史记录。"""
        history = self.get_data(KEY_HISTORY)
        if not isinstance(history, list) or not history:
            return schemas.Response(success=False, message="未找到历史记录")
        remaining = [
            item for item in history
            if not (isinstance(item, dict) and item.get("unique") == key)
        ]
        if len(remaining) == len(history):
            return schemas.Response(success=False, message="未找到对应的历史记录")
        self.save_data(KEY_HISTORY, remaining)
        return schemas.Response(success=True, message="删除成功")

    def clear_history(self) -> schemas.Response:
        """清空全部订阅历史。"""
        self.del_data(key=KEY_HISTORY)
        logger.info("歌手作品订阅：已通过接口清空订阅历史")
        return schemas.Response(success=True, message="已清空订阅历史")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：删除单条历史与清空历史。"""
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "删除一条订阅历史",
                "description": "按 unique 删除一条历史记录",
                "response_model": schemas.Response[None],
            },
            {
                "path": "/history",
                "endpoint": self.delete_history,
                "methods": ["DELETE"],
                "auth": "bear",
                "summary": "删除一条订阅历史",
                "description": "按 unique 删除一条历史记录",
                "response_model": schemas.Response[None],
            },
            {
                "path": "/clear_history",
                "endpoint": self.clear_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空订阅历史",
                "description": "清空本插件的全部订阅历史记录",
                "response_model": schemas.Response[None],
            },
        ]

    # ------------------------------------------------------------------ #
    # 配置页
    # ------------------------------------------------------------------ #
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页面描述与默认配置。"""
        return [
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
                                            "text": "按歌手名单检查 MusicBrainz 上的新发行并自动订阅。"
                                                    "只追新发行，不回补历史：首次启用时请用「追新窗口」"
                                                    "限制范围，建议先开「预演模式」跑一轮，"
                                                    "在详情页确认清单无误后再关闭预演正式订阅。",
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
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                            "hint": "保存后约 3 秒执行一轮，跑完自动关闭",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dry_run",
                                            "label": "预演模式",
                                            "hint": "只识别、筛选并记录将要订阅的清单，不真正创建订阅",
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "0 */6 * * *",
                                            "hint": "5 位 cron 表达式，留空则不定时执行",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_year",
                                            "label": "发行年份下限",
                                            "type": "number",
                                            "placeholder": f"{datetime.now().year}",
                                            "hint": "只处理发行年 ≥ 该年的作品；留空 = 不限",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "recent_days",
                                            "label": "追新窗口（天）",
                                            "type": "number",
                                            "placeholder": "30",
                                            "hint": "只订最近 N 天内已发行的作品；0 = 不订已发行的",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "preorder_days",
                                            "label": "提前订阅窗口（天）",
                                            "type": "number",
                                            "placeholder": "30",
                                            "hint": "未发行的作品在发行前 N 天内开始订阅；0 = 不提前",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "artists",
                                            "label": "歌手名单（一行一个）",
                                            "rows": 6,
                                            "auto-grow": True,
                                            "placeholder": "IU@b9545342-1e6d-4dae-84ac-013374ad8d7c\n许嵩",
                                            "hint": _ARTISTS_HINT,
                                            "persistent-hint": True,
                                            "style": "white-space: pre-line;",
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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "album_types",
                                            "label": "订阅类型",
                                            "items": ALBUM_TYPE_OPTIONS,
                                            "multiple": True,
                                            "chips": True,
                                            "hint": "音乐以 release-group 为单位订阅；按 MusicBrainz 主类型匹配"
                                                    "（专辑/EP/单曲），与宿主前端艺术家页一致",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "style": "white-space: pre-line;",
                                            "text": _FILTER_HINT,
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear_history",
                                            "label": "清空已订阅历史",
                                            "hint": "清空详情页的历史记录（不影响已处理记录）",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear_handled",
                                            "label": "清空已处理记录",
                                            "hint": "慎用：清空后，仍在窗口内的历史作品可能被重新订阅一遍",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "dry_run": True,
            "cron": "0 */6 * * *",
            "artists": "",
            "album_types": ["album", "ep"],
            "min_year": datetime.now().year,
            "recent_days": 30,
            "preorder_days": 30,
            "clear_history": False,
            "clear_handled": False,
        }

    # ------------------------------------------------------------------ #
    # 详情页
    # ------------------------------------------------------------------ #
    def get_page(self) -> List[dict]:
        """
        返回插件详情页：统计卡 -> 订阅展示 -> 歌手解析 -> 预演提醒。

        页面顺序（v1.0.3 起）：

        1. **统计卡**：四张（已处理总数 / 已订阅 / 预演 / 订阅失败）；
        2. **订阅展示**：所有订阅记录**平铺**成一张卡片，按处理时间倒序，每行
           封面 + 标题 + 歌手名 + 类型 + 发行日期 + 状态 + 处理时间 + 删除；
        3. **歌手解析**：解析结果单独成一块放在下面，每位歌手一行；
        4. **预演模式提醒**（仅预演时）。

        作品与歌手解析不再混排：作品行按时间倒序平铺后，同一位歌手的作品可能
        不相邻，所以每行都带上歌手名；歌手的解析情况（ID、异常提示）统一在
        下面的「歌手解析」区查看。
        """
        history = self.get_data(KEY_HISTORY)
        history = history if isinstance(history, list) else []
        handled = self.get_data(KEY_HANDLED)
        handled = handled if isinstance(handled, list) else []
        resolved = self.get_data(KEY_ARTISTS_RESOLVED)
        resolved = resolved if isinstance(resolved, list) else []

        entries = [item for item in history if isinstance(item, dict)]
        records = [record for record in resolved if isinstance(record, dict)]

        subscribed = sum(1 for item in entries if item.get("status") == STATUS_SUBSCRIBED)
        dry_run = sum(1 for item in entries if item.get("status") == STATUS_DRY_RUN)
        failed = sum(1 for item in entries if item.get("status") == STATUS_FAILED)

        page: List[dict] = [
            {
                "component": "VRow",
                "content": [
                    self.__stat_card("已处理总数", len(handled)),
                    self.__stat_card("已订阅", subscribed),
                    self.__stat_card("预演", dry_run),
                    self.__stat_card("订阅失败", failed),
                ],
            },
            self.__subscriptions_card(entries),
            self.__artists_card(records),
        ]

        if self._dry_run:
            page.append({
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "style": "white-space: pre-line;",
                    "text": "当前处于预演模式：命中的作品只记录在上面的订阅列表里，不会真正创建订阅。"
                            "确认清单无误后，请在配置里关闭「预演模式」。",
                },
            })
        return page

    def __subscriptions_card(self, entries: List[dict]) -> dict:
        """
        订阅展示区：把全部订阅记录**平铺**成一张卡片（不再按歌手分卡片）。

        排序取处理时间倒序（``time`` 是 ``YYYY-MM-DD HH:MM:SS``，字符串序即时间序；
        缺时间的旧记录排到最后）。平铺之后同一位歌手的作品可能不相邻，因此每行都
        带歌手名（见 ``__work_row``）。

        :param entries: 历史记录（已过滤出 dict）
        :return: 订阅展示卡片节点
        """
        content: List[dict] = [
            self.__section_title("订阅作品"),
            {"component": "VDivider", "props": {"class": "my-2"}},
        ]
        if entries:
            for entry in sorted(
                    entries, key=lambda item: str(item.get("time") or ""), reverse=True
            ):
                content.append(self.__work_row(entry))
        else:
            content.append(self.__text_line(
                self.__empty_message(), "text-caption text-medium-emphasis py-1"
            ))
        return {
            "component": "VCard",
            # px-3 给内容统一的左右内边距：窄屏上标题行与记录行都不再贴着卡片边缘，
            # 右侧那一列也不会因为贴边而被裁掉。
            "props": {"variant": "tonal", "class": "mb-2 px-3 py-2"},
            "content": content,
        }

    def __empty_message(self) -> str:
        """订阅展示还没有内容时，按「有没有配歌手」给不同的引导文案。"""
        if parse_artist_entries(getattr(self, "_artists", "") or ""):
            return "尚未运行过：启用插件并运行一次后，这里会按处理时间倒序列出订阅到的作品。"
        return "还没有配置歌手：请在配置页填写「歌手名单」并启用插件。"

    def __artists_card(self, records: List[dict]) -> dict:
        """
        歌手解析区：单独一块放在订阅展示**下面**，每位歌手一行。

        只展示解析结果（ID、锁定状态与解析异常），不再重复作品清单——作品统一在
        上面的订阅展示里看。解析异常（多个同名候选 / 无精确同名匹配 / 未解析到 ID /
        搜索失败）以 warning 紧跟在对应歌手那一行下面，仍然醒目。

        :param records: ``artists_resolved`` 里的解析记录（已过滤出 dict）
        :return: 歌手解析卡片节点
        """
        content: List[dict] = [
            self.__section_title("歌手解析"),
            {"component": "VDivider", "props": {"class": "my-2"}},
        ]
        if records:
            for record in records:
                content.append(self.__artist_row(record))
                issue = self.__artist_issue(record)
                if issue:
                    content.append({
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "text-caption mb-2",
                            "style": "white-space: pre-line;",
                            "text": issue,
                        },
                    })
        else:
            content.append(self.__text_line(
                self.__artists_empty_message(), "text-caption text-medium-emphasis py-1"
            ))
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-2 px-3 py-2"},
            "content": content,
        }

    def __artist_row(self, record: dict) -> dict:
        """
        歌手解析区的**一行**：只有歌手名（配置名与解析名不同才显示 ``配置名 → 解析名``）
        + 「已按ID锁定」chip（只在手动钉了 ID 时出现）+ 截断后可点击的艺术家ID。

        国家/类型这类括号内容、以及「精确匹配」chip（绝大多数歌手都会命中，属于噪音
        还占宽度）都不显示；解析异常由 ``__artist_issue`` 单独 warning 提示。
        这一行原先还挂着「已订阅 x · 预演 y · 失败 z」的计数摘要，平铺之后作品行
        不再按歌手归类，那份计数的归属感消失，故一并去掉。

        :param record: 该歌手的解析记录
        :return: 解析行节点
        """
        name = str(record.get("name") or record.get("config_name") or "未知歌手")
        config_name = str(record.get("config_name") or "")
        media_id = str(record.get("media_id") or "")

        if media_id:
            # 配置名与解析名一致时省略箭头，避免「许嵩 → 许嵩」这类冗余
            if config_name and normalize_name(config_name) != normalize_name(name):
                head = f"{config_name} → {name}"
            else:
                head = name
        else:
            head = f"{config_name or name} → 未解析到艺术家"

        nodes: List[dict] = [
            {"component": "span", "props": {"class": "text-body-2"}, "text": head},
        ]
        if record.get("locked"):
            nodes.append(self.__chip("已按ID锁定", "info"))
        if media_id:
            # 跳到 MoviePilot 自己的歌手页（不再跳第三方 MusicBrainz），显示文本仍截断
            link = self.__music_page_link("artist", media_id, name)
            nodes.append({
                "component": "a",
                "props": {
                    "href": link,
                    "target": "_blank",
                    "class": "text-caption text-medium-emphasis",
                },
                "text": short_artist_id(media_id),
            })
        return {
            "component": "div",
            "props": {"class": "d-flex align-center flex-wrap ga-2 py-1"},
            "content": nodes,
        }

    def __artists_empty_message(self) -> str:
        """歌手解析区还没有内容时，按「有没有配歌手」给不同的引导文案。"""
        if parse_artist_entries(getattr(self, "_artists", "") or ""):
            return "尚未运行过：运行一次后，这里会显示每位歌手的解析结果。"
        return "还没有配置歌手：请在配置页填写「歌手名单」。"

    @staticmethod
    def __chip(text: str, color: str) -> dict:
        """构造一个极短标记用的 chip（本插件目前只用于「已按ID锁定」）。"""
        return {
            "component": "VChip",
            "props": {
                "size": "x-small",
                "color": color,
                "variant": "tonal",
                "label": True,
            },
            "text": text,
        }

    @staticmethod
    def __artist_issue(record: dict) -> str:
        """
        判断解析结果是否需要**显眼提示**，返回说明文字；正常情况返回空串。

        正常情况（精确同名唯一命中 / 已按 ID 锁定）不再单独用一行文字重复说明；
        只有以下异常才返回文案，由调用方用 warning 配色醒目展示：
        未解析到艺术家ID、多个同名候选、无精确同名匹配、搜索失败。
        """
        if not isinstance(record, dict):
            return ""
        note = str(record.get("note") or "").strip()
        media_id = str(record.get("media_id") or "").strip()
        if not media_id:
            return note or "未解析到艺术家ID，已跳过"
        if record.get("multiple"):
            return note or "存在多个同名候选，如订错请改用「名字@艺术家ID」锁定"
        if record.get("locked"):
            return ""
        if not record.get("exact_match"):
            return note or "无精确同名匹配，如订错请改用「名字@艺术家ID」锁定"
        return ""

    def __work_row(self, item: dict) -> dict:
        """
        订阅展示里的一条作品记录（紧凑一行，窄屏可折行）。

        从左到右：**封面缩略图**（缺失时用等尺寸灰底 + 音乐图标占位）+ 标题
        （有 ``media_id`` 时是 MoviePilot 自己的专辑页链接，没有则退化成纯文本）
        + 歌手名 + 「类型 / 发行日期 / 处理结果 / 处理时间」，订阅失败时再补上
        ``message`` 里的错误原因。

        平铺之后同一位歌手的作品不一定相邻，所以歌手名必须跟着每一行进；各项各自是
        独立节点、由 flex 间隙分隔（不再用固定列宽的 ``VCol`` 硬挤），外层
        ``flex-wrap``：窄屏上折行而不是被裁掉右半截。
        """
        title = str(item.get("title") or "")
        media_id = str(item.get("media_id") or "")
        artist = str(item.get("artist") or "") or "未知歌手"
        album_type = str(item.get("album_type") or "专辑")
        release_date = str(item.get("release_date") or "无日期")
        status = str(item.get("status") or "")
        status_label = STATUS_LABELS.get(status, status or "未知")
        message = str(item.get("message") or "")
        handle_time = str(item.get("time") or "")

        title_node: dict
        if media_id:
            # 跳到 MoviePilot 自己的专辑页（不再跳第三方 MusicBrainz 页面）
            title_node = {
                "component": "a",
                "props": {
                    "href": self.__music_page_link("album", media_id, title),
                    "target": "_blank",
                },
                "text": title,
            }
        else:
            title_node = {"component": "span", "text": title}

        status_class = {
            STATUS_SUBSCRIBED: "text-caption text-success",
            STATUS_DRY_RUN: "text-caption text-warning",
            STATUS_FAILED: "text-caption text-error",
        }.get(status, "text-caption")

        meta: List[dict] = [
            # 歌手名用默认强调色（不带 medium-emphasis），比其它元信息醒目一点
            self.__span(artist, "text-caption"),
            self.__span(album_type, "text-caption text-medium-emphasis"),
            self.__span(release_date, "text-caption text-medium-emphasis"),
            self.__span(status_label, status_class),
        ]
        if status == STATUS_FAILED and message:
            meta.append(self.__span(message, "text-caption text-error"))
        if handle_time:
            meta.append(self.__span(handle_time, "text-caption text-medium-emphasis"))

        unique = item.get("unique") or history_unique(title, item.get("media_id"))
        return {
            "component": "div",
            # 外层 flex-wrap：空间不够时「删除」按钮整块换到下一行，绝不横向溢出
            "props": {"class": "d-flex flex-wrap align-center ga-2 py-1"},
            "content": [
                self.__cover(item.get("cover_url")),
                {
                    "component": "div",
                    # 内层同样 flex-wrap：标题与各元信息之间可以自由折行
                    "props": {
                        "class": "d-flex flex-wrap align-center ga-2 text-body-2 flex-grow-1",
                    },
                    "content": [title_node, *meta],
                },
                {
                    "component": "VBtn",
                    "props": {
                        "size": "small",
                        "variant": "tonal",
                        "color": "error",
                        "text": "删除",
                    },
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/delete_history",
                            "method": "get",
                            "params": {"key": unique},
                        }
                    },
                },
            ],
        }

    @staticmethod
    def __span(text: str, css_class: str = "text-caption text-medium-emphasis") -> dict:
        """构造一个内联文本节点（用于记录行的元信息）。"""
        return {"component": "span", "props": {"class": css_class}, "text": text}

    @staticmethod
    def __cover(cover_url: Any) -> dict:
        """
        构造作品行的封面缩略图。

        历史记录里存的是 MusicBrainz / Cover Art Archive 的封面地址（非空时可用）：
        专辑封面是正方形，故按 ``1/1`` 裁切、边长 ``COVER_SIZE`` 并加圆角。

        封面缺失（老记录或该作品没有封面图）时**不渲染 VImg**——``src`` 为空会露出
        一个空白裂图，比没有更难看——改为渲染等尺寸的灰底块，中间放一个音乐图标。

        :param cover_url: 历史记录里的封面地址
        :return: VImg 或占位块节点
        """
        url = str(cover_url or "").strip()
        if url:
            return {
                "component": "VImg",
                "props": {
                    "src": url,
                    "height": COVER_SIZE,
                    "width": COVER_SIZE,
                    "aspect-ratio": "1/1",
                    "class": "rounded object-cover flex-grow-0",
                    "cover": True,
                },
            }
        return {
            "component": "div",
            "props": {
                "class": "d-flex align-center justify-center rounded flex-grow-0",
                # 半透明灰：浅色与深色主题下都是「一块灰底」而不会一个太白一个太黑
                "style": (
                    f"width: {COVER_SIZE}px; height: {COVER_SIZE}px; "
                    "background-color: rgba(128, 128, 128, 0.22);"
                ),
            },
            "content": [
                {
                    "component": "VIcon",
                    "props": {"size": COVER_SIZE - 28},
                    "text": COVER_PLACEHOLDER_ICON,
                }
            ],
        }

    @staticmethod
    def __section_title(text: str) -> dict:
        """构造区块小标题（订阅作品 / 歌手解析）。"""
        return {
            "component": "div",
            "props": {"class": "text-subtitle-2"},
            "text": text,
        }

    @staticmethod
    def __stat_card(label: str, value: Any) -> dict:
        """
        构造统计小卡片。

        列宽 ``cols=6 md=3``：手机上一行两张、桌面上一行四张。
        原先的 ``md=2`` 是按五张卡的排法定的，删掉「最近运行」后四张卡会排不满一排。
        """
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "class": "text-center pa-2"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "text-caption"},
                            "text": str(label),
                        },
                        {
                            "component": "div",
                            "props": {"class": "text-h6"},
                            "text": str(value),
                        },
                    ],
                }
            ],
        }

    @staticmethod
    def __text_line(text: str, css_class: str = "text-body-2 py-1") -> dict:
        """构造一行说明文本。"""
        return {
            "component": "div",
            "props": {"class": css_class},
            "text": text,
        }
