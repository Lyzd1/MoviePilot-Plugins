"""
同步 HTTP 客户端：按域名串行限速、单请求超时、单曲总预算与有限重试。

- 每个域名一把锁，相邻请求至少间隔 ``interval`` 秒；
- 单请求超时取 ``min(配置超时, 剩余总预算)``；
- 403 / 429 / 503 最多指数退避重试 2 次，仍失败则抛出 ``ProviderError``；
- 所有异常都带分类信息，交由 Provider / 插件层记日志，不静默吞掉。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .log import logger

# 进程级、按域名的限速状态
_DOMAIN_LOCKS: "dict[str, threading.Lock]" = {}
_DOMAIN_LAST: "dict[str, float]" = {}
_DOMAIN_GUARD = threading.Lock()

# 触发退避重试的状态码
_RETRY_STATUS = (403, 429, 503)
_MAX_RETRIES = 2
_RETRY_BASE_SECONDS = 0.5

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class ProviderError(Exception):
    """歌词源请求失败（网络、状态码、响应解析等）。"""


class DeadlineExceeded(ProviderError):
    """单曲总预算已耗尽。"""


def _domain_of(url: str) -> str:
    """提取 URL 的域名，用作限速键。"""
    return (urlparse(url).netloc or "").lower()


def _domain_lock(domain: str) -> threading.Lock:
    """取得（或创建）某个域名的串行锁。"""
    with _DOMAIN_GUARD:
        lock = _DOMAIN_LOCKS.get(domain)
        if lock is None:
            lock = threading.Lock()
            _DOMAIN_LOCKS[domain] = lock
        return lock


def _wait_for_domain(domain: str, interval: float) -> None:
    """
    同一域名串行等待，保证相邻请求至少间隔 ``interval`` 秒。

    时间戳记录在进程级字典里，多个插件实例 / 线程共享同一节奏。
    """
    if interval <= 0:
        return
    lock = _domain_lock(domain)
    with lock:
        now = time.monotonic()
        last = _DOMAIN_LAST.get(domain, 0.0)
        delay = interval - (now - last)
        if delay > 0:
            time.sleep(delay)
        _DOMAIN_LAST[domain] = time.monotonic()


class HttpClient:
    """
    插件共享的同步 HTTP 客户端。

    :param timeout: 单请求超时秒数
    :param interval: 同域名相邻请求的最小间隔秒数
    :param user_agent: 默认 User-Agent
    """

    def __init__(self, timeout: float = 8.0, interval: float = 0.3,
                 user_agent: str = DEFAULT_USER_AGENT) -> None:
        self._timeout = max(float(timeout), 1.0)
        self._interval = max(float(interval), 0.0)
        self._client = httpx.Client(
            timeout=self._timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    def _effective_timeout(self, deadline: Optional[float]) -> float:
        """在总预算内取单请求超时。"""
        if deadline is None:
            return self._timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("单歌歌词预算已耗尽")
        return max(0.5, min(self._timeout, remaining))

    @staticmethod
    def _sleep_budget(seconds: float, deadline: Optional[float]) -> None:
        """在总预算内退避等待。"""
        if seconds <= 0:
            return
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded("单歌歌词预算已耗尽")
            seconds = min(seconds, remaining)
        time.sleep(seconds)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        deadline: Optional[float] = None,
    ) -> httpx.Response:
        """
        执行一次请求并返回仍处于打开状态的响应，调用方负责关闭。

        :raises DeadlineExceeded: 总预算耗尽
        :raises ProviderError: 网络错误、非 2xx 状态或重试后仍被限流
        """
        domain = _domain_of(url)
        attempts = 0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded("单歌歌词预算已耗尽")
            _wait_for_domain(domain, self._interval)
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self._effective_timeout(deadline),
                )
            except httpx.TimeoutException as error:
                raise ProviderError(f"请求超时：{domain}（{error.__class__.__name__}）") from error
            except httpx.HTTPError as error:
                raise ProviderError(f"网络异常：{domain}（{error.__class__.__name__}）") from error

            status = response.status_code
            if status in _RETRY_STATUS and attempts < _MAX_RETRIES:
                wait = _RETRY_BASE_SECONDS * (2 ** attempts)
                attempts += 1
                logger.warning(
                    f"【多源歌词】{domain} 返回 {status}，"
                    f"{wait:g} 秒后重试（第 {attempts}/{_MAX_RETRIES} 次）"
                )
                response.close()
                self._sleep_budget(wait, deadline)
                continue
            if status in _RETRY_STATUS:
                response.close()
                raise ProviderError(f"HTTP {status}：{domain} 重试后仍失败")
            if status >= 400:
                body = ""
                try:
                    body = response.text[:200]
                except Exception:
                    pass
                response.close()
                raise ProviderError(f"HTTP {status}：{domain} {body}")
            return response

    def get_json(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        deadline: Optional[float] = None,
    ) -> Any:
        """GET 并解析 JSON。"""
        response = self.request("GET", url, params=params, headers=headers, deadline=deadline)
        try:
            try:
                return response.json()
            except ValueError as error:
                raise ProviderError(f"响应不是合法 JSON：{_domain_of(url)}") from error
        finally:
            response.close()

    def get_text(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        deadline: Optional[float] = None,
    ) -> str:
        """GET 并返回文本（用于 QQ 的 JSONP 响应）。"""
        response = self.request("GET", url, params=params, headers=headers, deadline=deadline)
        try:
            return response.text
        finally:
            response.close()

    def post_form_json(
        self,
        url: str,
        *,
        data: dict[str, Any],
        headers: Optional[dict[str, str]] = None,
        deadline: Optional[float] = None,
    ) -> Any:
        """POST 表单并解析 JSON（用于网易 weapi）。"""
        response = self.request("POST", url, data=data, headers=headers, deadline=deadline)
        try:
            try:
                return response.json()
            except ValueError as error:
                raise ProviderError(f"响应不是合法 JSON：{_domain_of(url)}") from error
        finally:
            response.close()

    def close(self) -> None:
        """关闭底层连接池。"""
        try:
            self._client.close()
        except Exception as error:  # pragma: no cover - 关闭失败不影响主流程
            logger.debug(f"【多源歌词】关闭 HTTP 客户端失败：{error}")
