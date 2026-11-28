"""
日志适配：优先使用 MoviePilot 宿主日志，脱离宿主时回退到标准库 logging。
"""

from __future__ import annotations

try:  # MoviePilot 运行时
    from app.sdk.logging import logger  # type: ignore
except Exception:  # pragma: no cover - 离线测试 / 脱离宿主
    import logging

    logger = logging.getLogger("musiclyricsmulti")
