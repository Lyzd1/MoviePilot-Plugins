import time
import requests
from typing import Optional

# 默认参数
DEFAULT_ANNOUNCE_TIMES = 15
DEFAULT_INTERVAL = 330
FIRST_ANNOUNCE_DELAY = 180

def __simple_http_reannounce(base_url: str, torrent_hash: str) -> bool:
    """
    简化的HTTP汇报函数 - 直接调用API，无需认证
    """
    api_url = f"{base_url}/api/v2/torrents/reannounce"
    
    try:
        payload = {"hashes": torrent_hash}
        
        response = requests.post(api_url, data=payload, timeout=10)
        
        # qBittorrent成功返回200且body为空
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
    """
    简化的汇报任务触发函数
    只需要base_url和torrent_hash，无需认证
    """
    try:

        # 延迟第一次汇报
        time.sleep(FIRST_ANNOUNCE_DELAY)

        # 循环汇报
        for i in range(announce_times):
            success = __simple_http_reannounce(base_url, torrent_hash)
            
            if not success:
                break  # 如果失败就停止

            # 等待间隔（最后一次不需要等待）
            if i < announce_times - 1 and success:
                time.sleep(interval)
            
    except Exception:
        pass
