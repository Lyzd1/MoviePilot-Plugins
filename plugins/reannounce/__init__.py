import threading
import time
from typing import List, Tuple, Dict, Any, Optional

from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.core.event import eventmanager, Event
from app.schemas.types import EventType

class TorrentReannounce(_PluginBase):
    """
    一个MoviePilot插件，用于在添加新种子后自动执行多次强制汇报。
    原始逻辑基于 Reannounce.py 脚本。
    此插件仅支持 qBittorrent 下载器。
    """
    
    # 插件信息
    plugin_name = "种子自动汇报"
    plugin_desc = "新种子添加后，自动执行多次强制汇报（仅支持qBittorrent）"
    plugin_icon = "sync_alt.png"  # 使用一个Material Design图标
    plugin_version = "1.0"
    plugin_author = "Lyzd1"
    author_url = "https://github.com/Lyzd1"
    plugin_config_prefix = "Reannounce_"
    plugin_order = 100
    auth_level = 1
    LOG_TAG = "[TorrentReannounce] "

    # 私有属性（将从配置中读取）
    downloader_helper = None
    _enabled = False
    _downloaders = None
    _skip_tag = "辅种"
    _first_announce_delay = 180
    _interval = 330
    _announce_times = 15

    def init_plugin(self, config: dict = None):
        """
        初始化插件，读取配置
        """
        self.downloader_helper = DownloaderHelper()
        if config:
            self._enabled = config.get("enabled", False)
            self._downloaders = config.get("downloaders")
            self._skip_tag = config.get("skip_tag") or "辅种"
            self._first_announce_delay = self.str_to_number(config.get("first_announce_delay"), 180)
            self._interval = self.str_to_number(config.get("interval"), 330)
            self._announce_times = self.str_to_number(config.get("announce_times"), 15)

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """
        获取已连接的、受监控的下载器服务实例。
        (仿照 DownloadSiteTag 插件)
        """
        if not self._downloaders:
            logger.warning(f"{self.LOG_TAG}尚未配置下载器，请检查配置")
            return None

        services = self.downloader_helper.get_services(name_filters=self._downloaders)
        if not services:
            logger.warning(f"{self.LOG_TAG}获取下载器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"{self.LOG_TAG}下载器 {service_name} 未连接，请检查配置")
            else:
                active_services[service_name] = service_info

        if not active_services:
            logger.warning(f"{self.LOG_TAG}没有已连接的下载器，请检查配置")
            return None

        return active_services

    def get_state(self) -> bool:
        """
        返回插件启用状态
        """
        return self._enabled

    # --- 不需要API或后台服务 ---
    def get_command(self) -> List[Dict[str, Any]]:
        pass
    def get_api(self) -> List[Dict[str, Any]]:
        pass
    def get_service(self) -> List[Dict[str, Any]]:
        return []
    def stop_service(self):
        pass # 没有常驻调度器，无需停止

    @staticmethod
    def str_to_number(s: str, i: int) -> int:
        """
        字符串转数字，失败则返回默认值
        (来自 DownloadSiteTag 插件)
        """
        try:
            return int(s)
        except (ValueError, TypeError):
            return i

    @staticmethod
    def _get_label(torrent: Any, dl_type: str) -> list:
        """
        获取种子的标签列表
        (来自 DownloadSiteTag 插件，并稍作修正)
        """
        try:
            if dl_type == "qbittorrent":
                tags_str = torrent.get("tags", "")
                # 修正：确保空字符串不会返回 ['']
                return [str(tag).strip() for tag in tags_str.split(',') if str(tag).strip()]
            else:
                return torrent.labels or []
        except Exception as e:
            logger.error(f"获取种子标签时出错: {e}")
            return []

    def _force_reannounce(self, service: ServiceInfo, torrent_hash: str):
        """
        执行强制汇报 (仅qBittorrent)
        """
        if service.type != "qbittorrent":
            logger.warning(f"{self.LOG_TAG} 种子 {torrent_hash}：跳过汇报，因为下载器 {service.name} 不是 qBittorrent。")
            return
        
        try:
            # service.instance 即 DownloaderAbtract，它包含qbc客户端
            # 使用已封装的qbc客户端执行reannounce
            service.instance.qbc.torrents_reannounce(hashes=torrent_hash)
            logger.info(f"{self.LOG_TAG} 种子 {torrent_hash} 强制汇报成功！")
        except Exception as e:
            logger.error(f"{self.LOG_TAG} 种子 {torrent_hash} 强制汇报时出现异常：{e}")

    def _run_announce_loop(self, service: ServiceInfo, torrent_hash: str):
        """
        在后台线程中执行完整的汇报循环（包含延迟和等待）
        """
        try:
            logger.info(f"{self.LOG_TAG} 种子 {torrent_hash}: 等待 {self._first_announce_delay} 秒后进行第一次汇报...")
            time.sleep(self._first_announce_delay)
            
            for current_time in range(self._announce_times):
                logger.info(f"{self.LOG_TAG} 种子 {torrent_hash}: === 第 {current_time + 1} 轮汇报 (共 {self._announce_times} 轮) ===")
                self._force_reannounce(service, torrent_hash)
                
                # 如果不是最后一轮,则等待间隔时间
                if current_time < self._announce_times - 1:
                    logger.info(f"{self.LOG_TAG} 种子 {torrent_hash}: 等待 {self._interval} 秒后进行下一轮汇报...")
                    time.sleep(self._interval)
                    
            logger.info(f"{self.LOG_TAG} 种子 {torrent_hash} 汇报流程完成。")
        except Exception as e:
             logger.error(f"{self.LOG_TAG} 种子 {torrent_hash} 汇报线程中出现异常: {e}")

    @eventmanager.register(EventType.DownloadAdded)
    def download_added(self, event: Event):
        """
        监听“下载已添加”事件
        """
        if not self.get_state():
            return
        
        try:
            downloader_name = event.event_data.get("downloader")
            _hash = event.event_data.get("hash")
            
            if not downloader_name or not _hash:
                logger.debug(f"{self.LOG_TAG} 事件缺少 downloader 或 hash，跳过。")
                return
            
            # 检查是否是我们关心的下载器
            if not self.service_infos or downloader_name not in self.service_infos:
                return
                
            service = self.service_infos.get(downloader_name)
            
            # 此插件逻辑仅支持 qBittorrent
            if not service or service.type != "qbittorrent": 
                logger.debug(f"{self.LOG_TAG} 跳过 {downloader_name}，因为它不是qBittorrent或未连接。")
                return
                
            downloader_obj = service.instance
            
            # 事件触发时，种子可能尚未完全在qB中注册
            # 增加短暂延迟以确保能获取到种子信息（包括标签）
            time.sleep(5) 
            
            torrents, error = downloader_obj.get_torrents(ids=_hash)
            if error or not torrents:
                logger.error(f"{self.LOG_TAG} 无法从 {downloader_name} 获取种子信息: {_hash}")
                return
                
            torrent = torrents[0]
            torrent_tags = self._get_label(torrent=torrent, dl_type=service.type)
            
            # 检查标签是否包含“辅种”
            if self._skip_tag and self._skip_tag in torrent_tags:
                logger.info(f"{self.LOG_TAG} 种子 {_hash} 标签 {torrent_tags} 包含 “{self._skip_tag}”，跳过汇报处理。")
                return
            
            # 启动后台线程执行汇报循环，防止阻塞
            logger.info(f"{self.LOG_TAG} 种子 {_hash} (标签: {torrent_tags}) 已添加，准备启动汇报流程...")
            threading.Thread(
                target=self._run_announce_loop, 
                args=(service, _hash),
                daemon=True # 设置为守护线程，以便MP退出时它也能退出
            ).start()

        except Exception as e:
            logger.error(f"{self.LOG_TAG} 处理下载事件时发生严重错误: {e}")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                            'hint': '启用后，将监控新添加的qBittorrent下载任务。',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'downloaders',
                                            'label': '选择要监控的下载器 (仅qBittorrent生效)',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.downloader_helper.get_configs().values()
                                                      if config.type == "qbittorrent"]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'skip_tag',
                                            'label': '跳过标签',
                                            'placeholder': '辅种'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'first_announce_delay',
                                            'label': '首次汇报延迟 (秒)',
                                            'placeholder': '180',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '每次汇报间隔 (秒)',
                                            'placeholder': '330',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'announce_times',
                                            'label': '总汇报次数',
                                            'placeholder': '15',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '此插件通过监听“下载添加”事件触发。当新种子（仅限qB）被添加时，它会检查其标签，若不包含“跳过标签”（如“辅种”），则会启动一个后台任务，按设定的延迟和次数循环执行“强制汇报”。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            # 默认值
            "enabled": False,
            "downloaders": [],
            "skip_tag": "辅种",
            "first_announce_delay": "180",
            "interval": "330",
            "announce_times": "15"
        }

    def get_page(self) -> List[dict]:
        pass
