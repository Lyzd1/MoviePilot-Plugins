"""``imdb_vote`` / ``imdb_votes`` 两个 post 过滤器的单元测试。

本仓库在宿主机之外无法 ``import app.*``，故文件顶部先用 ``sys.modules`` 注入最小替身
（``app`` / ``app.schemas`` / ``app.schemas.types`` / ``app.sdk`` / ``app.sdk.config`` /
``app.sdk.logging`` / ``app.sdk.network``），再导入被测模块。**测试全程不打真实网络**：
真正的网络入口 ``core/filters._fetch_imdb_lookup`` 要么被 monkeypatch 成假的查询实现，
要么让它跑在假的 ``ImdbApi``（``filters._imdb_api``）之上。

刻意**不放** ``tests/__init__.py``：避免被 MoviePilot 当成插件子包加载。

运行：``python3 -m pytest plugins.v3/automaticsubscriptionassistant/tests/test_imdb_filters.py -q``
"""
from __future__ import annotations

import importlib
import sys
import types
from enum import Enum
from pathlib import Path

import pytest


# ==================== 宿主最小替身（必须在 import 被测模块之前注入）====================


class MediaType(Enum):
    """宿主 ``app.schemas.types.MediaType`` 的最小替身（取值口径一致）。"""

    MOVIE = "电影"
    TV = "电视剧"
    UNKNOWN = "未知"


def _install_host_stubs() -> None:
    """注入被测模块需要的最小 ``app.*`` 替身（幂等）。"""
    if "app.schemas.types" in sys.modules:
        return

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

        def warning(self, msg: str) -> None:
            self.warnings.append(msg)

        def error(self, msg: str) -> None:
            self.warnings.append(msg)

    class _RequestUtils:
        """假 HTTP 入口：本测试不发起任何请求。"""

        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - 不会被构造
            raise AssertionError("测试不应发起真实网络请求")

    app = types.ModuleType("app")
    schemas = types.ModuleType("app.schemas")
    schema_types = types.ModuleType("app.schemas.types")
    schema_types.MediaType = MediaType
    sdk = types.ModuleType("app.sdk")
    sdk_config = types.ModuleType("app.sdk.config")
    sdk_config.settings = types.SimpleNamespace(PROXY=None)
    sdk_logging = types.ModuleType("app.sdk.logging")
    sdk_logging.logger = _Logger()
    sdk_network = types.ModuleType("app.sdk.network")
    sdk_network.RequestUtils = _RequestUtils

    app.schemas = schemas
    app.sdk = sdk
    sys.modules["app"] = app
    sys.modules["app.schemas"] = schemas
    sys.modules["app.schemas.types"] = schema_types
    sys.modules["app.sdk"] = sdk
    sys.modules["app.sdk.config"] = sdk_config
    sys.modules["app.sdk.logging"] = sdk_logging
    sys.modules["app.sdk.network"] = sdk_network


_install_host_stubs()

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PKG = _PLUGIN_ROOT.name  # "automaticsubscriptionassistant"


def _ensure_package_shim() -> None:
    """把插件目录注册成「空壳包」（只给 ``__path__``，不执行插件 ``__init__.py``）。

    插件内部用 ``from ..core.models import ...`` 这类**相对导入**，故必须以「包」的身份被
    导入；而真实 ``__init__.py`` 是插件入口，会拉起 apscheduler / pytz / ``app.*`` 一整套
    宿主依赖。tests/conftest.py 已做同样的事（pytest ≥ 9 会把插件目录当成测试文件的外层包
    去导入它），这里再兜一次，保证单独运行本文件也能工作。
    """
    if _PLUGIN_PKG not in sys.modules:
        package = types.ModuleType(_PLUGIN_PKG)
        package.__path__ = [str(_PLUGIN_ROOT)]
        sys.modules[_PLUGIN_PKG] = package
    # ``providers`` 同理只给包壳：执行 providers/__init__.py 会把豆瓣/猫眼等来源一并导入，
    # 它们的宿主依赖（DomUtils、PlaywrightHelper…）不在本文件的替身范围内。
    providers_name = f"{_PLUGIN_PKG}.providers"
    if providers_name not in sys.modules:
        providers = types.ModuleType(providers_name)
        providers.__path__ = [str(_PLUGIN_ROOT / "providers")]
        sys.modules[providers_name] = providers


_ensure_package_shim()

filters = importlib.import_module(f"{_PLUGIN_PKG}.core.filters")
_models = importlib.import_module(f"{_PLUGIN_PKG}.core.models")
FilterVerdict = _models.FilterVerdict
RankMediaItem = _models.RankMediaItem
netflix = importlib.import_module(f"{_PLUGIN_PKG}.providers.netflix")


class FakeMediaInfo:
    """``MediaInfo`` 的最小替身：过滤器只读 imdb_id / 各标题字段 / type / year。"""

    def __init__(self, title="Fake", en_title=None, original_title=None,
                 imdb_id=None, mtype=MediaType.TV, year=None):
        self.title = title
        self.en_title = en_title
        self.original_title = original_title
        self.imdb_id = imdb_id
        self.type = mtype
        self.year = year


class fake_api:
    """假的宿主 IMDb 客户端：搜索返回固定候选，详情按 ID 查表并记录调用。

    宿主真实的 ``search_titles`` 结果里 ``rating`` 恒为 None（评分只能另查 ``get_title``），
    本替身刻意保持该特性——若被测代码漏掉详情查询，评分就会是 None 而不是编造的值。
    """

    def __init__(self, titles=None, detail=None):
        self.titles = list(titles or [])
        self.detail = dict(detail or {})
        self.search_calls: list = []
        self.detail_calls: list = []

    def search_titles(self, query, limit=8):
        self.search_calls.append((query, limit))
        return self.titles[:limit]

    def get_title(self, imdb_id):
        self.detail_calls.append(imdb_id)
        lookup = self.detail.get(imdb_id)
        if lookup is None:
            return None
        return types.SimpleNamespace(
            id=imdb_id,
            type="tvSeries",
            rating=types.SimpleNamespace(
                aggregate_rating=lookup.rating, vote_count=lookup.votes),
        )


def imdb_titles(*specs):
    """构造搜索候选（``rating`` 恒为 None，与宿主一致）。"""
    return [
        types.SimpleNamespace(
            id=imdb_id, type=title_type, primary_title=primary,
            original_title=original, start_year=year,
            rating=None)
        for imdb_id, title_type, primary, original, year in specs
    ]


@pytest.fixture(autouse=True)
def _clean_imdb_cache():
    """每个用例前后清空模块级查询缓存，避免用例之间互相串结果。"""
    filters._IMDB_CACHE.clear()
    yield
    filters._IMDB_CACHE.clear()


@pytest.fixture()
def spy(monkeypatch):
    """把网络入口 ``_fetch_imdb_lookup`` 换成假实现：记录调用次数与入参。

    ``spy.result`` 给定返回值，``spy.raises`` 给定要抛的异常。
    """
    calls = {"count": 0, "queries": [], "ids": []}

    class _Spy:
        result = filters.ImdbLookup(matched=False)
        raises = None

        def __call__(self, mediainfo, imdb_id, query):
            calls["count"] += 1
            calls["queries"].append(query)
            calls["ids"].append(imdb_id)
            if self.raises is not None:
                raise self.raises
            return self.result

    spy_obj = _Spy()
    spy_obj.calls = calls
    monkeypatch.setattr(filters, "_fetch_imdb_lookup", spy_obj)
    return spy_obj


@pytest.fixture()
def install_api(monkeypatch):
    """把宿主 IMDb 客户端替换为 ``fake_api``（走真实的 ``_fetch_imdb_lookup`` 逻辑）。"""

    def _install(titles=None, detail=None):
        api = fake_api(titles, detail)
        monkeypatch.setattr(filters, "_imdb_api", lambda: api)
        return api

    return _install


def _item(title="Some Show", mtype=MediaType.TV):
    """构造一条最小的 ``RankMediaItem``（过滤器不使用它，仅保持签名完整）。"""
    return RankMediaItem(title=title, type_hint=mtype, unique_seed=f"tv_{title}")


def _accepts(filter_cls, mediainfo, config):
    """跑一次过滤器，返回裁决。"""
    return filter_cls().accept(_item(), mediainfo, config)


def _lookup(rating=None, votes=None, matched=True, imdb_id="tt1234567"):
    """构造一个查询结果。"""
    return filters.ImdbLookup(matched=matched, imdb_id=imdb_id, rating=rating, votes=votes)


# ==================== 1. 未启用（阈值 ≤ 0 / 空 / 非法）不查询 ====================


@pytest.mark.parametrize("filter_id", ["imdb_vote", "imdb_votes"])
@pytest.mark.parametrize("config", [
    {"imdb_vote": 0, "imdb_votes": 0}, {}, {"imdb_vote": None, "imdb_votes": None},
])
def test_disabled_threshold_passes_without_querying(spy, filter_id, config):
    """阈值为 0 / 缺失 / None → 放行，且**一次 IMDb 查询都不发**。"""
    mediainfo = FakeMediaInfo(title="Some Show", imdb_id="tt1234567")
    verdict = _accepts(filters.BUILTIN_FILTERS[filter_id], mediainfo, config)
    assert verdict.accepted is True
    assert spy.calls["count"] == 0


def test_illegal_threshold_passes_without_querying(spy):
    """非法阈值（非数字串）同样视为未启用。"""
    verdict = _accepts(
        filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_vote": "abc"})
    assert verdict.accepted is True
    assert spy.calls["count"] == 0


# ==================== 2. imdb_vote：评分判定 ====================


def test_imdb_vote_accepts_when_rating_reaches_threshold(spy):
    """IMDb 评分 8.3 ≥ 8 → 放行（按 imdb_id 直接查详情）。"""
    spy.result = _lookup(rating=8.3, votes=30000)
    verdict = _accepts(
        filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_vote": 8})
    assert verdict.accepted is True
    assert spy.calls["count"] == 1
    assert spy.calls["ids"] == ["tt1234567"]


def test_imdb_vote_rejects_low_rating(spy):
    """IMDb 评分 5.2 < 8 → 拒绝，reason 带出实际分数。"""
    spy.result = _lookup(rating=5.2, votes=1189)
    verdict = _accepts(
        filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_vote": 8})
    assert verdict.accepted is False
    assert verdict.filter_id == "imdb_vote"
    assert verdict.reason == "IMDb 评分 5.2 < 8.0"


# ==================== 3. imdb_votes：评分人数判定 ====================


def test_imdb_votes_rejects_few_votes(spy):
    """评分人数 1189 < 20000 → 拒绝。"""
    spy.result = _lookup(rating=8.3, votes=1189)
    verdict = _accepts(
        filters.ImdbVoteCountFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_votes": 20000})
    assert verdict.accepted is False
    assert verdict.filter_id == "imdb_votes"
    assert verdict.reason == "IMDb 评分人数 1189 < 20000"


def test_imdb_votes_accepts_enough_votes(spy):
    """评分人数 30000 ≥ 20000 → 放行（评分低不影响人数过滤器）。"""
    spy.result = _lookup(rating=7.1, votes=30000)
    verdict = _accepts(
        filters.ImdbVoteCountFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_votes": 20000})
    assert verdict.accepted is True


def test_imdb_votes_rejects_missing_vote_count(spy):
    """无评分人数（None）按不达标处理。"""
    spy.result = _lookup(rating=8.3, votes=None)
    verdict = _accepts(
        filters.ImdbVoteCountFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_votes": 20000})
    assert verdict.accepted is False
    assert verdict.reason == "IMDb 评分人数 0 < 20000"


# ==================== 4. 无 imdb_id：标题搜索兜底 ====================


def test_searches_by_title_when_imdb_id_missing(install_api):
    """没有 imdb_id → 用 en_title 搜索，命中候选后再查详情读评分。"""
    api = install_api(
        imdb_titles(("tt9999999", "tvSeries", "Wednesday", "Wednesday", 2022)),
        {"tt9999999": _lookup(rating=8.3, votes=30000, imdb_id="tt9999999")},
    )
    mediainfo = FakeMediaInfo(title="星期三", en_title="Wednesday",
                              original_title="Wednesday", mtype=MediaType.TV, year="2022")
    verdict = _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 8})
    assert verdict.accepted is True
    assert api.search_calls == [("Wednesday", 8)]
    assert api.detail_calls == ["tt9999999"]  # 评分必须另查详情


def test_search_prefers_candidate_closest_to_year(install_api):
    """多条同名命中 → 取 start_year 与 mediainfo.year 最接近的一条。"""
    api = install_api(
        imdb_titles(("tt1111111", "movie", "Dune", "Dune", 1984),
                    ("tt2222222", "movie", "Dune", "Dune", 2021)),
        {"tt1111111": _lookup(rating=6.0, votes=50000, imdb_id="tt1111111"),
         "tt2222222": _lookup(rating=8.0, votes=900000, imdb_id="tt2222222")},
    )
    mediainfo = FakeMediaInfo(title="沙丘", en_title="Dune", mtype=MediaType.MOVIE, year="2021")
    verdict = _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 7.5})
    assert verdict.accepted is True  # 命中 2021 版（8.0），而非 1984 版（6.0）
    assert api.detail_calls == ["tt2222222"]


def test_search_prefers_latest_year_when_mediainfo_has_no_year(install_api):
    """mediainfo.year 缺失 → 取 start_year 最大的一条。"""
    api = install_api(
        imdb_titles(("tt1111111", "movie", "Dune", "Dune", 1984),
                    ("tt2222222", "movie", "Dune", "Dune", 2021)),
        {"tt1111111": _lookup(rating=6.0, votes=50000, imdb_id="tt1111111"),
         "tt2222222": _lookup(rating=8.0, votes=900000, imdb_id="tt2222222")},
    )
    mediainfo = FakeMediaInfo(title="沙丘", en_title="Dune", mtype=MediaType.MOVIE, year=None)
    assert _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 7.5}).accepted is True
    assert api.detail_calls == ["tt2222222"]


# ---------- 分级匹配（放宽后：精确 → 完整前缀 → 类型 + 年份兜底）----------


def test_prefix_relaxation_accepts_shorter_netflix_title(install_api):
    """前缀放宽：查询是候选标题前缀且查询 ≥ 5 字符时，长度比不达标也命中。

    奈飞榜单给简称、IMDb 用全称：``DAHMER`` → ``Dahmer – Monster: The Jeffrey Dahmer
    Story``（比值约 0.15）。干扰候选的年份与 mediainfo 完全一致，若前缀规则没先命中，
    兜底就会把那一条选中——故断言详情查询落在长标题条目上即可证明前缀规则生效。
    """
    api = install_api(
        imdb_titles(
            ("tt13207736", "tvSeries", "Dahmer – Monster: The Jeffrey Dahmer Story", None, 2022),
            ("tt9999999", "tvSeries", "Unrelated Show", None, 2025),
        ),
        {"tt13207736": _lookup(rating=8.0, votes=120000, imdb_id="tt13207736"),
         "tt9999999": _lookup(rating=3.0, votes=10, imdb_id="tt9999999")},
    )
    mediainfo = FakeMediaInfo(title="DAHMER", mtype=MediaType.TV, year="2025")
    assert _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 8}).accepted is True
    assert api.detail_calls == ["tt13207736"]


def test_prefix_ratio_guard_holds_for_short_query(install_api):
    """短查询串（< 5 字符）不享受前缀例外，交给兜底按年份定夺。

    ``It`` 是 ``It Chapter Two`` 的前缀，但比值 2/14 远低于 0.6 且查询串不足 5 字符，
    故前缀规则不命中；兜底按年份选中 2014 的 ``It Follows``。
    """
    api = install_api(
        imdb_titles(("tt1111111", "movie", "It Chapter Two", None, 2019),
                    ("tt2222222", "movie", "It Follows", None, 2014)),
        {"tt1111111": _lookup(rating=6.5, votes=200000, imdb_id="tt1111111"),
         "tt2222222": _lookup(rating=6.8, votes=300000, imdb_id="tt2222222")},
    )
    mediainfo = FakeMediaInfo(title="It", mtype=MediaType.MOVIE, year="2014")
    assert _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 6}).accepted is True
    assert api.detail_calls == ["tt2222222"]


def test_type_year_fallback_matches_whole_show_and_logs(install_api):
    """兜底：标题写法完全不同（候选是长查询的前缀、比值不达标）时按年份选，并记 debug 日志。

    ``Monster: The Lizzie Borden Story`` 在 IMDb 按整剧口径记在 ``Monster``（tt13207736）下。
    """
    api = install_api(
        imdb_titles(("tt13207736", "tvSeries", "Monster", None, 2022)),
        {"tt13207736": _lookup(rating=8.0, votes=120000, imdb_id="tt13207736")},
    )
    filters.logger.debugs.clear()
    mediainfo = FakeMediaInfo(
        title="Monster: The Lizzie Borden Story", mtype=MediaType.TV, year="2025")
    assert _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 8}).accepted is True
    assert api.detail_calls == ["tt13207736"]
    assert any("非精确匹配" in msg for msg in filters.logger.debugs)


def test_search_restricted_by_media_type(install_api):
    """候选类型按识别类型限定：剧集来源不会命中同名电影。"""
    api = install_api(
        imdb_titles(("tt1111111", "movie", "Fargo", "Fargo", 1996)),
        {"tt1111111": _lookup(rating=8.1, votes=700000, imdb_id="tt1111111")},
    )
    verdict = _accepts(
        filters.ImdbVoteFilter,
        FakeMediaInfo(title="冰血暴", en_title="Fargo", mtype=MediaType.TV, year="2014"),
        {"imdb_vote": 8})
    assert verdict.accepted is False
    assert verdict.reason == "IMDb 未匹配到条目"
    assert api.detail_calls == []


def test_search_matches_original_title_too(install_api):
    """候选的 ``original_title`` 与查询串同名也算命中（不只比 primary_title）。"""
    api = install_api(
        imdb_titles(("tt3333333", "tvSeries", "Squid Game", "오징어 게임", 2021)),
        {"tt3333333": _lookup(rating=8.0, votes=600000, imdb_id="tt3333333")},
    )
    mediainfo = FakeMediaInfo(title="鱿鱼游戏", en_title="오징어 게임",
                              mtype=MediaType.TV, year="2021")
    assert _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 8}).accepted is True
    assert api.detail_calls == ["tt3333333"]


def test_query_string_falls_back_to_original_then_title(install_api):
    """查询串优先级：en_title → original_title → title（前两个都空时用 title）。"""
    api = install_api(
        imdb_titles(("tt4444444", "tvSeries", "Some Show", "Some Show", 2020)),
        {"tt4444444": _lookup(rating=8.5, votes=40000, imdb_id="tt4444444")},
    )
    mediainfo = FakeMediaInfo(title="Some Show", en_title=None, original_title="")
    assert _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 8}).accepted is True
    assert api.search_calls == [("Some Show", 8)]


# ==================== 5. 搜索无匹配 ====================


def test_no_match_rejects(install_api):
    """搜索无任何同名候选 → 拒绝，reason 为「IMDb 未匹配到条目」。"""
    api = install_api([])
    mediainfo = FakeMediaInfo(title="不存在的剧", en_title="No Such Show", mtype=MediaType.TV)
    verdict = _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 8})
    assert verdict.accepted is False
    assert verdict.filter_id == "imdb_vote"
    assert verdict.reason == "IMDb 未匹配到条目"
    assert api.search_calls == [("No Such Show", 8)]
    assert api.detail_calls == []


def test_fallback_still_picks_only_same_type_candidate(install_api):
    """标题写法完全不同但有同类型候选 → 兜底照样选它（只放宽「候选选择」）。

    匹配到之后照常进入本过滤器的判定：这条人数不达标，所以最终仍被拒，但 reason 是
    「评分人数不足」而不是「未匹配」——两者的区别正是这轮放宽要带来的。
    """
    api = install_api(
        imdb_titles(("tt5555555", "tvSeries", "Another Show", None, 2020)),
        {"tt5555555": _lookup(rating=7.0, votes=500, imdb_id="tt5555555")},
    )
    mediainfo = FakeMediaInfo(title="Some Show", en_title="Some Show", mtype=MediaType.TV)
    verdict = _accepts(filters.ImdbVoteCountFilter, mediainfo, {"imdb_votes": 20000})
    assert verdict.accepted is False
    assert verdict.reason == "IMDb 评分人数 500 < 20000"
    assert api.detail_calls == ["tt5555555"]


def test_no_match_without_any_title(install_api):
    """连查询串都凑不出来（标题全空）→ 按未匹配拒绝，且**不发搜索请求**。"""
    api = install_api([])
    verdict = _accepts(
        filters.ImdbVoteFilter, FakeMediaInfo(title="", en_title=None), {"imdb_vote": 8})
    assert verdict.accepted is False
    assert verdict.reason == "IMDb 未匹配到条目"
    assert api.search_calls == []


def test_no_match_is_cached(install_api):
    """「未匹配」也是确定结论，同样进缓存（同一条目不再重复搜索）。"""
    api = install_api([])
    mediainfo = FakeMediaInfo(title="Some Show", en_title="Some Show", mtype=MediaType.TV)
    config = {"imdb_vote": 8, "imdb_votes": 20000}
    _accepts(filters.ImdbVoteFilter, mediainfo, config)
    _accepts(filters.ImdbVoteCountFilter, mediainfo, config)
    assert len(api.search_calls) == 1


# ==================== 6. 查询异常 → 放行 ====================


@pytest.mark.parametrize("filter_cls,config", [
    (filters.ImdbVoteFilter, {"imdb_vote": 8}),
    (filters.ImdbVoteCountFilter, {"imdb_votes": 20000}),
])
def test_query_exception_passes(spy, filter_cls, config):
    """查询抛异常（网络故障 / 宿主没有 IMDb 客户端）→ 放行，不拦条目。"""
    spy.raises = RuntimeError("宿主未提供 IMDb 客户端")
    verdict = _accepts(filter_cls, FakeMediaInfo(imdb_id="tt1234567"), config)
    assert verdict.accepted is True
    assert spy.calls["count"] == 1


def test_missing_host_client_passes(monkeypatch):
    """宿主没有 IMDb 客户端（导入失败）时同样放行——真实走一遍 ``_imdb_api`` 的报错路径。"""
    monkeypatch.setattr(filters, "ImdbApi", None)
    verdict = _accepts(
        filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_vote": 8})
    assert verdict.accepted is True


def test_exception_is_not_cached(spy):
    """异常结果不入缓存：下一个条目仍会真正重试（一次抖动不污染整个进程）。"""
    spy.raises = RuntimeError("boom")
    mediainfo = FakeMediaInfo(imdb_id="tt1234567")
    assert _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 8}).accepted is True
    spy.raises = None
    spy.result = _lookup(rating=5.2, votes=10)
    assert _accepts(filters.ImdbVoteFilter, mediainfo, {"imdb_vote": 8}).accepted is False
    assert spy.calls["count"] == 2


# ==================== 7. 匹配到条目但没有评分 ====================


def test_matched_without_rating_rejects(spy):
    """匹配到条目但 aggregate_rating 为 None（即便有人数）→ 拒绝「IMDb 暂无评分」。"""
    spy.result = _lookup(rating=None, votes=1189)
    verdict = _accepts(
        filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_vote": 8})
    assert verdict.accepted is False
    assert verdict.filter_id == "imdb_vote"
    assert verdict.reason == "IMDb 暂无评分"


def test_matched_with_zero_rating_rejects(spy):
    """评分为 0（无有效评分）与 None 同义。"""
    spy.result = _lookup(rating=0.0, votes=0)
    verdict = _accepts(
        filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt1234567"), {"imdb_vote": 8})
    assert verdict.accepted is False
    assert verdict.reason == "IMDb 暂无评分"


def test_detail_missing_yields_no_rating(install_api):
    """按已有 imdb_id 查详情却查不到（条目下架等）→ 同样按「暂无评分」拒绝。"""
    api = install_api([], {})
    verdict = _accepts(
        filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt0000000"), {"imdb_vote": 8})
    assert verdict.accepted is False
    assert verdict.reason == "IMDb 暂无评分"
    assert api.detail_calls == ["tt0000000"]
    assert api.search_calls == []  # 有 imdb_id 就不走搜索


# ==================== 8. 两个过滤器共用一次查询（缓存） ====================


def test_two_filters_share_one_query(spy):
    """同一条目先跑 imdb_vote 再跑 imdb_votes → 只查一次 IMDb。"""
    spy.result = _lookup(rating=8.3, votes=1189)
    mediainfo = FakeMediaInfo(imdb_id="tt1234567")
    config = {"imdb_vote": 8, "imdb_votes": 20000}

    first = _accepts(filters.ImdbVoteFilter, mediainfo, config)
    second = _accepts(filters.ImdbVoteCountFilter, mediainfo, config)

    assert first.accepted is True       # 8.3 ≥ 8
    assert second.accepted is False     # 1189 < 20000
    assert spy.calls["count"] == 1      # 缓存生效：只查一次


def test_search_result_is_cached_across_filters(install_api):
    """无 imdb_id（搜索兜底）时同样只查一次——缓存键用「识别类型 + 归一化查询串」。"""
    api = install_api(
        imdb_titles(("tt9999999", "tvSeries", "Wednesday", "Wednesday", 2022)),
        {"tt9999999": _lookup(rating=8.3, votes=1189, imdb_id="tt9999999")},
    )
    mediainfo = FakeMediaInfo(title="Wednesday", en_title="Wednesday", mtype=MediaType.TV)
    config = {"imdb_vote": 8, "imdb_votes": 20000}
    _accepts(filters.ImdbVoteFilter, mediainfo, config)
    _accepts(filters.ImdbVoteCountFilter, mediainfo, config)
    assert len(api.search_calls) == 1
    assert len(api.detail_calls) == 1


def test_cache_key_normalizes_title(install_api):
    """查询串的标点/大小写差异命中同一缓存（归一化后同名）。"""
    api = install_api(
        imdb_titles(("tt9999999", "tvSeries", "The Bear", "The Bear", 2022)),
        {"tt9999999": _lookup(rating=8.6, votes=200000, imdb_id="tt9999999")},
    )
    config = {"imdb_vote": 8}
    _accepts(filters.ImdbVoteFilter,
             FakeMediaInfo(title="The Bear", en_title="The Bear", mtype=MediaType.TV), config)
    _accepts(filters.ImdbVoteFilter,
             FakeMediaInfo(title="the  bear!", en_title="the  bear!", mtype=MediaType.TV), config)
    assert len(api.search_calls) == 1


def test_other_entry_is_queried_again(spy):
    """不同条目各自查询（缓存不越界命中）。"""
    spy.result = _lookup(rating=8.3, votes=30000)
    config = {"imdb_vote": 8}
    _accepts(filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt1111111"), config)
    _accepts(filters.ImdbVoteFilter, FakeMediaInfo(imdb_id="tt2222222"), config)
    assert spy.calls["count"] == 2


# ==================== 9. 过滤链与奈飞 spec ====================


def test_build_filter_chain_for_netflix():
    """``build_filter_chain(["imdb_vote","imdb_votes","season_exclude"], "netflix")`` 三个都建出来。"""
    chain = filters.build_filter_chain(
        ["imdb_vote", "imdb_votes", "season_exclude"], "netflix")
    assert [type(f) for f in chain._filters] == [
        filters.ImdbVoteFilter, filters.ImdbVoteCountFilter,
        filters.NetflixSeasonExcludeFilter]


def test_netflix_spec_swaps_vote_for_imdb_filters():
    """奈飞 spec：``vote`` 已删除，新增两项 IMDb 过滤且排在 ``season_exclude`` 之前。"""
    spec = netflix.NetflixRankProvider().get_spec()
    keys = [f.key for f in spec.filters_schema]
    assert "vote" not in keys
    assert keys == ["imdb_vote", "imdb_votes", "season_exclude"]

    by_key = {f.key: f for f in spec.filters_schema}
    assert by_key["imdb_vote"].default == 8
    assert by_key["imdb_vote"].kind == "float"
    assert by_key["imdb_votes"].default == 20000
    assert by_key["imdb_votes"].kind == "number"

    # spec 声明的每个 key 都要能真的构造出过滤器（否则 runner 会静默跳过）。
    chain = filters.build_filter_chain(keys, spec.provider_id)
    assert len(chain._filters) == len(keys)


def test_netflix_chain_passes_when_thresholds_disabled(spy):
    """奈飞链在阈值未启用时全放行，且不发起任何 IMDb 查询。"""
    chain = filters.build_filter_chain(["imdb_vote", "imdb_votes"], "netflix")
    verdict = chain.run("post", _item(), FakeMediaInfo(imdb_id="tt1234567"),
                        {"imdb_vote": 0, "imdb_votes": 0})
    assert verdict.accepted is True
    assert spy.calls["count"] == 0


def test_netflix_chain_short_circuits_on_imdb_vote(spy):
    """链上 ``imdb_vote`` 拒绝即短路：``imdb_votes`` 不再执行（只查一次 IMDb）。"""
    spy.result = _lookup(rating=5.2, votes=100)
    chain = filters.build_filter_chain(["imdb_vote", "imdb_votes"], "netflix")
    verdict = chain.run("post", _item(), FakeMediaInfo(imdb_id="tt1234567"),
                        {"imdb_vote": 8, "imdb_votes": 20000})
    assert verdict.accepted is False
    assert verdict.filter_id == "imdb_vote"
    assert verdict.reason == "IMDb 评分 5.2 < 8.0"
    assert spy.calls["count"] == 1


def test_other_providers_keep_tmdb_vote():
    """其它来源不受影响：``vote`` 仍是通用 ``VoteFilter``（TMDB 口径），未注册 IMDb 口径。"""
    assert filters.BUILTIN_FILTERS["vote"] is filters.VoteFilter
    chain = filters.build_filter_chain(["vote"], "maoyan")
    assert [type(f) for f in chain._filters] == [filters.VoteFilter]
    assert "imdb_vote" not in [f.key for f in netflix_meta_schema_keys()]


def netflix_meta_schema_keys():
    """奈飞 spec 的 options 字段（供上面的「未污染其它 schema」断言用）。"""
    return netflix.NetflixRankProvider().get_spec().options_schema


def test_verdict_helpers_unchanged():
    """确认复用既有的裁决构造器（风格一致性）。"""
    assert FilterVerdict.accept().accepted is True
    assert FilterVerdict.reject("imdb_vote", "x").filter_id == "imdb_vote"
