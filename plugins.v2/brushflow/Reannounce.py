import time
import requests
from typing import Optional

DEFAULT_ANNOUNCE_TIMES = 15
DEFAULT_INTERVAL = 330
FIRST_ANNOUNCE_DELAY = 180


def __simple_http_reannounce(base_url: str, torrent_hash: str) -> bool:
    api_url = f"{base_url}/api/v2/torrents/reannounce"
    try:
        payload = {"hashes": torrent_hash}
        response = requests.post(api_url, data=payload, timeout=10)
        if response.status_code == 200 and not response.text:
            return True
        else:
            return False
    except requests.exceptions.RequestException:
        return False
    except Exception:
        return False


def trigger_reannounce_task(base_url: str, torrent_hash: str, tags: str = "",
                            interval: int = DEFAULT_INTERVAL,
                            announce_times: int = DEFAULT_ANNOUNCE_TIMES):
    try:
        time.sleep(FIRST_ANNOUNCE_DELAY)
        for i in range(announce_times):
            success = __simple_http_reannounce(base_url, torrent_hash)
            if not success:
                break
            if i < announce_times - 1 and success:
                time.sleep(interval)
    except Exception:
        pass
