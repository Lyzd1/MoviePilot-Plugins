import time
import logging
import requests
from typing import Optional

# 使用 MoviePilot 的日志系统
logger = logging.getLogger(__name__)

# 默认参数
DEFAULT_ANNOUNCE_TIMES = 15
DEFAULT_INTERVAL = 330
FIRST_ANNOUNCE_DELAY = 180

def log_and_print(message, level="debug"):
    """统一日志记录和打印的函数"""
    if level == "info":
        logger.info(message)
    elif level == "error":
        logger.error(message)
    elif level == "warning":
        logger.warning(message)
    elif level == "debug":
        logger.debug(message)

def __simple_http_reannounce(base_url: str, torrent_hash: str) -> bool:
    """
    简化的HTTP汇报函数 - 直接调用API，无需认证
    """
    api_url = f"{base_url}/api/v2/torrents/reannounce"
    
    try:
        payload = {"hashes": torrent_hash}
        
        log_and_print(f"发送汇报请求到: {api_url}", "debug")
        response = requests.post(api_url, data=payload, timeout=10)
        
        # qBittorrent成功返回200且body为空
        if response.status_code == 200 and not response.text:
            log_and_print(f"汇报成功 - Hash: {torrent_hash}", "debug")
            return True
        else:
            log_and_print(f"汇报失败 - 状态码: {response.status_code}, 响应: {response.text[:100]}", "error")
            return False
            
    except requests.exceptions.RequestException as e:
        log_and_print(f"HTTP请求异常: {e}", "error")
        return False
    except Exception as e:
        log_and_print(f"汇报过程中发生未知错误: {e}", "error")
        return False

def trigger_reannounce_task(base_url: str, torrent_hash: str, tags: str = "", 
                           interval: int = DEFAULT_INTERVAL, 
                           announce_times: int = DEFAULT_ANNOUNCE_TIMES):
    """
    简化的汇报任务触发函数
    只需要base_url和torrent_hash，无需认证
    """
    try:
        # 检查是否跳过（辅种）
        if "辅种" in tags:
            log_and_print(f"种子 {torrent_hash} 标签包含'辅种'，跳过汇报处理", "info")
            return

        log_and_print(f"开始为种子 {torrent_hash} 执行汇报任务...", "info")
        log_and_print(f"下载器URL: {base_url}", "debug")
        log_and_print(f"汇报间隔: {interval}秒, 总次数: {announce_times}", "debug")

        # 延迟第一次汇报
        log_and_print(f"种子 {torrent_hash}: 第一次汇报延迟 {FIRST_ANNOUNCE_DELAY} 秒...", "debug")
        time.sleep(FIRST_ANNOUNCE_DELAY)

        # 循环汇报
        for i in range(announce_times):
            success = __simple_http_reannounce(base_url, torrent_hash)
            
            if success:
                log_and_print(f"种子 {torrent_hash}: 第 {i + 1}/{announce_times} 次汇报成功", "debug")
            else:
                log_and_print(f"种子 {torrent_hash}: 第 {i + 1}/{announce_times} 次汇报失败", "error")
                break  # 如果失败就停止

            # 等待间隔（最后一次不需要等待）
            if i < announce_times - 1 and success:
                log_and_print(f"种子 {torrent_hash}: 等待 {interval} 秒进行下一次汇报...", "debug")
                time.sleep(interval)

        if success:
            log_and_print(f"种子 {torrent_hash}: 汇报任务完成", "info")
        else:
            log_and_print(f"种子 {torrent_hash}: 汇报任务中止", "error")
            
    except Exception as e:
        log_and_print(f"种子 {torrent_hash}: 汇报任务发生意外错误: {e}", "error")
