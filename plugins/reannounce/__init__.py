import time
import threading
from typing import Dict, Any, Optional

# 导入 MoviePilot 必要的模块
from app.log import logger
from app.plugins import _PluginBase
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from app.helper.downloader import DownloaderHelper

# 配置常量 (可作为插件配置项，此处沿用原脚本的默认值)
DEFAULT_ANNOUNCE_TIMES = 15     # 默认汇报次数
DEFAULT_INTERVAL = 330          # 默认间隔时间(秒): 5分30秒
FIRST_ANNOUNCE_DELAY = 180      # 第一次汇报延迟(秒): 3分钟


class AutoReannounce(_PluginBase):
    # 插件元数据
    plugin_name = "自动强制汇报（Reannounce）"
    plugin_desc = "下载任务添加后，自动调用下载器API进行多次强制汇报，消除辅种/刷流时的连接性问题。"
    plugin_version = "1.0"
    plugin_author = "参考原脚本"
    LOG_TAG = "[AutoReannounce] "
    
    # 插件配置（此处为简化，默认使用硬编码常量）
    # 实际项目中，建议在 get_config() 中配置，并通过 self.plugin_config 获取
    
    def init_service(self, config: dict) -> bool:
        """初始化服务，加载配置"""
        # 可以从 config 中加载 interval 和 announce_times
        self._interval = config.get("interval", DEFAULT_INTERVAL)
        self._announce_times = config.get("announce_times", DEFAULT_ANNOUNCE_TIMES)
        logger.info(f"{self.LOG_TAG}插件初始化成功。")
        return True

    def get_state(self):
        return True

    @eventmanager.register(EventType.DownloadAdded)
    def download_added(self, event: Event):
        """
        监听下载添加事件，并触发强制汇报工作线程。
        """
        event_data = event.event_data
        torrent_hash = event_data.get("hash")
        downloader_name = event_data.get("downloader")
        context = event_data.get("context")
        
        if not torrent_hash or not downloader_name or not context:
            logger.warning(f"{self.LOG_TAG}接收到的事件数据缺失 (Hash/Downloader/Context)，跳过处理。")
            return
            
        # 提取标签信息 (沿用原脚本的逻辑，用于跳过“辅种”任务)
        # 注意：这里我们使用 torrent_info.tags 作为标签列表（如果存在）
        torrent_tags_list = getattr(context.torrent_info, 'tags', [])
        
        # 将标签列表转为字符串，方便后续检查，或者直接检查列表
        if any("辅种" in tag for tag in torrent_tags_list):
            logger.info(f"{self.LOG_TAG}种子 {torrent_hash} 标签包含“辅种”，跳过自动汇报。")
            return
            
        # 启动一个新的线程来执行汇报，避免阻塞 MoviePilot 主线程
        worker_thread = threading.Thread(
            target=self._worker_reannounce,
            args=(downloader_name, torrent_hash, torrent_tags_list, self._interval, self._announce_times),
            daemon=True
        )
        worker_thread.start()
        
    def _worker_reannounce(self, downloader_name: str, torrent_hash: str, torrent_tags: list, interval: int, announce_times: int):
        """
        实际执行强制汇报的线程工作函数。
        """
        logger.info(f"{self.LOG_TAG}开始为种子 {torrent_hash} 执行强制汇报任务...")
        logger.info(f"{self.LOG_TAG}使用的下载器: {downloader_name}")
        logger.info(f"{self.LOG_TAG}汇报间隔时间(秒): {interval}，总汇报次数: {announce_times}")
        
        # 1. 获取下载器实例（不需要 BASE_URL，MP自带配置）
        downloader = DownloaderHelper().get_instance(downloader_name)
        if not downloader:
            logger.error(f"{self.LOG_TAG}无法获取下载器实例: {downloader_name}，请检查配置。")
            return

        # 2. 第一次汇报延迟
        if FIRST_ANNOUNCE_DELAY > 0:
            logger.info(f"{self.LOG_TAG}第一次汇报延迟 {FIRST_ANNOUNCE_DELAY} 秒...")
            time.sleep(FIRST_ANNOUNCE_DELAY)

        # 3. 执行多次汇报循环
        for i in range(1, announce_times + 1):
            try:
                # 假设 MP 的下载器模块（如 Qbittorrent/Transmission）有 reannounce 方法
                # 能够接收哈希列表并执行汇报操作。
                if hasattr(downloader, 'reannounce'):
                     downloader.reannounce([torrent_hash])
                else:
                     # 如果下载器模块没有直接的 reannounce 方法，则记录警告并退出
                     logger.warning(f"{self.LOG_TAG}下载器 {downloader_name} 实例没有 'reannounce' 方法。汇报失败。")
                     return
                     
                logger.info(f"{self.LOG_TAG}种子 {torrent_hash} 第 {i}/{announce_times} 次汇报成功。")
                
                if i < announce_times:
                    time.sleep(interval)
                
            except Exception as e:
                logger.error(f"{self.LOG_TAG}种子 {torrent_hash} 第 {i} 次汇报失败，异常信息: {e}")
                if i < announce_times:
                    time.sleep(interval) # 失败后仍等待，然后重试

        logger.info(f"{self.LOG_TAG}种子 {torrent_hash} 强制汇报任务完成。")