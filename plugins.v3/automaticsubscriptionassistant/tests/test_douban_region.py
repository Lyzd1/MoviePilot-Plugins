"""豆瓣「扩量地区 / 其它扩量地区 / 扩量取数」的单元测试。

覆盖三件事：正文信息行的地区解析（:meth:`_parse_regions`，必须同时吃内置榜的
「``标签：2026 / 美国 / …``」与列表榜的「``2026 / 韩国 / …``」两种格式）、三个配置字段
合并成地区集合（:meth:`_parse_extra_regions`）、以及叠加式取舍语义
（:meth:`_apply_extra_regions`：浅位照旧全收、深位只收地区命中者）。

本仓库在宿主机之外无法 ``import app.*``，故与 ``test_imdb_filters.py`` 同一套路：文件顶部
先往 ``sys.modules`` 注入最小替身（其中 ``app.sdk.utilities.DomUtils`` 必须能真的读
``xml.dom.minidom`` 节点），再把插件目录注册成「空壳包」，然后用**真实包名**导入被测模块。
**全程不打真实网络**：抓到网络的那条路径（``_fetch_addr``）在用例里用假的 ``RequestUtils``
喂一段自己写的 RSS 文本。替身是**追加式**的，与其它测试文件同场运行时互不覆盖。

刻意**不放** ``tests/__init__.py``：避免被 MoviePilot 当成插件子包加载。

运行：``python3 -m pytest plugins.v3/automaticsubscriptionassistant/tests -q``
"""
from __future__ import annotations

import importlib
import sys
import types
import xml.dom.minidom
from enum import Enum
from pathlib import Path

import pytest


# ==================== 宿主最小替身（必须在 import 被测模块之前注入）====================


class MediaType(Enum):
    """宿主 ``app.schemas.types.MediaType`` 的最小替身（取值口径一致）。"""

    MOVIE = "电影"
    TV = "电视剧"
    UNKNOWN = "未知"


class MediaSource(Enum):
    """宿主 ``app.schemas.types.MediaSource`` 的最小替身（只要 ``Douban``）。"""

    Douban = "douban"
    TheMovieDb = "themoviedb"


class _Logger:
    """吞掉日志的假 logger；各档消息都记下来供断言。"""

    def __init__(self) -> None:
        self.debugs: list = []
        self.infos: list = []
        self.warnings: list = []

    def debug(self, msg: str) -> None:
        self.debugs.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.warnings.append(msg)


class DomUtils:
    """宿主 ``app.sdk.utilities.DomUtils`` 的最小替身：真的去读 minidom 节点的文本。

    被测代码用 ``DomUtils.tag_value(item, "description", default="")`` 取正文，故这里只需
    支持 CDATA / 文本节点两种形态，且**不能**把标签本身也带出来。
    """

    @staticmethod
    def tag_value(node, tag, default=None, **_kwargs):
        found = node.getElementsByTagName(tag)
        if not found:
            return default
        child = found[0].firstChild
        if child is None:
            return default
        value = getattr(child, "data", None)
        if value is None:
            value = child.nodeValue
        return default if value is None else value


class _RequestUtils:
    """假 HTTP 入口：默认拒绝构造，用例要用时自行 monkeypatch 掉。"""

    def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - 不会被构造
        raise AssertionError("测试不应发起真实网络请求")


# 本文件新建的替身模块名（只记自己建的，用完要还回去，见 _release_host_stubs）。
_CREATED_STUBS: list = []


def _module(name: str, parent: types.ModuleType | None = None) -> types.ModuleType:
    """取出或创建 ``sys.modules`` 里的模块，并挂到父模块属性上（幂等、追加式）。"""
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        _CREATED_STUBS.append(name)
    if parent is not None:
        setattr(parent, name.rsplit(".", 1)[-1], mod)
    return mod


def _install_host_stubs() -> None:
    """注入被测模块需要的最小 ``app.*`` 替身（幂等；已在的不覆盖）。"""
    app = _module("app")
    schemas = _module("app.schemas", app)
    schema_types = _module("app.schemas.types", schemas)
    if not hasattr(schema_types, "MediaType"):
        schema_types.MediaType = MediaType
    if not hasattr(schema_types, "MediaSource"):
        schema_types.MediaSource = MediaSource
    sdk = _module("app.sdk", app)
    sdk_config = _module("app.sdk.config", sdk)
    if not hasattr(sdk_config, "settings"):
        sdk_config.settings = types.SimpleNamespace(PROXY=None)
    sdk_logging = _module("app.sdk.logging", sdk)
    if not hasattr(sdk_logging, "logger"):
        sdk_logging.logger = _Logger()
    if not hasattr(sdk_logging.logger, "warn"):  # 豆瓣用 logger.warn
        sdk_logging.logger.warn = sdk_logging.logger.warning
    sdk_network = _module("app.sdk.network", sdk)
    if not hasattr(sdk_network, "RequestUtils"):
        sdk_network.RequestUtils = _RequestUtils
    sdk_utilities = _module("app.sdk.utilities", sdk)
    if not hasattr(sdk_utilities, "DomUtils"):
        sdk_utilities.DomUtils = DomUtils


def _release_host_stubs() -> None:
    """把**本文件新建的**替身模块从 ``sys.modules`` 摘掉，让其它测试文件能装自己那套。

    为什么必须还：``core/filters._IMDB_TYPES_BY_MEDIA_TYPE`` 是**以 ``MediaType`` 类对象为键**
    的 dict（``app.schemas.types.MediaType`` → 允许的 IMDb ``titleType`` 集合），查不到时落到
    宽松兜底。``test_imdb_filters.py`` 的用例用**它自己那个** ``MediaType`` 类构造 ``mediainfo``，
    若 ``core.filters`` 在导入时绑定的是本文件另建的类，``.get()`` 就会全部落空、
    「候选类型按识别类型限定」那条用例随之退化到兜底而失败——两个测试文件谁先被导入谁说了算，
    纯靠收集顺序是靠不住的。

    故本文件只在**导入被测模块期间**占用替身，随后立刻归还：之后 ``test_imdb_filters.py``
    导入时 ``app.schemas.types`` 不在 ``sys.modules`` 里，它会照常安装自己那套（本文件的
    ``douban`` 模块早已把用到的对象绑定在模块属性上，摘掉不影响后续用例）。反过来，若它先
    导入、替身已就位，这里就只补齐缺的（``app.sdk.utilities`` 等）、且不摘别人建的东西。
    """
    for name in reversed(_CREATED_STUBS):
        sys.modules.pop(name, None)
    _CREATED_STUBS.clear()


_install_host_stubs()

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PKG = _PLUGIN_ROOT.name  # "automaticsubscriptionassistant"


def _ensure_package_shim() -> None:
    """把插件目录注册成「空壳包」（只给 ``__path__``，不执行插件 ``__init__.py``）。"""
    if _PLUGIN_PKG not in sys.modules:
        package = types.ModuleType(_PLUGIN_PKG)
        package.__path__ = [str(_PLUGIN_ROOT)]
        sys.modules[_PLUGIN_PKG] = package
    providers_name = f"{_PLUGIN_PKG}.providers"
    if providers_name not in sys.modules:
        providers = types.ModuleType(providers_name)
        providers.__path__ = [str(_PLUGIN_ROOT / "providers")]
        sys.modules[providers_name] = providers


_ensure_package_shim()

douban = importlib.import_module(f"{_PLUGIN_PKG}.providers.douban")
_models = importlib.import_module(f"{_PLUGIN_PKG}.core.models")
RankMediaItem = _models.RankMediaItem

# 被测模块已把用到的宿主对象绑定在自身模块属性上（douban.logger / douban.RequestUtils…），
# 故替身用完即还，避免与其它测试文件的替身「两套 MediaType 类」打架。
_release_host_stubs()

Provider = douban.DoubanRankProvider


# ==================== 构造真实的 minidom <item> 节点 ====================


def _dom_item(description: str = None, title: str = "某剧",
              link: str = "https://movie.douban.com/subject/1234567/") -> object:
    """按 RSS 的真实形态（正文是 CDATA）构造一个 ``<item>`` DOM 节点。"""
    parts = [f"<title>{title}</title>", f"<link>{link}</link>"]
    if description is not None:
        parts.append(f"<description><![CDATA[{description}]]></description>")
    # 变量名不要叫 xml：会遮蔽上面 import 的 xml.dom.minidom。
    document = f"<item>{''.join(parts)}</item>"
    return xml.dom.minidom.parseString(document).documentElement


def _mk_item(title: str, regions=None) -> RankMediaItem:
    """构造一条最小条目（只关心标题与地区）。"""
    return RankMediaItem(title=title, unique_seed=f"seed_{title}", regions=regions)


# 两种真实形态的信息行（第 2 段恒为出品地区）。
_INFO_A = ("<p>标签：2026 / 美国 / 剧情 悬疑 惊悚 犯罪 / 迈克尔·肖沃特 / "
           "安妮·海瑟薇 达科塔·约翰逊</p>")
_INFO_B = "<p>我的有罪之人</p><p>8.6</p><p>2026 / 韩国 / 剧情 喜剧 爱情 / 朴丹熙 / 任时完 薛仁雅</p>"


# ==================== 1. _parse_regions：正文信息行的地区解析 ====================


def test_parse_regions_builtin_rank_format():
    """内置榜格式（带「标签：」前缀）能解析出地区。"""
    assert Provider._parse_regions(_dom_item(_INFO_A)) == ["美国"]


def test_parse_regions_list_rank_format():
    """列表榜格式（无前缀、前面还有标题行与裸评分行）能解析出地区。"""
    assert Provider._parse_regions(_dom_item(_INFO_B)) == ["韩国"]


@pytest.mark.parametrize("info_line,expected", [
    ("2026 / 西班牙 法国 / 剧情", ["西班牙", "法国"]),
    ("2026 / 德国 法国 美国 / 剧情 惊悚", ["德国", "法国", "美国"]),
    ("2026 / 中国大陆 中国香港 / 剧情", ["中国大陆", "中国香港"]),
    ("2026 / 法国 日本 德国 比利时 / 剧情", ["法国", "日本", "德国", "比利时"]),
])
def test_parse_regions_multiple_regions_split_by_space(info_line, expected):
    """多个产地用空格分隔 → 拆成列表，内容与顺序正确。"""
    assert Provider._parse_regions(_dom_item(f"<p>标签：{info_line}</p>")) == expected


def test_parse_regions_tolerates_missing_cast_segment():
    """段数不固定（综艺条目可能没有主演）也不影响：地区恒取第 2 段。"""
    dom = _dom_item("<p>标签：2026 / 韩国 / 真人秀</p>")
    assert Provider._parse_regions(dom) == ["韩国"]


def test_parse_regions_dedups_and_keeps_order():
    """同一地区写多遍只留一个（去重保序）。"""
    dom = _dom_item("<p>标签：2026 / 韩国,韩国，韩国 / 剧情</p>")
    assert Provider._parse_regions(dom) == ["韩国"]


def test_parse_regions_ignores_title_vote_and_img():
    """标题行、裸评分行、``<img src>``（哪怕里面有年份和斜杠）都不被误判。"""
    dom = _dom_item(
        "<p>我的有罪之人</p><p>8.6</p>"
        '<img src="https://img1.doubanio.com/view/photo/2026 / 韩国 / x.jpg"/>'
        "<p>2026 / 韩国 / 剧情 喜剧 爱情 / 朴丹熙 / 任时完 薛仁雅</p>"
    )
    assert Provider._parse_regions(dom) == ["韩国"]


def test_parse_regions_ignores_img_only_body():
    """正文里只有海报与评分、没有信息行 → None（``<img>`` 里的年份+斜杠不算信息行）。"""
    dom = _dom_item('<p>某剧</p><p>8.6</p>'
                    '<img src="https://example.com/2026 / 韩国 / poster.jpg"/>')
    assert Provider._parse_regions(dom) is None


def test_parse_regions_title_with_year_is_not_info_line():
    """标题里带年份但不是「年份 / 地区」形态 → None。"""
    assert Provider._parse_regions(_dom_item("<p>1972 年的夏天</p><p>8.6</p>")) is None


def test_parse_regions_without_description_is_none():
    """完全没有 description（或正文为空）→ None。"""
    assert Provider._parse_regions(_dom_item(None)) is None
    assert Provider._parse_regions(_dom_item("")) is None


def test_parse_item_fills_regions_and_keeps_vote():
    """``_parse_item`` 把地区写进条目；列表榜的裸评分仍由既有 ``_parse_vote`` 解析（未被改动）。"""
    item = Provider()._parse_item(_dom_item(_INFO_B))
    assert item is not None
    assert item.regions == ["韩国"]
    assert item.source_vote == 8.6
    assert item.douban_id == "1234567"


def test_parse_item_without_regions_is_none():
    """无信息行的条目照旧能被解析出来，只是 regions 为 None。"""
    item = Provider()._parse_item(_dom_item("<p>某剧</p><p>8.6</p>"))
    assert item is not None
    assert item.regions is None


# ==================== 2. _parse_extra_regions：两个配置字段合并成集合 ====================


def test_parse_extra_regions_merges_list_and_text():
    """多选列表 + 文本框取并集。"""
    regions = Provider._parse_extra_regions({
        "extra_regions": ["韩国", "美国"],
        "extra_regions_text": "日本\n中国大陆",
    })
    assert regions == {"韩国", "美国", "日本", "中国大陆"}


@pytest.mark.parametrize("text", [
    "韩国,美国", "韩国，美国", "韩国、美国", "韩国／美国", "韩国\n美国",
])
def test_parse_extra_regions_text_separators(text):
    """文本框支持 换行 / 逗号 / 顿号 / 全角斜杠 分隔。"""
    regions = Provider._parse_extra_regions({"extra_regions_text": text})
    assert regions == {"韩国", "美国"}


def test_parse_extra_regions_text_multiple_lines_with_spaces():
    """多行文本 + 行内空格分隔都能拆开，且去空、去重。"""
    regions = Provider._parse_extra_regions(
        {"extra_regions_text": "  西班牙 法国 \n\n中国香港\n西班牙\n"})
    assert regions == {"西班牙", "法国", "中国香港"}


def test_parse_extra_regions_accepts_comma_string_for_multiselect():
    """多选值被存成逗号分隔字符串时同样能解析。"""
    assert Provider._parse_extra_regions({"extra_regions": "韩国,美国"}) == {"韩国", "美国"}


@pytest.mark.parametrize("options", [
    {}, {"extra_regions": [], "extra_regions_text": ""}, None,
    {"extra_regions": None, "extra_regions_text": None}, {"extra_regions_text": "  \n  "},
])
def test_parse_extra_regions_empty(options):
    """都没填（或只有空白）→ 空集合 = 不扩量。"""
    assert Provider._parse_extra_regions(options) == set()


# ==================== 3. _apply_extra_regions：叠加式取舍语义（核心）====================


def _titles(items):
    """取标题列表，便于断言顺序。"""
    return [item.title for item in items]


def test_apply_without_extra_regions_equals_current_behavior():
    """扩量地区为空 ⇒ 与现状完全一致：limit>0 取前 N 条。"""
    items = [_mk_item(str(i)) for i in range(5)]
    assert _titles(Provider._apply_extra_regions(items, 1, 0, set())) == ["0"]
    assert _titles(Provider._apply_extra_regions(items, 3, 0, set())) == ["0", "1", "2"]


def test_apply_without_extra_regions_and_no_limit_returns_all():
    """扩量地区为空 + limit=0（不限）⇒ 原样返回全部。"""
    items = [_mk_item(str(i)) for i in range(5)]
    assert _titles(Provider._apply_extra_regions(items, 0, 0, set())) == _titles(items)


def test_apply_shallow_ignores_region_and_deep_filters_by_region():
    """核心场景：limit=1、扩量取数=3、地区={韩国} → 第 1 条无论哪国都留，第 2~3 条只留韩国。"""
    items = [
        _mk_item("榜首国产剧", ["中国大陆"]),   # 浅位：不看国家，保留
        _mk_item("第二名韩剧", ["韩国"]),       # 深位：命中，保留
        _mk_item("第三名美剧", ["美国"]),       # 深位：未命中，丢弃
        _mk_item("第四名韩剧", ["韩国"]),       # 超出深度 3，丢弃
    ]
    kept = Provider._apply_extra_regions(items, 1, 3, {"韩国"})
    assert _titles(kept) == ["榜首国产剧", "第二名韩剧"]


def test_apply_depth_is_max_of_default_and_extra():
    """取数深度 = max(默认, 扩量)：扩量比默认小 → 与只取前 N 等价。"""
    items = [
        _mk_item("a", ["韩国"]), _mk_item("b", ["韩国"]),
        _mk_item("c", ["韩国"]), _mk_item("d", ["韩国"]),
    ]
    # extra_limit=1 < default_limit=2 → 深度 2，第 3 条即便命中韩国也不进。
    assert _titles(Provider._apply_extra_regions(items, 2, 1, {"韩国"})) == ["a", "b"]
    # extra_limit=4 > default_limit=2 → 深度 4，第 3~4 条命中即并入。
    assert _titles(Provider._apply_extra_regions(items, 2, 4, {"韩国"})) == ["a", "b", "c", "d"]


def test_apply_no_limit_returns_all_even_with_extra_regions():
    """limit<=0（不限）⇒ 扩量配置不生效，原样返回全部。"""
    items = [_mk_item("a", ["中国大陆"]), _mk_item("b", ["美国"]), _mk_item("c")]
    assert _titles(Provider._apply_extra_regions(items, 0, 3, {"韩国"})) == ["a", "b", "c"]


@pytest.mark.parametrize("regions", [
    ["德国", "法国", "美国"], ["美国"], ["法国", "美国", "德国"],
])
def test_apply_multi_region_item_hits_any(regions):
    """多产地条目命中其中任一个即保留。"""
    items = [_mk_item("榜首", ["中国大陆"]), _mk_item("第二名", regions)]
    assert _titles(Provider._apply_extra_regions(items, 1, 3, {"美国"})) == ["榜首", "第二名"]


def test_apply_deep_item_without_regions_is_dropped_shallow_kept():
    """深位 regions=None → 不保留（无法证明命中）；浅位 regions=None → 照旧保留。"""
    items = [
        _mk_item("浅位无地区", None),
        _mk_item("深位无地区", None),
        _mk_item("深位韩国", ["韩国"]),
    ]
    assert _titles(Provider._apply_extra_regions(items, 1, 3, {"韩国"})) == [
        "浅位无地区", "深位韩国"]


def test_apply_preserves_input_order():
    """返回顺序与输入一致（浅位在前、深位命中的按原名次追加）。"""
    items = [
        _mk_item("r1", ["中国大陆"]), _mk_item("r2", ["韩国"]), _mk_item("r3", ["美国"]),
        _mk_item("r4", ["韩国"]), _mk_item("r5", ["韩国"]),
    ]
    assert _titles(Provider._apply_extra_regions(items, 1, 5, {"韩国"})) == ["r1", "r2", "r4", "r5"]


def test_apply_does_not_mutate_input():
    """纯函数：不改入参列表、不改入参条目对象，返回的是新列表。"""
    items = [_mk_item("a", ["美国"]), _mk_item("b", ["韩国"]), _mk_item("c", None)]
    snapshot = [(item.title, item.regions) for item in items]
    original = list(items)

    kept = Provider._apply_extra_regions(items, 1, 3, {"韩国"})

    assert kept is not items
    assert items == original
    assert [(item.title, item.regions) for item in items] == snapshot


# ==================== 4. _fetch_addr：抓取阶段的接线、日志与产出 ====================


def _rss(*descriptions) -> str:
    """按真实 RSS 形态拼一段榜单文档（顺序即榜单名次）。

    ``<title>`` 是条目名（``剧N``），正文里的首个 ``<p>`` 才是剧名——与真实榜单一致。
    """
    items = "".join(
        f'<item><title>剧{index}</title>'
        f'<link>https://movie.douban.com/subject/{index}/</link>'
        f"<description><![CDATA[{desc}]]></description></item>"
        for index, desc in enumerate(descriptions, start=1)
    )
    return f"<rss><channel>{items}</channel></rss>"


def _install_fake_rss(monkeypatch, text):
    """把 ``RequestUtils`` 换成返回固定文本的假实现，并记录调用信息。"""
    captured = {}

    class FakeRequestUtils:
        def __init__(self, **kwargs) -> None:
            captured["kwargs"] = kwargs

        def get_res(self, addr):
            captured["addr"] = addr
            return types.SimpleNamespace(text=text)

    monkeypatch.setattr(douban, "RequestUtils", FakeRequestUtils)
    return captured


_LIST = (
    "<p>榜首国产剧</p><p>8.6</p><p>2026 / 中国大陆 / 剧情 / 导演A / 主演A</p>",
    "<p>第二名韩剧</p><p>8.4</p><p>2026 / 韩国 / 剧情 / 导演B / 主演B</p>",
    "<p>第三名美剧</p><p>8.2</p><p>2026 / 美国 / 剧情 / 导演C / 主演C</p>",
)


def test_fetch_addr_applies_extra_regions(monkeypatch):
    """limit=1 + 扩量取数=3 + 地区={韩国} → 第 1 条照旧、第 2 条命中并入、第 3 条丢弃。"""
    _install_fake_rss(monkeypatch, _rss(*_LIST))
    out = list(Provider()._fetch_addr("http://x/rss", False, 1, None,
                                      extra_limit=3, extra_regions={"韩国"}))
    assert [item.title for item in out] == ["剧1", "剧2"]
    assert out[1].regions == ["韩国"]


def test_fetch_addr_logs_extra_hit(monkeypatch):
    """扩量生效时补一条日志，并沿用既有的「共 N 条 / 已按每榜取前 N 条」两条。"""
    _install_fake_rss(monkeypatch, _rss(*_LIST))
    douban.logger.infos.clear()
    list(Provider()._fetch_addr("http://x/rss", False, 1, None,
                                extra_limit=3, extra_regions={"韩国"}))
    assert any("共 3 条数据" in msg for msg in douban.logger.infos)
    assert any("已按每榜取前 1 条" in msg for msg in douban.logger.infos)
    assert any("扩量取数 3" in msg and "排名 2~3 中地区命中的 1 条已并入" in msg
               for msg in douban.logger.infos)


def test_fetch_addr_without_extra_regions_keeps_old_logs(monkeypatch):
    """没配扩量 → 行为与日志都保持原样（不出现任何「扩量」字样）。"""
    _install_fake_rss(monkeypatch, _rss(*_LIST))
    douban.logger.infos.clear()
    out = list(Provider()._fetch_addr("http://x/rss", False, 1))
    assert [item.title for item in out] == ["剧1"]
    assert not any("扩量" in msg for msg in douban.logger.infos)


def test_fetch_addr_depth_logged_even_without_hit(monkeypatch):
    """扩量开了但深位一条都没命中 → 照样报「0 条已并入」（便于排查配置）。"""
    _install_fake_rss(monkeypatch, _rss(*_LIST))
    douban.logger.infos.clear()
    out = list(Provider()._fetch_addr("http://x/rss", False, 1, None,
                                      extra_limit=3, extra_regions={"法国"}))
    assert [item.title for item in out] == ["剧1"]
    assert any("中地区命中的 0 条已并入" in msg for msg in douban.logger.infos)


def test_fetch_addr_no_limit_ignores_extra_regions(monkeypatch):
    """limit=0（不限）⇒ 全部返回，扩量配置不生效、也不打扩量日志。"""
    _install_fake_rss(monkeypatch, _rss(*_LIST))
    douban.logger.infos.clear()
    out = list(Provider()._fetch_addr("http://x/rss", False, 0, None,
                                      extra_limit=1, extra_regions={"韩国"}))
    assert [item.title for item in out] == ["剧1", "剧2", "剧3"]
    assert not any("扩量" in msg for msg in douban.logger.infos)


def test_fetch_addr_parses_vote_and_regions_end_to_end(monkeypatch):
    """顺带回归：判分与地区都在抓取阶段落到条目上。"""
    _install_fake_rss(monkeypatch, _rss(_LIST[0]))
    out = list(Provider()._fetch_addr("http://x/rss", False, 0))
    assert out[0].source_vote == 8.6
    assert out[0].regions == ["中国大陆"]


def test_fetch_wires_options_into_fetch_addr(monkeypatch):
    """``fetch`` 从 options 读出扩量配置并传给 ``_fetch_addr``（端到端接线）。"""
    _install_fake_rss(monkeypatch, _rss(*_LIST))
    options = {
        "ranks": ["tv-hot"],
        "limit": 1,
        "extra_limit": 3,
        "extra_regions": ["韩国"],
        "extra_regions_text": "",
    }
    out = list(Provider().fetch(options, None))
    assert [item.title for item in out] == ["剧1", "剧2"]


def test_fetch_without_extra_options_behaves_as_before(monkeypatch):
    """用户真实配置（无扩量字段）→ 每榜只取前 1 条，与升级前完全一致。"""
    _install_fake_rss(monkeypatch, _rss(*_LIST))
    options = {"ranks": ["tv-hot"], "limit": 1, "extra_regions": [], "extra_regions_text": ""}
    out = list(Provider().fetch(options, None))
    assert [item.title for item in out] == ["剧1"]


# ==================== 5. schema：三个新字段就位且顺序正确 ====================


def test_spec_adds_extra_fields_after_limit():
    """新字段插在「每榜取前N」之后、「使用代理服务器」之前。"""
    spec = Provider().get_spec()
    keys = [field.key for field in spec.options_schema]
    # 三个新字段紧跟「每榜取前N」，且仍排在「使用代理服务器」之前（高级项夹在中间）。
    assert keys[:5] == ["ranks", "limit", "extra_regions", "extra_regions_text", "extra_limit"]
    assert keys.index("proxy") == len(keys) - 1
    assert "proxy" not in keys[:5]

    by_key = {field.key: field for field in spec.options_schema}
    assert by_key["extra_regions"].kind == "multi-select"
    assert by_key["extra_regions"].default == []
    assert by_key["extra_regions"].options
    assert by_key["extra_regions_text"].kind == "textarea"
    assert by_key["extra_regions_text"].default == ""
    assert by_key["extra_limit"].kind == "number"
    assert by_key["extra_limit"].default == 0


def test_region_options_are_chinese_names_with_same_title_and_value():
    """预置选项的 title/value 都是中文地区名（与榜单正文写法一致），且无重复。"""
    options = douban.DOUBAN_REGION_OPTIONS
    values = [opt["value"] for opt in options]
    assert len(values) == len(set(values))
    assert all(opt["title"] == opt["value"] for opt in options)
    assert {"中国大陆", "中国香港", "韩国", "美国", "法国"} <= set(values)


def test_new_labels_are_localized():
    """三个新 label 在 zh-TW / en-US 下都有译文（i18n 表已补）。"""
    i18n = importlib.import_module(f"{_PLUGIN_PKG}.core.i18n")
    for label in ("扩量地区", "其它扩量地区", "扩量取数"):
        assert label in i18n._TEXT
        assert i18n._TEXT[label]["zh-TW"]
        assert i18n._TEXT[label]["en-US"]


def test_localized_spec_keeps_new_field_values():
    """本地化只换展示文本，不动 key/kind/default/options。"""
    i18n = importlib.import_module(f"{_PLUGIN_PKG}.core.i18n")
    spec = Provider().get_spec().to_dict()
    localized = i18n.localize_spec(spec, "en-US")
    by_key = {field["key"]: field for field in localized["options_schema"]}
    assert by_key["extra_regions"]["label"] == "Extra regions"
    assert by_key["extra_regions"]["kind"] == "multi-select"
    assert by_key["extra_regions"]["options"][0]["value"] == douban.DOUBAN_REGION_OPTIONS[0]["value"]
    assert by_key["extra_limit"]["label"] == "Extra fetch depth"
    assert by_key["extra_limit"]["default"] == 0
    # 中文地区名在 _country_title 里查不到 iso2 → 原样透传（可接受）。
    assert by_key["extra_regions"]["options"][0]["title"] == douban.DOUBAN_REGION_OPTIONS[0]["value"]
