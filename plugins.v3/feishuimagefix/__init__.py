"""
MoviePilot v3 单文件插件：飞书图片修复（FeishuImageFix）

宿主 moviepilot-v3 v3.0.10-1 的
``app.modules.feishu.feishu.Feishu._upload_remote_image`` 直接用
``RequestUtils(...).get_res(image_url)`` 取图，请求里既不带 Referer 也不带代理，
于是两处真实的图床都取不到图：

1. 豆瓣图床 ``img*.doubanio.com`` 按 Referer 防盗链，无 Referer 直接返回 HTTP 418 + 空 body；
2. ``image.tmdb.org`` 直连会被 reset，必须走 ``PROXY_HOST``（``RequestUtils`` 的代理只来自
   构造参数，这里没传）。

本插件在 ``init_plugin()``（每次启动都会跑）读取宿主该方法源码判定上游是否已修复，
仅在「未修复」时用同进程猴子补丁替换掉它：

- 取图优先复用宿主既有的正确通路 ``app.application.image.ImageHelper``（内部对
  ``doubanio.com`` 自动加 ``Referer: https://movie.douban.com/`` 且不走代理，其它域名按配置走代理）；
- ``ImageHelper`` 不可用（例如独立进程里运行时配置尚未装配）时，回退到本插件自带的下载器，
  逻辑与 ``ImageHelper._get_request_params`` 对齐；
- 拿到图片字节后照旧写临时文件，再调用宿主原有的 ``Feishu._upload_image`` 拿 image_key。

本插件不修改宿主的任何文件，只替换内存中的类方法，停用插件即还原。
上游修复后（源码里出现 referer 或 ImageHelper 即可判定），日志与插件详情页会提示可以删除本插件。

@author: Lyzd1
"""

import importlib
import inspect
import tempfile
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests

from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.version import get_app_version
from app.sdk.plugin.base import _PluginBase

# --------------------------------------------------------------------------- #
# 补丁目标与状态常量
# --------------------------------------------------------------------------- #

# 被补丁的宿主模块 / 类 / 方法
PATCH_TARGET_MODULE = "app.modules.feishu.feishu"
PATCH_TARGET_CLASS = "Feishu"
PATCH_TARGET_METHOD = "_upload_remote_image"

# 打在新方法上的标记，用于幂等判定
PATCH_FLAG_ATTR = "__feishu_image_fix_patched__"
# 存在类上的原方法备份，便于模块被重新加载后依旧能还原
ORIGINAL_ATTR = "__feishu_image_fix_original__"

# 三种检测结果
STATUS_PATCHED = "patched"
STATUS_UPSTREAM_FIXED = "upstream_fixed"
STATUS_UNKNOWN = "unknown"

# 状态 -> 详情页告警条颜色 / 文案
_STATUS_ALERT_TYPE: Dict[str, str] = {
    STATUS_PATCHED: "success",
    STATUS_UPSTREAM_FIXED: "info",
    STATUS_UNKNOWN: "warning",
}
_STATUS_LABEL: Dict[str, str] = {
    STATUS_PATCHED: "已打补丁：修复生效中",
    STATUS_UPSTREAM_FIXED: "上游已修复：可以删除本插件",
    STATUS_UNKNOWN: "结构未知：需要人工确认",
}

# 回退下载器使用的常规浏览器 UA
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 图片后缀推断用的兜底表（与宿主 Feishu 同口径）
_IMAGE_SUFFIXES: Set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".heic",
}
_MIME_SUFFIX_MAP: Dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
}

# 模块级补丁状态：原方法引用 + 互斥锁
_patch_lock = threading.Lock()
_original_method: Optional[Any] = None


# --------------------------------------------------------------------------- #
# 判据：纯函数，便于脱离宿主运行时单独校验
# --------------------------------------------------------------------------- #

def _source_features(source: Optional[str]) -> Dict[str, bool]:
    """
    提取判据关心的关键代码特征。

    :param source: 宿主方法的源码文本
    :return: 各特征是否命中
    """
    text = source or ""
    lowered = text.lower()
    return {
        "has_source": bool(text.strip()),
        "has_referer": "referer" in lowered,
        "has_imagehelper": "imagehelper" in lowered,
        "has_requestutils": "requestutils(" in lowered,
    }


def detect_patch_status(source: Optional[str]) -> str:
    """
    根据宿主方法源码判定补丁状态（纯函数，不依赖宿主运行时）。

    判定规则：
    - 源码里出现 ``referer``（大小写不敏感）或 ``ImageHelper`` -> ``upstream_fixed``（上游已修复，不打补丁）
    - 源码里有 ``RequestUtils(`` 调用，且既无 referer 也无 ImageHelper -> ``patched``（未修复，需要打补丁）
    - 方法不存在 / 源码为空 / 既没有 ``RequestUtils(`` 也没有 ``ImageHelper`` -> ``unknown``（结构未知，不打补丁）

    :param source: 宿主方法源码文本，取不到时传 None
    :return: ``patched`` / ``upstream_fixed`` / ``unknown``
    """
    features = _source_features(source)
    if not features["has_source"]:
        return STATUS_UNKNOWN
    if features["has_referer"] or features["has_imagehelper"]:
        return STATUS_UPSTREAM_FIXED
    if features["has_requestutils"]:
        return STATUS_PATCHED
    return STATUS_UNKNOWN


def describe_patch_reason(source: Optional[str], status: str) -> str:
    """
    生成人类可读的检测依据，用于日志、插件数据与详情页。

    :param source: 宿主方法源码文本
    :param status: ``detect_patch_status`` 的返回结果
    :return: 中文判据说明
    """
    features = _source_features(source)
    if status == STATUS_UPSTREAM_FIXED:
        hit = "referer" if features["has_referer"] else "ImageHelper"
        return f"宿主源码中出现 {hit}，判定上游已自行处理 Referer/代理，本插件不介入"
    if status == STATUS_PATCHED:
        return (
            "宿主源码存在 RequestUtils( 调用，但既未出现 referer 也未出现 ImageHelper，"
            "判定上游尚未修复"
        )
    if not features["has_source"]:
        return "未能读到宿主 Feishu._upload_remote_image 的源码，无法判断，请人工确认"
    return (
        "宿主源码中既没有 RequestUtils( 也没有 referer/ImageHelper，"
        "方法结构无法识别，未打补丁，请人工确认"
    )


# --------------------------------------------------------------------------- #
# 回退下载器：ImageHelper 不可用时的取图通路
# --------------------------------------------------------------------------- #

def fetch_image_response(
        url: str,
        proxy_host: Optional[str] = None,
        timeout: int = 30,
        ua: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """
    回退下载器：按 ``ImageHelper._get_request_params`` 的同款策略取图。

    - URL 含 ``doubanio.com``：补 ``Referer: https://movie.douban.com/``，且**不走代理**
    - 其它 URL：``proxy_host`` 非空时同时作为 http/https 代理，为空则直连

    :param url: 图片地址
    :param proxy_host: 形如 ``http://172.17.0.1:7893`` 的代理地址，为空表示直连
    :param timeout: 超时秒数
    :param ua: 自定义 UA，缺省使用常规浏览器 UA
    :return: ``(图片字节, Content-Type)``，失败返回 ``(None, None)``
    """
    if not url:
        return None, None

    headers = {"User-Agent": ua or _DEFAULT_USER_AGENT}
    proxies: Optional[Dict[str, str]] = None
    if "doubanio.com" in url:
        # 豆瓣图床按 Referer 防盗链，且走代理反而更容易被拒
        headers["Referer"] = "https://movie.douban.com/"
    elif proxy_host:
        proxies = {"http": proxy_host, "https": proxy_host}

    try:
        response = requests.get(
            url, headers=headers, proxies=proxies, timeout=timeout
        )
    except Exception as err:
        logger.warning(f"飞书图片下载失败：{url}（{err}）")
        return None, None

    try:
        content = response.content
        if response.status_code != 200 or not content:
            logger.warning(
                f"飞书图片下载失败：{url}，状态码={response.status_code}"
            )
            return None, None
        content_type = response.headers.get("Content-Type")
        return content, content_type
    except Exception as err:
        logger.warning(f"飞书图片下载失败：{url}（{err}）")
        return None, None
    finally:
        try:
            response.close()
        except Exception:
            pass


def download_image_bytes(
        url: str,
        proxy_host: Optional[str] = None,
        timeout: int = 30,
        ua: Optional[str] = None,
) -> Optional[bytes]:
    """
    回退下载器的字节接口，只关心拿没拿到图片内容。

    :param url: 图片地址
    :param proxy_host: 形如 ``http://172.17.0.1:7893`` 的代理地址，为空表示直连
    :param timeout: 超时秒数
    :param ua: 自定义 UA
    :return: 图片字节，失败返回 None
    """
    content, _ = fetch_image_response(
        url, proxy_host=proxy_host, timeout=timeout, ua=ua
    )
    return content


# --------------------------------------------------------------------------- #
# 补丁实现
# --------------------------------------------------------------------------- #

def _runtime_proxy_host() -> Optional[str]:
    """读取运行时配置里的 PROXY_HOST，未装配或异常时按直连处理。"""
    try:
        proxy_host = (get_runtime_setting("PROXY_HOST") or "").strip()
    except Exception:
        return None
    return proxy_host or None


def _fetch_via_image_helper(image_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    优先走宿主既有的正确通路 ``ImageHelper``。

    注意：独立进程里运行时配置尚未装配时构造 ``ImageHelper()`` 会抛
    ``RuntimeError``，这里必须整体兜住异常，交给回退下载器。
    """
    try:
        from app.application.image import ImageHelper

        helper = ImageHelper()
        # 带 MIME 的接口能顺带把后缀推断做对，优先用它
        mime_fetcher = getattr(helper, "fetch_image_with_mime_type", None)
        if callable(mime_fetcher):
            result = mime_fetcher(image_url)
            if result:
                content, mime_type = result
                if content:
                    return content, mime_type
            return None, None
        content = helper.fetch_image(image_url)
        if content:
            return content, None
    except Exception as err:
        logger.debug(f"飞书图片改用回退下载通路：{image_url}（{err}）")
    return None, None


def _fetch_remote_image(image_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """取图：先试宿主 ImageHelper，再走回退下载器。"""
    content, content_type = _fetch_via_image_helper(image_url)
    if content:
        return content, content_type
    return fetch_image_response(image_url, proxy_host=_runtime_proxy_host())


def _guess_suffix(
        instance: Any, image_url: str, content_type: Optional[str]
) -> str:
    """推断临时文件后缀：优先复用宿主方法，其次 MIME，再次 URL 后缀，默认 .jpg。"""
    guesser: Optional[Callable[..., str]] = getattr(
        instance, "_guess_image_suffix", None
    )
    if callable(guesser):
        try:
            suffix = guesser(image_url=image_url, content_type=content_type)
            if suffix:
                return suffix
        except Exception:
            pass

    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_type in _MIME_SUFFIX_MAP:
        return _MIME_SUFFIX_MAP[normalized_type]
    path_suffix = Path(urlparse(image_url).path).suffix.lower()
    if path_suffix in _IMAGE_SUFFIXES:
        return path_suffix
    return ".jpg"


def _looks_like_valid_image(
        instance: Any, image_url: str, content_type: Optional[str], content: bytes
) -> bool:
    """复用宿主的内容校验（存在时），避免把普通网页当成图片传给飞书。"""
    validator: Optional[Callable[..., bool]] = getattr(
        instance, "_is_supported_remote_image_response", None
    )
    if not callable(validator):
        return True
    try:
        return bool(validator(image_url, content_type, content))
    except Exception:
        return True


def _patched_upload_remote_image(
        self: Any, image_url: Optional[str]
) -> Optional[str]:
    """
    替换宿主 ``Feishu._upload_remote_image`` 的实现。

    与宿主原实现的差异只有取图这一段：优先 ``ImageHelper``，回退自建下载器
    （豆瓣补 Referer 且不走代理，其它 URL 走 PROXY_HOST）。其余行为——两个特例、
    写临时文件、调用 ``Feishu._upload_image``、失败日志与临时文件清理——保持不变。
    """
    image_url = (image_url or "").strip()
    if not image_url:
        return None
    if image_url.startswith("feishu://image/"):
        resource_path = image_url.replace("feishu://image/", "", 1)
        return resource_path.rsplit("/", 1)[-1].strip() or None

    temp_path: Optional[Path] = None
    try:
        content, content_type = _fetch_remote_image(image_url)
        if not content:
            logger.warning(f"飞书图片下载失败：{image_url}")
            return None
        if not _looks_like_valid_image(self, image_url, content_type, content):
            logger.warning(f"飞书图片地址不是有效图片：{image_url}, content_type={content_type}")
            return None
        suffix = _guess_suffix(self, image_url, content_type)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fp:
            fp.write(content)
            temp_path = Path(fp.name)
        image_key = self._upload_image(temp_path)
        if not image_key:
            logger.warning(f"飞书图片上传失败：{image_url}")
            return None
        return image_key
    except Exception as err:
        logger.error(f"飞书远程图片上传失败：{err}")
        return None
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception as err:
                logger.debug(f"删除飞书临时图片失败：{err}")


def _retarget_method_filename(new_method: Any, original_method: Any) -> Any:
    """
    把补丁方法的 ``co_filename`` 对齐到原方法，便于日志与回溯归位。

    做法与宿主内既有插件（curetmdbanimeshy）的 ``_retarget_method_filename`` 一致。
    """
    original_func = original_method
    if isinstance(original_method, (staticmethod, classmethod)):
        original_func = original_method.__func__

    target_filename = getattr(
        getattr(original_func, "__code__", None), "co_filename", None
    )
    if not target_filename:
        return new_method

    is_static = isinstance(new_method, staticmethod)
    is_class = isinstance(new_method, classmethod)
    patch_func = getattr(new_method, "__func__", new_method)
    patch_code = getattr(patch_func, "__code__", None)

    if not patch_code or not hasattr(patch_code, "replace"):
        return new_method

    try:
        patch_func.__code__ = patch_code.replace(co_filename=target_filename)
    except Exception as err:
        logger.debug(f"方法文件名对齐失败，保留原补丁实现：{err}")
        return new_method

    if is_static:
        return staticmethod(patch_func)
    if is_class:
        return classmethod(patch_func)
    return patch_func


def _build_patched_method(original_method: Any) -> Any:
    """构造替换方法：保留原方法元信息并打上幂等标记。"""
    original_func = getattr(original_method, "__func__", original_method)

    @wraps(original_func)
    def wrapper(self: Any, image_url: Optional[str] = None) -> Optional[str]:
        return _patched_upload_remote_image(self, image_url)

    setattr(wrapper, PATCH_FLAG_ATTR, True)
    return _retarget_method_filename(wrapper, original_method)


def apply_patch() -> Tuple[bool, str]:
    """
    给宿主 ``Feishu._upload_remote_image`` 打补丁（幂等）。

    :return: ``(本次是否新打了补丁, 说明)``
    """
    global _original_method
    try:
        module = importlib.import_module(PATCH_TARGET_MODULE)
        target = getattr(module, PATCH_TARGET_CLASS)
    except Exception as err:
        return False, f"无法导入宿主飞书模块 {PATCH_TARGET_MODULE}：{err}"

    current = getattr(target, PATCH_TARGET_METHOD, None)
    if current is None:
        return False, f"宿主 {PATCH_TARGET_CLASS}.{PATCH_TARGET_METHOD} 不存在，跳过打补丁"

    with _patch_lock:
        # 双重检查：避免并发或重复调用时包两层
        current = getattr(target, PATCH_TARGET_METHOD, None)
        if getattr(current, PATCH_FLAG_ATTR, False):
            return False, "补丁已存在，跳过重复打补丁"
        try:
            original = _original_method or getattr(target, ORIGINAL_ATTR, None)
            if original is None:
                original = inspect.getattr_static(target, PATCH_TARGET_METHOD)
        except AttributeError as err:
            return False, f"未能在类上定位原方法：{err}"
        _original_method = original
        setattr(target, ORIGINAL_ATTR, original)
        setattr(target, PATCH_TARGET_METHOD, _build_patched_method(original))

    return True, f"已替换 {PATCH_TARGET_CLASS}.{PATCH_TARGET_METHOD}"


def revert_patch() -> bool:
    """
    还原宿主 ``Feishu._upload_remote_image``。

    :return: 是否真的做了还原
    """
    global _original_method
    try:
        module = importlib.import_module(PATCH_TARGET_MODULE)
        target = getattr(module, PATCH_TARGET_CLASS)
    except Exception as err:
        logger.error(f"还原飞书图片补丁失败，无法导入宿主模块：{err}")
        return False

    with _patch_lock:
        original = _original_method or getattr(target, ORIGINAL_ATTR, None)
        if original is None:
            return False
        try:
            setattr(target, PATCH_TARGET_METHOD, original)
        except Exception as err:
            logger.error(f"还原飞书图片补丁失败：{err}")
            return False
        _original_method = None
        try:
            delattr(target, ORIGINAL_ATTR)
        except Exception:
            pass
    return True


def is_patched() -> bool:
    """当前宿主方法是否已被本插件替换。"""
    try:
        module = importlib.import_module(PATCH_TARGET_MODULE)
        target = getattr(module, PATCH_TARGET_CLASS)
    except Exception:
        return False
    current = getattr(target, PATCH_TARGET_METHOD, None)
    return bool(getattr(current, PATCH_FLAG_ATTR, False))


def inspect_host_status() -> Tuple[str, str]:
    """
    读取宿主方法源码并判定状态。

    若方法已被本插件替换，则改用保存下来的原方法源码做判定——补丁方法的
    ``co_filename`` 已被对齐到宿主文件，直接读取会读到错位的行。

    :return: ``(status, reason)``
    """
    try:
        from app.modules.feishu.feishu import Feishu
    except Exception as err:
        return STATUS_UNKNOWN, f"无法导入宿主飞书模块：{err}"

    method = getattr(Feishu, PATCH_TARGET_METHOD, None)
    if method is None:
        return STATUS_UNKNOWN, f"宿主 Feishu 上未找到 {PATCH_TARGET_METHOD} 方法，无法判断，请人工确认"

    if getattr(method, PATCH_FLAG_ATTR, False):
        # 已打过补丁，回到原方法取源码
        saved = _original_method or getattr(Feishu, ORIGINAL_ATTR, None)
        if saved is not None:
            method = getattr(saved, "__func__", saved)

    try:
        source = inspect.getsource(method)
    except Exception as err:
        return STATUS_UNKNOWN, f"无法读取 {PATCH_TARGET_METHOD} 源码：{err}，请人工确认"

    status = detect_patch_status(source)
    return status, describe_patch_reason(source, status)


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #

class FeishuImageFix(_PluginBase):
    # 插件名称
    plugin_name = "飞书图片修复"
    # 插件描述
    plugin_desc = "给宿主飞书模块的图片下载补上豆瓣 Referer 与 TMDB 代理；上游修好后可删除本插件。"
    # 插件版本
    plugin_version = "0.1.0"
    # 插件作者
    plugin_author = "Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "feishuimagefix_"
    # 加载顺序
    plugin_order = 9999
    # 可使用的用户级别
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        # 最近一次检测结果
        self._state: Optional[Dict[str, Any]] = None

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """生效配置：检测宿主方法结构，按需打补丁，并落盘检测结论。"""
        status, reason = inspect_host_status()
        patched_now = False

        if status == STATUS_PATCHED:
            patched_now, patch_message = apply_patch()
            reason = f"{reason}；{patch_message}"
            if patched_now:
                logger.info("飞书图片修复：检测到宿主尚未修复，已打补丁")
            else:
                logger.info(f"飞书图片修复：{patch_message}")
        elif status == STATUS_UPSTREAM_FIXED:
            logger.warning("飞书图片修复：上游已修复，可以删除本插件")
            if is_patched():
                # 极少数情况下补丁还在，顺手还原，避免叠加
                if revert_patch():
                    logger.info("飞书图片修复：检测到上游已修复，已还原此前的补丁")
        else:
            logger.warning(f"飞书图片修复：无法判断宿主结构，请人工确认（{reason}）")
            if is_patched() and revert_patch():
                logger.info("飞书图片修复：结构未知，已还原此前的补丁")

        self._state = {
            "status": status,
            "reason": reason,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "app_version": get_app_version(),
        }
        self.save_data("state", self._state)

    def stop_service(self) -> None:
        """停用插件：还原宿主方法。"""
        try:
            if revert_patch():
                logger.info("飞书图片修复：已还原宿主飞书图片下载方法")
        except Exception as err:
            logger.error(f"飞书图片修复：停止插件时还原失败：{err}")

    def get_state(self) -> bool:
        """插件本身不提供开关，加载即生效。"""
        return True

    def get_api(self) -> List[Dict[str, Any]]:
        """本插件不提供 API。"""
        return []

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """配置页：只用 vuetify 组件把说明讲清楚，不提供任何用户配置项。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "class": "mb-2",
                                        },
                                        "text": "本插件不需要任何配置：插件启动时会自动读取宿主飞书模块的图片下载方法，"
                                                "判断上游是否已经处理好 Referer 与代理，只有确认未修复时才打补丁。",
                                    },
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                        },
                                        "text": "上游修好之后，插件会在日志和插件详情页提示“可以删除本插件”，"
                                                "届时卸载即可，不会影响宿主功能。",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ], {}

    def get_page(self) -> List[Dict[str, Any]]:
        """详情页：展示当前状态、判据依据、检测时间与程序版本。"""
        state = self._load_state()
        status = str(state.get("status") or STATUS_UNKNOWN)
        alert_type = _STATUS_ALERT_TYPE.get(status, "warning")
        label = _STATUS_LABEL.get(status, _STATUS_LABEL[STATUS_UNKNOWN])

        reason = state.get("reason") or "尚无检测记录，插件可能还没完成启动。"
        checked_at = state.get("checked_at") or "未记录"
        app_version = state.get("app_version") or get_app_version()

        def detail_line(text: str) -> Dict[str, Any]:
            return {
                "component": "div",
                "props": {"class": "text-body-2 py-1"},
                "text": text,
            }

        return [
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
                "props": {
                    "variant": "tonal",
                    "class": "mb-2",
                },
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": "检测详情",
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            detail_line(f"检测对象：{PATCH_TARGET_CLASS}.{PATCH_TARGET_METHOD}"
                                        f"（{PATCH_TARGET_MODULE}）"),
                            detail_line(f"判据：源码含 referer 或 ImageHelper → 上游已修复，不打补丁；"
                                        f"含 RequestUtils( 且两者皆无 → 判定未修复并打补丁；两者皆无 → 结构未知"),
                            detail_line(f"检测依据：{reason}"),
                            detail_line(f"检测时间：{checked_at}"),
                            detail_line(f"当前程序版本：{app_version}"),
                        ],
                    },
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                },
                "text": "补丁只替换内存中的类方法，不改动宿主任何文件；停用本插件即自动还原。",
            },
        ]

    def _load_state(self) -> Dict[str, Any]:
        """读取最近一次检测结果，内存优先，其次数据库。"""
        if isinstance(self._state, dict) and self._state:
            return self._state
        try:
            state = self.get_data("state")
        except Exception as err:
            logger.debug(f"飞书图片修复：读取插件数据失败：{err}")
            return {}
        return state if isinstance(state, dict) else {}
