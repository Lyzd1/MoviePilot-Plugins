"""歌词源实现包。"""

from __future__ import annotations

from .base import BaseProvider
from .kugou import KugouProvider
from .net_ease import NetEaseProvider
from .qq_music import QQMusicProvider

#: 源名称 → Provider 类
PROVIDER_CLASSES = {
    NetEaseProvider.name: NetEaseProvider,
    QQMusicProvider.name: QQMusicProvider,
    KugouProvider.name: KugouProvider,
}

#: 默认启用顺序（优先级即此顺序）
DEFAULT_SOURCES = (NetEaseProvider.name, QQMusicProvider.name, KugouProvider.name)

__all__ = [
    "BaseProvider",
    "NetEaseProvider",
    "QQMusicProvider",
    "KugouProvider",
    "PROVIDER_CLASSES",
    "DEFAULT_SOURCES",
]
