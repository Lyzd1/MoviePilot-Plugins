"""
简单的进程内 LRU + TTL 缓存。

不引入第三方依赖；容量与 TTL 由插件配置固定。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class TTLCache:
    """
    线程安全的定容量 TTL 缓存。

    :param capacity: 最大条目数，超出时淘汰最久未使用项
    :param ttl: 条目存活秒数
    """

    def __init__(self, capacity: int, ttl: float) -> None:
        self._capacity = max(int(capacity), 1)
        self._ttl = float(ttl)
        self._data: "OrderedDict[Any, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Optional[Any]:
        """读取缓存；过期或不存在时返回 None。"""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if time.monotonic() >= expires_at:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Any, value: Any) -> None:
        """写入或刷新缓存条目。"""
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
