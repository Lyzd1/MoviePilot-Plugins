"""
MoviePilot v3 单文件插件：通知过滤器（NotifyFilter）

宿主 moviepilot-v3 v3.1.0 的所有通知——宿主各业务链、插件的
``self.post_message``、以及监听 ``EventType.NoticeMessage`` 的第三方通知插件——
最终都汇聚到 ``app.chain._messaging.NotificationMixin`` 的两个派发出口：

- ``_dispatch_notification_steps``（同步，``post_message`` 调用）
- ``_async_dispatch_notification_steps``（异步，``async_post_message`` 调用）

这两个方法的上游已经写完消息历史（``messageoper.add``），出口里才依次
``eventmanager.send_event(EventType.NoticeMessage)`` 与投递各通知渠道。
宿主对 ``NoticeMessage`` 事件的返回值不做取消判断，所以监听事件无法阻止通知，
唯一可行的拦截点就是这两个派发出口本身。

本插件在 ``init_plugin()``（每次启动与保存配置都会跑）先读取宿主两个方法的源码，
判定结构是否可识别，再决定是否打补丁：

- 命中规则 -> 直接返回：既不发送 ``NoticeMessage`` 事件、也不投递任何渠道，
  因此飞书 / Telegram / webhook 与第三方通知插件都收不到；消息中心历史已在上游写完，
  「历史保留、只是不推送」自然成立；命中的规则、类型、标题会写入日志与插件统计；
- 未命中 / 未启用 / 宿主结构未知 -> 原样调用被替换掉的原方法（``*args, **kwargs``
  透传），宿主行为完全不变。

不要改成 patch ``post_message``（要复刻渲染逻辑，脆弱），也不要 patch 渠道模块
（会漏掉 ``NoticeMessage`` 事件路径，第三方通知插件照样发）。本插件不修改宿主的
任何文件，只替换内存中的类方法，停用插件（``stop_service``）即还原。

@author: Lyzd1
"""

import importlib
import inspect
import re
import threading
from datetime import datetime
from functools import wraps
from typing import Any, Dict, List, Optional, Pattern, Tuple

from app.runtime.log import logger
from app.runtime.version import get_app_version
from app.sdk.plugin.base import _PluginBase

# --------------------------------------------------------------------------- #
# 补丁目标与状态常量
# --------------------------------------------------------------------------- #

# 被补丁的宿主模块 / 类 / 两个派发方法
PATCH_TARGET_MODULE = "app.chain._messaging"
PATCH_TARGET_CLASS = "NotificationMixin"
PATCH_SYNC_METHOD = "_dispatch_notification_steps"
PATCH_ASYNC_METHOD = "_async_dispatch_notification_steps"

# 打在新方法上的标记，用于幂等判定（同步/异步各一层，标记相同）
PATCH_FLAG_ATTR = "__notifyfilter_patched__"
# 挂在宿主类上的原方法备份，模块被重新加载后依旧能还原
ORIGINAL_SYNC_ATTR = "__notifyfilter_original_sync__"
ORIGINAL_ASYNC_ATTR = "__notifyfilter_original_async__"
# 挂在宿主类上的当前规则引擎：补丁函数每次调用都从这里取，插件模块重载也不失效
ENGINE_ATTR = "__notifyfilter_engine__"

# 三种自检结果
STATUS_PATCHED = "patched"
STATUS_DISABLED = "disabled"
STATUS_UNKNOWN = "unknown"

# 状态 -> 详情页告警条颜色 / 文案
_STATUS_ALERT_TYPE: Dict[str, str] = {
    STATUS_PATCHED: "success",
    STATUS_DISABLED: "info",
    STATUS_UNKNOWN: "warning",
}
_STATUS_LABEL: Dict[str, str] = {
    STATUS_PATCHED: "已打补丁：拦截生效中",
    STATUS_DISABLED: "未启用：未对宿主做任何改动",
    STATUS_UNKNOWN: "结构未知：需人工确认，当前放行全部通知",
}

# 结构判据：方法源码里必须同时出现的关键调用
# （异步出口走的是 eventmanager.async_send_event / messagequeue.async_send_message）
_SYNC_MARKERS: Tuple[str, ...] = ("send_event", "_deliver_notification")
_ASYNC_MARKERS: Tuple[str, ...] = ("async_send_event", "async_send_message")

# 匹配范围
SCOPE_BOTH = "both"
SCOPE_TITLE = "title"
SCOPE_TEXT = "text"
_SCOPE_VALUES: Tuple[str, ...] = (SCOPE_BOTH, SCOPE_TITLE, SCOPE_TEXT)
_SCOPE_OPTIONS: List[Dict[str, str]] = [
    {"title": "标题 + 内容（推荐）", "value": SCOPE_BOTH},
    {"title": "仅标题", "value": SCOPE_TITLE},
    {"title": "仅内容", "value": SCOPE_TEXT},
]

# 通知类型选项：优先取宿主 MessageType（9 个中文值），宿主不可用时用同名兜底
_MTYPE_FALLBACK: Tuple[str, ...] = (
    "资源下载", "整理入库", "订阅", "站点", "媒体服务器",
    "手动处理", "插件", "智能体", "其它",
)

# 详情页「最近命中样本」保留条数
RECENT_LIMIT = 20
# 最近命中样本里内容预览的最大字符数
PREVIEW_LIMIT = 80

# 表单里的规则示例与说明（\n 已是字面换行，配合 white-space: pre-line 渲染）
_RULES_EXAMPLE = (
    "# 不接收站点「电力赠送 / 扣减」通知（数字每天变，所以不要写死数字）\n"
    "(收到来自\\s*\\S+\\s*赠送的|响应了你的请求，)(赠送|扣减)\n"
    "# 不接收站点 Cookie 失效提醒\n"
    "Cookie已失效"
)
_RULES_FORMAT = (
    "规则格式（每行一条，命中任意一条就不推送）：\n"
    "· 每行一条 Python 正则，按“忽略大小写”做包含匹配（re.search）\n"
    "· 以 # 开头的整行是注释，空行自动忽略\n"
    "· 写不出正则时可直接写关键字原文，例如：Cookie已失效\n"
    "· 数字会变的通知（电力 / 积分等）请用 \\s*\\S+\\s* 之类的通配，不要写死数字"
)

# 保护补丁与规则解析状态
_patch_lock = threading.Lock()


def _empty_stats() -> Dict[str, Any]:
    """统计的初始结构。"""
    return {
        "total": 0,
        "by_rule": {},
        "rule_errors": [],
        "recent": [],
        "updated_at": "",
    }


# --------------------------------------------------------------------------- #
# 纯函数：规则解析与匹配（不依赖宿主运行时，便于独立校验）
# --------------------------------------------------------------------------- #

def parse_rules(raw: Any) -> Tuple[List[Tuple[str, Pattern]], List[str]]:
    """
    把「每行一条」的规则文本编译成正则列表。

    :param raw: 用户填写的多行文本
    :return: ``([(规则原文, 编译后的正则)], [无效规则说明])``
    """
    rules: List[Tuple[str, Pattern]] = []
    errors: List[str] = []
    seen = set()
    for line in str(raw or "").splitlines():
        text = line.strip()
        # 空行与整行注释忽略
        if not text or text.startswith("#"):
            continue
        if text in seen:
            continue
        seen.add(text)
        try:
            rules.append((text, re.compile(text, re.IGNORECASE)))
        except re.error as err:
            errors.append(f"{text}（{err}）")
    return rules, errors


def build_match_text(scope: str, title: Any, text: Any) -> str:
    """
    按范围拼接待匹配文本。

    :param scope: ``both`` / ``title`` / ``text``
    :param title: 消息标题
    :param text: 消息内容
    :return: 待匹配文本（两者皆空时返回空串）
    """
    title_text = str(title or "")
    body_text = str(text or "")
    if scope == SCOPE_TITLE:
        return title_text
    if scope == SCOPE_TEXT:
        return body_text
    return f"{title_text}\n{body_text}"


def normalize_mtype(mtype: Any) -> str:
    """把 ``MessageType`` 枚举或字符串统一成中文值字符串，缺省为空串。"""
    value = getattr(mtype, "value", mtype)
    return "" if value is None else str(value)


def find_matching_rule(
        rules: List[Tuple[str, Pattern]],
        scope: str,
        mtypes: Any,
        mtype: Any,
        title: Any,
        text: Any,
) -> Optional[str]:
    """
    纯函数匹配：命中返回规则原文，未命中返回 ``None``。

    :param rules: ``parse_rules`` 产出的规则列表
    :param scope: 匹配范围
    :param mtypes: 限定生效的通知类型（空表示不限）
    :param mtype: 本条消息的类型（``MessageType`` 或中文值）
    :param title: 消息标题
    :param text: 消息内容
    """
    if not rules:
        return None
    allowed = tuple(mtypes or ())
    if allowed:
        current = normalize_mtype(mtype)
        if not current or current not in allowed:
            return None
    blob = build_match_text(scope, title, text)
    if not blob:
        return None
    for rule_text, pattern in rules:
        try:
            if pattern.search(blob):
                return rule_text
        except Exception:
            # 单条规则出问题不得影响其它规则
            continue
    return None


def detect_method_status(
        source: Optional[str], markers: Tuple[str, ...]
) -> Tuple[str, str]:
    """
    根据宿主方法源码判定结构是否可识别（纯函数）。

    :param source: 宿主方法源码文本
    :param markers: 该方法必须出现的关键调用
    :return: ``(STATUS_PATCHED 或 STATUS_UNKNOWN, 中文判据说明)``
    """
    text = source or ""
    if not text.strip():
        return STATUS_UNKNOWN, "未读到方法源码"
    missing = [marker for marker in markers if marker not in text]
    if missing:
        return STATUS_UNKNOWN, f"源码中缺少关键调用 {'、'.join(missing)}"
    return STATUS_PATCHED, f"源码中关键调用齐全（{'、'.join(markers)}）"


def _preview(message: Any, scope: str) -> str:
    """生成最近命中样本里的内容预览（压平换行并截断）。"""
    blob = build_match_text(
        scope,
        getattr(message, "title", None),
        getattr(message, "text", None),
    )
    flat = " ".join(str(blob or "").split())
    return flat[:PREVIEW_LIMIT] + ("…" if len(flat) > PREVIEW_LIMIT else "")


# --------------------------------------------------------------------------- #
# 规则引擎：一次 init_plugin 生效的规则集 + 命中统计
# --------------------------------------------------------------------------- #

class _NotifyFilterEngine:
    """一次配置生效期间使用的规则集，并负责命中日志与统计落盘。"""

    def __init__(
            self,
            plugin: Any,
            rules: List[Tuple[str, Pattern]],
            rule_errors: List[str],
            scope: str,
            mtypes: List[str],
            stats: Dict[str, Any],
    ) -> None:
        self._plugin = plugin
        self._rules = rules
        self._rule_errors = list(rule_errors)
        self._scope = scope
        self._mtypes = tuple(mtypes)
        self._stats = stats if isinstance(stats, dict) else _empty_stats()
        self._lock = threading.Lock()

    @property
    def rule_count(self) -> int:
        """生效规则条数。"""
        return len(self._rules)

    @property
    def rule_errors(self) -> List[str]:
        """无法解析、被跳过的规则。"""
        return list(self._rule_errors)

    @property
    def stats(self) -> Dict[str, Any]:
        """当前累计统计。"""
        return self._stats

    def match(self, message: Any) -> Optional[str]:
        """返回命中的规则原文，未命中返回 ``None``。"""
        if not self._rules:
            return None
        return find_matching_rule(
            rules=self._rules,
            scope=self._scope,
            mtypes=self._mtypes,
            mtype=getattr(message, "mtype", None),
            title=getattr(message, "title", None),
            text=getattr(message, "text", None),
        )

    def intercept(self, message: Any) -> bool:
        """
        判定是否需要拦截：命中即记日志与统计并返回 ``True``。

        任何异常都按「不拦截」处理——过滤器出问题绝不能把通知整个弄丢。
        """
        try:
            rule = self.match(message)
        except Exception as err:
            logger.error(f"通知过滤器：规则匹配异常，本次放行（{err}）")
            return False
        if rule is None:
            return False
        try:
            self._record(message, rule)
        except Exception as err:
            logger.debug(f"通知过滤器：记录拦截统计失败：{err}")
        return True

    def _record(self, message: Any, rule: str) -> None:
        """记录一次拦截：日志 + 统计（``stats`` 键落盘）。"""
        mtype = normalize_mtype(getattr(message, "mtype", None))
        title = str(getattr(message, "title", None) or "")
        logger.info(
            f"通知过滤器：已拦截通知（规则={rule} 类型={mtype or '未知'} 标题={title}）"
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            stats = self._stats
            stats["total"] = int(stats.get("total") or 0) + 1
            by_rule = stats.get("by_rule")
            if not isinstance(by_rule, dict):
                by_rule = {}
            by_rule[rule] = int(by_rule.get(rule) or 0) + 1
            stats["by_rule"] = by_rule
            stats["rule_errors"] = list(self._rule_errors)
            recent = stats.get("recent")
            if not isinstance(recent, list):
                recent = []
            recent.insert(0, {
                "time": now,
                "mtype": mtype,
                "title": title,
                "rule": rule,
                "head": _preview(message, self._scope),
            })
            stats["recent"] = recent[:RECENT_LIMIT]
            stats["updated_at"] = now
            self._plugin.save_data("stats", stats)


# --------------------------------------------------------------------------- #
# 补丁实现
# --------------------------------------------------------------------------- #

def _target_class() -> Optional[Any]:
    """取宿主 ``NotificationMixin`` 类，宿主不可用时返回 None。"""
    try:
        module = importlib.import_module(PATCH_TARGET_MODULE)
    except Exception:
        return None
    return getattr(module, PATCH_TARGET_CLASS, None)


def _current_engine() -> Optional[Any]:
    """
    读取当前生效的规则引擎。

    引擎挂在宿主类属性上而不是插件模块全局里，插件模块被重新加载后补丁函数
    依旧能读到最新配置。返回对象只做鸭子类型判断，不做 isinstance 校验。
    """
    target = _target_class()
    if target is None:
        return None
    engine = getattr(target, ENGINE_ATTR, None)
    return engine if callable(getattr(engine, "intercept", None)) else None


def _set_engine(engine: Optional[Any]) -> None:
    """把规则引擎写到宿主类属性上（None 表示放行全部通知）。"""
    target = _target_class()
    if target is None:
        return
    try:
        setattr(target, ENGINE_ATTR, engine)
    except Exception as err:
        logger.debug(f"通知过滤器：写入规则引擎失败：{err}")


def _unwrap(method: Any) -> Any:
    """把 staticmethod / classmethod 还原成底层函数。"""
    return getattr(method, "__func__", method)


def _retarget_method_filename(new_method: Any, original_method: Any) -> Any:
    """
    把补丁方法的 ``co_filename`` 对齐到原方法，便于日志与回溯归位。

    做法与宿主内既有插件（curetmdbanimeshy、feishuimagefix）的
    ``_retarget_method_filename`` 一致。
    """
    original_func = _unwrap(original_method)
    target_filename = getattr(
        getattr(original_func, "__code__", None), "co_filename", None
    )
    if not target_filename:
        return new_method

    is_static = isinstance(new_method, staticmethod)
    is_class = isinstance(new_method, classmethod)
    patch_func = _unwrap(new_method)
    patch_code = getattr(patch_func, "__code__", None)
    if not patch_code or not hasattr(patch_code, "replace"):
        return new_method

    try:
        patch_func.__code__ = patch_code.replace(co_filename=target_filename)
    except Exception as err:
        logger.debug(f"通知过滤器：方法文件名对齐失败，保留原补丁实现：{err}")
        return new_method

    if is_static:
        return staticmethod(patch_func)
    if is_class:
        return classmethod(patch_func)
    return patch_func


def _build_sync_wrapper(original: Any) -> Any:
    """构造同步派发出口的替换方法（保留原方法元信息并打上幂等标记）。"""
    original_func = _unwrap(original)

    @wraps(original_func)
    def wrapper(self: Any, message: Any, *args: Any, **kwargs: Any) -> None:
        try:
            engine = _current_engine()
            if engine is not None and engine.intercept(message):
                # 命中：不发送 NoticeMessage 事件、不投递任何渠道，直接返回
                return None
        except Exception as err:
            logger.error(f"通知过滤器：拦截判断异常，本次放行（{err}）")
        return original_func(self, message, *args, **kwargs)

    setattr(wrapper, PATCH_FLAG_ATTR, True)
    return _retarget_method_filename(wrapper, original)


def _build_async_wrapper(original: Any) -> Any:
    """构造异步派发出口的替换方法（语义与同步版一致）。"""
    original_func = _unwrap(original)

    @wraps(original_func)
    async def wrapper(self: Any, message: Any, *args: Any, **kwargs: Any) -> None:
        try:
            engine = _current_engine()
            if engine is not None and engine.intercept(message):
                return None
        except Exception as err:
            logger.error(f"通知过滤器：拦截判断异常，本次放行（{err}）")
        return await original_func(self, message, *args, **kwargs)

    setattr(wrapper, PATCH_FLAG_ATTR, True)
    return _retarget_method_filename(wrapper, original)


def apply_patch() -> Tuple[bool, str]:
    """
    给宿主两个派发出口打补丁（幂等，重复调用不会叠加）。

    :return: ``(本次是否新打了补丁, 说明)``
    """
    target = _target_class()
    if target is None:
        return False, f"无法导入宿主通知模块 {PATCH_TARGET_MODULE}"

    with _patch_lock:
        for method_name in (PATCH_SYNC_METHOD, PATCH_ASYNC_METHOD):
            if getattr(target, method_name, None) is None:
                return False, f"宿主 {PATCH_TARGET_CLASS}.{method_name} 不存在，跳过打补丁"

        # 两个方法都已打过 -> 直接返回，避免包两层
        if all(
                getattr(getattr(target, method_name, None), PATCH_FLAG_ATTR, False)
                for method_name in (PATCH_SYNC_METHOD, PATCH_ASYNC_METHOD)
        ):
            return False, "补丁已存在，跳过重复打补丁"

        patched: List[str] = []
        for method_name, original_attr, builder in (
                (PATCH_SYNC_METHOD, ORIGINAL_SYNC_ATTR, _build_sync_wrapper),
                (PATCH_ASYNC_METHOD, ORIGINAL_ASYNC_ATTR, _build_async_wrapper),
        ):
            current = getattr(target, method_name, None)
            if getattr(current, PATCH_FLAG_ATTR, False):
                continue
            # 原方法备份优先取类属性（模块重载后仍可用），其次按静态方式取当前方法
            original = getattr(target, original_attr, None)
            if original is None:
                try:
                    original = inspect.getattr_static(target, method_name)
                except AttributeError as err:
                    return False, f"未能在类上定位 {method_name} 原方法：{err}"
            setattr(target, original_attr, original)
            setattr(target, method_name, builder(original))
            patched.append(method_name)

    return True, "已替换 " + "、".join(patched)


def revert_patch() -> bool:
    """
    还原宿主两个派发出口。

    :return: 是否真的做了还原
    """
    target = _target_class()
    if target is None:
        logger.error("通知过滤器：还原补丁失败，无法导入宿主通知模块")
        return False

    reverted = False
    with _patch_lock:
        for method_name, original_attr in (
                (PATCH_SYNC_METHOD, ORIGINAL_SYNC_ATTR),
                (PATCH_ASYNC_METHOD, ORIGINAL_ASYNC_ATTR),
        ):
            original = getattr(target, original_attr, None)
            if original is None:
                continue
            try:
                setattr(target, method_name, original)
                reverted = True
            except Exception as err:
                logger.error(f"通知过滤器：还原 {method_name} 失败：{err}")
                continue
            try:
                delattr(target, original_attr)
            except Exception:
                pass
    return reverted


def is_patched() -> bool:
    """当前宿主两个派发出口是否都已被本插件替换。"""
    target = _target_class()
    if target is None:
        return False
    return all(
        getattr(getattr(target, method_name, None), PATCH_FLAG_ATTR, False)
        for method_name in (PATCH_SYNC_METHOD, PATCH_ASYNC_METHOD)
    )


def inspect_host_status() -> Tuple[str, str]:
    """
    读取宿主两个派发方法的源码并判定结构是否可识别。

    若方法已被本插件替换，则改用保存下来的原方法源码做判定——补丁方法的
    ``co_filename`` 已被对齐到宿主文件，直接读取会读到错位的行。

    :return: ``(status, reason)``
    """
    target = _target_class()
    if target is None:
        return STATUS_UNKNOWN, f"无法导入宿主通知模块 {PATCH_TARGET_MODULE}"

    reasons: List[str] = []
    for method_name, original_attr, markers in (
            (PATCH_SYNC_METHOD, ORIGINAL_SYNC_ATTR, _SYNC_MARKERS),
            (PATCH_ASYNC_METHOD, ORIGINAL_ASYNC_ATTR, _ASYNC_MARKERS),
    ):
        method = getattr(target, method_name, None)
        if method is None:
            return STATUS_UNKNOWN, (
                f"宿主 {PATCH_TARGET_CLASS} 上未找到 {method_name} 方法"
            )
        if getattr(method, PATCH_FLAG_ATTR, False):
            saved = getattr(target, original_attr, None)
            if saved is not None:
                method = _unwrap(saved)
        try:
            source = inspect.getsource(method)
        except Exception as err:
            return STATUS_UNKNOWN, f"无法读取 {method_name} 源码：{err}"
        status, reason = detect_method_status(source, markers)
        if status != STATUS_PATCHED:
            return STATUS_UNKNOWN, f"{method_name}：{reason}"
        reasons.append(f"{method_name}：{reason}")
    return STATUS_PATCHED, "；".join(reasons)


# --------------------------------------------------------------------------- #
# 配置解析辅助
# --------------------------------------------------------------------------- #

def _as_bool(value: Any) -> bool:
    """把配置值转成布尔（兼容字符串形式）。"""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(value)


def _normalize_scope(value: Any) -> str:
    """匹配范围取值，非法或缺省时回落到「标题 + 内容」。"""
    scope = str(value or "").strip().lower()
    return scope if scope in _SCOPE_VALUES else SCOPE_BOTH


def _normalize_mtypes(value: Any) -> List[str]:
    """通知类型多选：空表示不限；只保留非空字符串。"""
    if not value:
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return [str(item).strip() for item in value if str(item or "").strip()]


def _mtype_options() -> List[Dict[str, str]]:
    """通知类型下拉选项：优先取宿主 MessageType 的中文值。"""
    try:
        from app.schemas.types import MessageType

        values = [str(item.value) for item in MessageType]
    except Exception:
        values = list(_MTYPE_FALLBACK)
    return [{"title": value, "value": value} for value in values]


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #

class NotifyFilter(_PluginBase):
    # 插件名称
    plugin_name = "通知过滤器"
    # 插件描述
    plugin_desc = "按自定义规则（正则）拦截指定内容的消息通知：命中即不推送任何渠道，消息中心历史仍然保留"
    # 插件图标
    plugin_icon = "contract.png"
    # 插件版本
    plugin_version = "0.1.0"
    # 插件作者
    plugin_author = "Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "notifyfilter_"
    # 加载顺序
    plugin_order = 60
    # 可使用的用户级别
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        # 当前是否启用（与配置项 enabled 同步）
        self._enabled = False
        # 当前生效的规则引擎
        self._engine: Optional[_NotifyFilterEngine] = None
        # 最近一次自检结果
        self._state: Optional[Dict[str, Any]] = None

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """生效配置：解析规则，按需给宿主通知派发出口打补丁，并落盘自检结论。"""
        config = config or {}
        enabled = _as_bool(config.get("enabled", False))
        scope = _normalize_scope(config.get("scope"))
        mtypes = _normalize_mtypes(config.get("mtypes"))
        rules, rule_errors = parse_rules(config.get("rules"))

        self._enabled = enabled
        stats = self._load_stats()
        # 规则错误每次生效都刷新，其余统计保留
        stats["rule_errors"] = list(rule_errors)
        try:
            self.save_data("stats", stats)
        except Exception as err:
            logger.debug(f"通知过滤器：保存统计失败：{err}")

        if not enabled:
            # 未启用：清空引擎并还原，保证宿主行为与未装插件时完全一致
            self._engine = None
            _set_engine(None)
            if revert_patch():
                logger.info("通知过滤器：插件未启用，已还原此前的补丁")
            status, reason = STATUS_DISABLED, "插件未启用，未对宿主做任何改动"
        else:
            status, reason = inspect_host_status()
            if status == STATUS_PATCHED:
                self._engine = _NotifyFilterEngine(
                    plugin=self,
                    rules=rules,
                    rule_errors=rule_errors,
                    scope=scope,
                    mtypes=mtypes,
                    stats=stats,
                )
                _set_engine(self._engine)
                patched_now, patch_message = apply_patch()
                reason = f"{reason}；{patch_message}"
                if patched_now:
                    logger.info(
                        f"通知过滤器：已启用，生效规则 {len(rules)} 条，已打补丁"
                    )
                else:
                    logger.info(
                        f"通知过滤器：已启用，生效规则 {len(rules)} 条，{patch_message}"
                    )
                if rule_errors:
                    logger.warning(
                        f"通知过滤器：{len(rule_errors)} 条规则无法解析已跳过：{rule_errors}"
                    )
            else:
                self._engine = None
                _set_engine(None)
                if revert_patch():
                    logger.info("通知过滤器：宿主结构未知，已还原此前的补丁")
                logger.warning(
                    f"通知过滤器：无法识别宿主通知结构，未打补丁，请人工确认（{reason}）"
                )

        self._state = {
            "status": status,
            "reason": reason,
            "scope": scope,
            "mtypes": list(mtypes),
            "rule_count": len(rules),
            "rule_errors": list(rule_errors),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "app_version": get_app_version(),
        }
        self.save_data("state", self._state)

    def stop_service(self) -> None:
        """停用插件：清空规则引擎并还原宿主方法。"""
        try:
            self._engine = None
            _set_engine(None)
            if revert_patch():
                logger.info("通知过滤器：已还原宿主通知派发方法")
        except Exception as err:
            logger.error(f"通知过滤器：停止插件时还原失败：{err}")

    def get_state(self) -> bool:
        """插件运行状态：跟随配置项 enabled。"""
        return bool(self._enabled)

    def get_api(self) -> List[Dict[str, Any]]:
        """本插件不提供 API。"""
        return []

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """配置页：总开关 + 规则文本域 + 范围/类型限制 + 可复制的示例与说明。"""
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
                                            "text": "本插件在宿主通知的派发出口处拦截：命中的通知不再推送到任何渠道"
                                                    "（飞书、Telegram、webhook，以及各类监听 NoticeMessage 的通知插件），"
                                                    "但网页「消息中心」的历史记录照常保留，随时可以回看。",
                                        },
                                    },
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "class": "mb-2",
                                            "style": "white-space: pre-line;",
                                            "text": "规则作用于「所有」通知，写得过于宽泛会连带拦掉其它通知"
                                                    "（例如只写「成功」两个字，可能把「智能体」回复、入库完成等一起拦掉）。"
                                                    "建议只写你确实不想收到的那类内容的特征词，添加后观察详情页的"
                                                    "「最近命中样本」再逐步收紧。",
                                        },
                                    },
                                ],
                            }
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "hint": "总开关；关闭时插件不改动宿主任何行为",
                                            "persistent-hint": True,
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rules",
                                            "label": "过滤规则（每行一条）",
                                            "rows": 10,
                                            "auto-grow": True,
                                            "placeholder": _RULES_EXAMPLE,
                                            "hint": "每行一条 Python 正则，# 开头为注释，空行忽略；"
                                                    "写不出正则时可直接写关键字原文",
                                            "persistent-hint": True,
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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "scope",
                                            "label": "匹配范围",
                                            "items": _SCOPE_OPTIONS,
                                            "hint": "规则在哪些文本上匹配",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mtypes",
                                            "label": "限定通知类型（留空 = 全部类型）",
                                            "items": _mtype_options(),
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "只拦截选中类型的通知；不选则对所有类型生效",
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "class": "mb-2",
                                            "style": "white-space: pre-line;",
                                            "text": _RULES_FORMAT,
                                        },
                                    },
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "style": "white-space: pre-line;",
                                            "text": f"可以直接复制下面的示例，按需改词：\n{_RULES_EXAMPLE}",
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "rules": "",
            "scope": SCOPE_BOTH,
            "mtypes": [],
        }

    def get_page(self) -> List[Dict[str, Any]]:
        """详情页：展示状态、生效规则、累计拦截、按规则计数、最近命中与无效规则。"""
        state = self._load_state()
        stats = self._load_stats()

        status = str(state.get("status") or STATUS_UNKNOWN)
        alert_type = _STATUS_ALERT_TYPE.get(status, "warning")
        label = _STATUS_LABEL.get(status, _STATUS_LABEL[STATUS_UNKNOWN])
        reason = state.get("reason") or "尚无自检记录，插件可能还没完成启动。"

        total = int(stats.get("total") or 0)
        by_rule = stats.get("by_rule")
        by_rule = by_rule if isinstance(by_rule, dict) else {}
        recent = stats.get("recent")
        recent = recent if isinstance(recent, list) else []
        rule_errors = state.get("rule_errors")
        if not isinstance(rule_errors, list):
            rule_errors = stats.get("rule_errors")
        rule_errors = rule_errors if isinstance(rule_errors, list) else []

        def detail_line(text: str) -> Dict[str, Any]:
            return {
                "component": "div",
                "props": {"class": "text-body-2 py-1"},
                "text": text,
            }

        overview = [
            detail_line(f"生效规则：{int(state.get('rule_count') or 0)} 条"),
            detail_line(f"累计拦截：{total} 条"),
            detail_line(f"匹配范围：{_scope_label(state.get('scope'))}"),
            detail_line(f"限定类型：{_mtypes_label(state.get('mtypes'))}"),
            detail_line(f"最近自检：{state.get('checked_at') or '未记录'}"),
            detail_line(f"当前程序版本：{state.get('app_version') or get_app_version()}"),
        ]
        if int(state.get("rule_count") or 0) == 0:
            overview.append(detail_line("提示：还没填规则，插件不会拦任何通知。"))

        page: List[Dict[str, Any]] = [
            {
                "component": "VAlert",
                "props": {
                    "type": alert_type,
                    "variant": "tonal",
                    "class": "mb-4",
                },
                "content": [
                    {
                        "component": "strong",
                        "props": {"class": "d-block mb-1"},
                        "text": f"当前状态：{label}",
                    },
                    {
                        "component": "span",
                        "text": reason,
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-2"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": "运行详情",
                    },
                    {
                        "component": "VCardText",
                        "content": overview,
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-2"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": f"按规则计数（共 {total} 条）",
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            detail_line(
                                f"「{rule}」：{count} 条"
                            )
                            for rule, count in sorted(
                                by_rule.items(),
                                key=lambda item: (
                                    item[1] if isinstance(item[1], int) else 0
                                ),
                                reverse=True,
                            )
                        ] or [detail_line("暂无拦截记录。")],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-2"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": f"最近命中样本（最多 {RECENT_LIMIT} 条，新在前）",
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            detail_line(
                                f"{item.get('time') or '-'}｜{item.get('mtype') or '未知'}"
                                f"｜{item.get('title') or '-'}"
                                f"｜规则：{item.get('rule') or '-'}"
                                f"｜{item.get('head') or ''}"
                            )
                            for item in recent
                            if isinstance(item, dict)
                        ] or [detail_line("暂无命中记录。")],
                    },
                ],
            },
        ]

        if rule_errors:
            page.append({
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-2"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": f"无效规则（{len(rule_errors)} 条，已跳过）",
                    },
                    {
                        "component": "VCardText",
                        "content": [detail_line(str(item)) for item in rule_errors],
                    },
                ],
            })

        page.append({
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "style": "white-space: pre-line;",
                "text": "补丁只替换内存中的宿主类方法，不改动宿主任何文件；停用或卸载本插件即自动还原。\n"
                        "被拦截的通知仍然会写进网页「消息中心」的历史，只是不再推送到任何渠道。",
            },
        })
        return page

    def _load_state(self) -> Dict[str, Any]:
        """读取最近一次自检结果，内存优先，其次数据库。"""
        if isinstance(self._state, dict) and self._state:
            return self._state
        try:
            state = self.get_data("state")
        except Exception as err:
            logger.debug(f"通知过滤器：读取插件数据失败：{err}")
            return {}
        return state if isinstance(state, dict) else {}

    def _load_stats(self) -> Dict[str, Any]:
        """读取累计统计，内存/数据库都没有时给出空结构。"""
        engine = self._engine
        if engine is not None and isinstance(engine.stats, dict):
            return engine.stats
        try:
            stats = self.get_data("stats")
        except Exception as err:
            logger.debug(f"通知过滤器：读取统计失败：{err}")
            return _empty_stats()
        if not isinstance(stats, dict):
            return _empty_stats()
        merged = _empty_stats()
        merged.update(stats)
        return merged


def _scope_label(value: Any) -> str:
    """匹配范围的中文标签。"""
    scope = _normalize_scope(value)
    for option in _SCOPE_OPTIONS:
        if option["value"] == scope:
            return option["title"]
    return scope


def _mtypes_label(value: Any) -> str:
    """限定类型的中文标签。"""
    mtypes = _normalize_mtypes(value)
    return "不限" if not mtypes else "、".join(mtypes)
