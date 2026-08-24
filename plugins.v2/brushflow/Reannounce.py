import time
from typing import Any, Callable, Optional

DEFAULT_ANNOUNCE_TIMES = 15
DEFAULT_INTERVAL = 330
FIRST_ANNOUNCE_DELAY = 180


def __qb_reannounce(qbc_provider: Callable[[], Optional[Any]], torrent_hash: str) -> bool:
    """
    通过 MoviePilot 已登录的 qBittorrent 客户端执行一次重新宣告。

    每次调用都通过 qbc_provider 实时获取当前可用的 qbc 客户端
    （复用 MoviePilot 登录会话与鉴权，下载器重连后也能拿到新实例），
    调用 qbc.torrents_reannounce 走 qBittorrent Web API 的 torrents/reannounce。
    """
    try:
        qbc = qbc_provider() if callable(qbc_provider) else None
        reannounce = getattr(qbc, "torrents_reannounce", None) if qbc else None
        if not callable(reannounce):
            return False
        reannounce(torrent_hashes=[torrent_hash])
        return True
    except Exception:
        return False


def trigger_reannounce_task(qbc_provider: Callable[[], Optional[Any]], torrent_hash: str, tags: str = "",
                            interval: int = DEFAULT_INTERVAL,
                            announce_times: int = DEFAULT_ANNOUNCE_TIMES):
    """
    定时重复重新宣告：等待首次延迟后，按间隔重复宣告指定次数；
    任一次宣告失败（客户端不可用/调用异常）即终止本轮宣告。
    """
    try:
        time.sleep(FIRST_ANNOUNCE_DELAY)
        for i in range(announce_times):
            success = __qb_reannounce(qbc_provider, torrent_hash)
            if not success:
                break
            if i < announce_times - 1:
                time.sleep(interval)
    except Exception:
        pass
