import time
import logging
import requests  # 重新导入 requests 用于手动发送 HTTP 请求
from typing import Optional, Union, List
from urllib.parse import urljoin  # 用于构造完整的 URL

# --- 配置 ---

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,  # 日志级别设置为 DEBUG 以显示定时汇报信息
    format="%(asctime)s - [%(levelname)s] - (Reannounce) - %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

# 默认参数
DEFAULT_ANNOUNCE_TIMES = 15
DEFAULT_INTERVAL = 330
FIRST_ANNOUNCE_DELAY = 180

# --- 核心功能 ---

def log_and_print(message, level="debug"):
    """
    统一日志记录和打印的函数。
    定时汇报的日志将使用 DEBUG 级别。
    """
    if level == "info":
        logging.info(message)
    elif level == "error":
        logging.error(message)
    elif level == "warning":
        logging.warning(message)
    elif level == "debug":
        logging.debug(message)
        

def __get_base_url_and_auth(downloader: any) -> Optional[dict]:
    """
    通过 MoviePilot 的 downloader 对象获取 base_url 和认证信息。
    """
    try:
        # 1. 检查下载器类型并获取基础 URL
        downloader_type = downloader.__class__.__name__
        
        # 获取连接信息：MoviePilot 的下载器实例通常将配置存储在 _config 或 config 属性中
        config = getattr(downloader, '_config', None) or getattr(downloader, 'config', None)
        
        if not config:
            log_and_print("无法从 downloader 对象获取配置信息。", "error")
            return None

        host = config.get('host')
        port = config.get('port')
        username = config.get('username')
        password = config.get('password')
        
        if not host or not port:
            log_and_print("配置信息中缺少 host 或 port。", "error")
            return None

        # 构造基础 URL (e.g., http://host:port)
        # 兼容 qBittorrent/Transmission 的默认协议
        scheme = 'https' if config.get('ssl') else 'http'
        base_url = f"{scheme}://{host}:{port}"

        # 2. 返回认证信息和 API 路径 (仅支持 qBittorrent 风格的 API)
        if downloader_type == 'Qbittorrent':
            return {
                "type": "qbittorrent",
                "base_url": base_url,
                "api_path": "/api/v2/torrents/reannounce",
                "auth": (username, password)
            }
        
        # Transmission 客户端的 RPC 机制复杂，不适合用此方式直接调用
        return None

    except Exception as e:
        log_and_print(f"获取下载器 URL 和认证信息失败: {e}", "error")
        return None


def __force_http_reannounce(base_info: dict, torrent_hash: str) -> bool:
    """
    手动构造 HTTP 请求进行强制汇报 (针对 qBittorrent)
    流程：登录 -> 使用会话 Cookie 发送汇报请求。
    """
    
    api_url = urljoin(base_info["base_url"], base_info["api_path"])
    auth = base_info["auth"]

    payload = {
        "hashes": torrent_hash
    }
    
    try:
        session = requests.Session()
        
        # 1. 登录 (获取 Session Cookie)
        login_url = urljoin(base_info["base_url"], "/api/v2/auth/login")
        login_data = {'username': auth[0], 'password': auth[1]}
        
        login_response = session.post(login_url, data=login_data, timeout=10, allow_redirects=True)
        
        if "Fails" in login_response.text or login_response.status_code != 200:
            log_and_print(f"qBittorrent 登录失败，请检查用户名/密码/2FA: {login_response.text[:50]}...", "error")
            return False

        # 2. 执行汇报操作
        log_and_print(f"发送汇报请求到: {api_url}", "debug")
        response = session.post(api_url, data=payload, timeout=10)

        # qBittorrent 成功返回 200 且 body 为空
        if response.status_code == 200 and not response.text:
            return True
        else:
            log_and_print(f"HTTP 汇报请求失败，状态码: {response.status_code}, 响应: {response.text[:50]}...", "error")
            return False

    except requests.exceptions.RequestException as e:
        log_and_print(f"HTTP 请求异常: {e}", "error")
        return False
    except Exception as e:
        log_and_print(f"汇报过程中发生未知错误: {e}", "error")
        return False


def trigger_reannounce_task(downloader, torrent_hash: str, tags: str = "", interval: int = DEFAULT_INTERVAL, announce_times: int = DEFAULT_ANNOUNCE_TIMES):
    """
    核心函数：优先使用 HTTP API 模式，失败后回退到抽象方法。
    """
    
    try:
        # 1. 检查是否跳过 (辅种)
        if "辅种" in tags:
            message = f"种子 {torrent_hash} 标签包含“辅种”，跳过汇报处理。"
            log_and_print(message, "info")
            return

        log_and_print(f"开始为种子 {torrent_hash} 执行汇报任务...", "info")
        log_and_print(f"使用的汇报间隔时间(秒): {interval}", "debug")
        log_and_print(f"使用的总汇报次数: {announce_times}", "debug")

        # 尝试获取连接信息，用于判断是否能使用 HTTP API 模式
        base_info = __get_base_url_and_auth(downloader)
        use_http_api = base_info and base_info.get("type") == "qbittorrent"
        
        if use_http_api:
            log_and_print(f"启用 HTTP API 模式汇报 (URL: {base_info['base_url']}{base_info['api_path']})", "debug")
        else:
            log_and_print("未启用 HTTP API 模式 (非 qBittorrent)，将使用抽象方法。", "warning")
        
        # 2. 延迟第一次汇报
        log_and_print(f"种子 {torrent_hash}: 第一次汇报延迟 {FIRST_ANNOUNCE_DELAY} 秒...", "debug")
        time.sleep(FIRST_ANNOUNCE_DELAY)

        # 3. 循环汇报
        for i in range(announce_times):
            success = False
            
            # 检查下载器连接
            if not downloader or not downloader.is_connected:
                log_and_print(f"种子 {torrent_hash}: 下载器未连接，终止汇报任务。", "error")
                return

            # 强制汇报逻辑
            try:
                if use_http_api:
                    # 优先使用手动构造的 HTTP 请求
                    success = __force_http_reannounce(base_info, torrent_hash)
                    
                if not success:
                    # 如果 HTTP API 模式失败，或未启用，使用抽象方法作为回退/主要方式 (适用于 Transmission)
                    downloader.reannounce_torrents(torrent_hash)
                    success = True 
                    log_and_print(f"种子 {torrent_hash}: 抽象方法汇报成功。", "debug")
                
                message = f"种子 {torrent_hash}: 第 {i + 1}/{announce_times} 次汇报成功。"
                log_and_print(message, "debug") 
                
            except Exception as e:
                message = f"种子 {torrent_hash}: 第 {i + 1}/{announce_times} 次汇报失败: {e}"
                log_and_print(message, "error")
                success = False

            # 4. 等待间隔
            if i < announce_times - 1 and success:
                log_and_print(f"种子 {torrent_hash}: 等待 {interval} 秒进行下一次汇报...", "debug")
                time.sleep(interval)
            elif not success:
                log_and_print(f"种子 {torrent_hash}: 汇报失败，任务中止。", "error")
                return
            else:
                log_and_print(f"种子 {torrent_hash}: 汇报任务完成。", "info")
                
    except Exception as e:
        log_and_print(f"种子 {torrent_hash}: 汇报任务线程发生意外错误: {e}", "error")

# 确保文件作为模块被导入使用，不要保留 __name__ == "__main__" 命令行执行逻辑