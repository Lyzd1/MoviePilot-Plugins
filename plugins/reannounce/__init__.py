import time
import sys
import logging
from typing import List, Dict, Any, Optional

import requests

from app.core.event import eventmanager, Event
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.schemas import ServiceInfo

# 配置
# 注意：BASE_URL 将不再需要，因为我们将使用 DownloaderHelper 实例的方法
# DEFAULT_ANNOUNCE_TIMES, DEFAULT_INTERVAL, FIRST_ANNOUNCE_DELAY 将作为插件配置项

# 设置日志前缀
LOG_TAG = "[ReannouncePlugin] "

class ReannouncePlugin(_PluginBase):
    # 插件元数据
    plugin_name = "qbit强制汇报(Reannounce)"
    plugin_desc = "监听下载事件，定时汇报最新种子"
    plugin_icon = "Reannounce.png"  # 假设有一个图标
    plugin_version = "1.0"
    plugin_author = "Lyzd1" # 假设
    author_url = "https://github.com/Lyzd1" # 假设
    plugin_config_prefix = "ReannouncePlugin_"
    plugin_order = 10  # 靠后执行
    auth_level = 1

    # 私有属性 - 对应原脚本中的配置和辅助类
    downloader_helper: Optional[DownloaderHelper] = None
    _enabled: bool = False
    _announce_times: int = 15
    _interval: int = 330
    _first_delay: int = 180
    _skip_tag: str = "辅种"
    _downloaders: Optional[List[str]] = None

    def init_plugin(self, config: dict = None):
        """
        插件初始化
        """
        self.downloader_helper = DownloaderHelper()
        
        # 读取配置
        if config:
            self._enabled = config.get("enabled", False)
            # 使用 str_to_number 转换配置值，确保是整数
            self._announce_times = self.str_to_number(config.get("announce_times"), 15)
            self._interval = self.str_to_number(config.get("interval"), 330)
            self._first_delay = self.str_to_number(config.get("first_delay"), 180)
            self._skip_tag = config.get("skip_tag") or "辅种"
            self._downloaders = config.get("downloaders")

        # 打印初始化信息
        logger.info(f"{LOG_TAG}插件初始化完成. "
                    f"启用: {self._enabled}, 汇报次数: {self._announce_times}, "
                    f"间隔: {self._interval}s, 首次延迟: {self._first_delay}s")

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """
        获取已配置且连接成功的下载器服务信息
        """
        if not self._downloaders:
            logger.warning(f"{LOG_TAG}尚未配置下载器，请检查配置")
            return None

        # 获取所有配置的下载器服务信息
        services = self.downloader_helper.get_services(name_filters=self._downloaders)
        if not services:
            logger.warning(f"{LOG_TAG}获取下载器实例失败，请检查配置")
            return None

        # 过滤出活动的下载器
        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"{LOG_TAG}下载器 {service_name} 未连接，跳过")
            else:
                active_services[service_name] = service_info

        if not active_services:
            logger.warning(f"{LOG_TAG}没有已连接的下载器，请检查配置")
            return None

        return active_services

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def str_to_number(s: Any, default_i: int) -> int:
        """安全地将字符串转换为整数，失败则返回默认值"""
        try:
            return int(s)
        except (ValueError, TypeError):
            return default_i

    # --- 核心功能实现 ---

    def _force_reannounce_in_thread(self, downloader_obj: Any, torrent_hash: str, tags: str):
        """
        在单独的线程中执行多次强制汇报，以防止阻塞主线程。
        """
        # 检查标签是否包含跳过关键字，如果包含则跳过
        if self._skip_tag in tags:
            logger.info(f"{LOG_TAG}种子 {torrent_hash} 标签包含“{self._skip_tag}”，跳过强制汇报流程。")
            return
            
        logger.info(f"{LOG_TAG}种子 {torrent_hash} 汇报流程启动...")
        logger.info(f"{LOG_TAG}等待 {self._first_delay} 秒后进行第一次汇报...")
        
        # 第一次汇报前延迟
        time.sleep(self._first_delay)

        # 对种子进行多轮汇报
        for current_time in range(self._announce_times):
            logger.info(f"{LOG_TAG}=== 种子 {torrent_hash} 第 {current_time + 1} 轮汇报 (共 {self._announce_times} 轮) ===")
            
            # 使用下载器实例的 reannounce_torrent 方法
            success, msg = self._reannounce(downloader_obj, torrent_hash)
            
            if success:
                logger.info(f"{LOG_TAG}种子 {torrent_hash} 强制汇报成功！")
            else:
                logger.error(f"{LOG_TAG}种子 {torrent_hash} 强制汇报失败：{msg}")
            
            # 如果不是最后一轮,则等待间隔时间
            if current_time < self._announce_times - 1:
                logger.info(f"{LOG_TAG}等待 {self._interval} 秒后进行下一轮汇报...")
                time.sleep(self._interval)
            
        logger.info(f"{LOG_TAG}种子 {torrent_hash} 汇报流程完成。")
        
    def _reannounce(self, downloader_obj: Any, torrent_hash: str) -> (bool, str):
        """
        执行单个种子的强制汇报操作。
        """
        try:
            # MoviePilot 下载器实例通常提供 reannounce_torrent 方法
            # qBittorrent 客户端：downloader_obj.qbc.torrents_reannounce(hashes=torrent_hash)
            # Transmission 客户端：downloader_obj.tc.reannounce_torrent(ids=torrent_hash)
            # 这里统一调用 MoviePilot 提供的封装方法 (如果有的话)
            # 由于没有完整的 MoviePilot DownloaderBase 代码，我们模拟调用：
            # 注意：在实际 MP 插件中，需要根据 `service.type` 调用不同的 API 或统一封装。
            
            # 由于原脚本是直接 POST 到 API，我们模拟一个 API 调用或使用 MP 封装：
            
            # 假设 MP 的 downloader 实例有统一的 reannounce 方法
            # if hasattr(downloader_obj, "reannounce_torrent"):
            #     # 某些下载器可能要求ids是列表
            #     downloader_obj.reannounce_torrent(ids=torrent_hash) 
            #     return True, "OK"
            
            # 模拟原脚本的 HTTP API 调用 (仅适用于原脚本的目标 qBittorrent API)
            # **警告：直接调用 API 不符合 MP 插件最佳实践，应使用 downloader_obj 的方法**
            base_url = "http://192.168.10.6:8080/api/v2" # 注意：这需要是配置中的下载器地址
            reannounce_url = f"{base_url}/torrents/reannounce"
            payload = {"hashes": torrent_hash}
            
            # 实际 MP 插件不应该硬编码或绕过认证，我们保留这个结构以作演示
            # **在实际部署中，需要确保使用 downloader_obj 实例**
            # **假设我们使用的是一个 qBittorrent API 的 Downloader 实例，它的 reannounce 方法可能如下：**
            if downloader_obj.type == "qbittorrent":
                 downloader_obj.qbc.torrents_reannounce(hashes=torrent_hash)
                 return True, "Success via qBittorrent client API"
            elif downloader_obj.type == "transmission":
                 downloader_obj.tc.reannounce_torrent(ids=torrent_hash)
                 return True, "Success via Transmission client API"
            else:
                 # 对于其他下载器，如果没有统一接口，则无法执行
                 return False, f"下载器类型 {downloader_obj.type} 暂不支持强制汇报"

        except Exception as e:
            return False, str(e)

    # --- 事件监听 ---

    @eventmanager.register(EventType.DownloadAdded)
    def download_added(self, event: Event):
        """
        监听下载任务添加事件，启动强制汇报流程
        """
        if not self.get_state():
            return

        if not event.event_data:
            return

        try:
            downloader_name = event.event_data.get("downloader")
            _hash = event.event_data.get("hash")
            context = event.event_data.get("context")
            
            if not downloader_name or not _hash or not context:
                logger.info(f"{LOG_TAG}触发下载事件，但缺少必要的下载器或哈希信息，跳过。")
                return

            service = self.service_infos.get(downloader_name)
            if not service:
                logger.info(f"{LOG_TAG}触发下载事件，但未监听下载器 {downloader_name}，跳过。")
                return
            
            # 从 Context 中获取标签信息（如果有的话，取决于事件触发时的完整性）
            # 默认情况下，事件触发时可能没有完整的标签，但我们可以尝试从 torrent_info 获取 site_name 作为标签判断依据
            torrent_info = context.torrent_info
            
            # 在添加事件时，标签信息可能不是一个干净的字符串，我们尝试使用 site_name/label
            # 这里简单使用 site_name 作为标签判断
            tags = torrent_info.site_name or "" 
            # 如果是 qBittorrent，也可以尝试获取 context 中原始的 label 或 tags 字段
            
            logger.info(f"{LOG_TAG}捕获到下载任务添加事件: 下载器={downloader_name}, Hash={_hash}, 标签={tags}")

            # 在单独的线程中运行长时间的汇报过程，避免阻塞事件管理器
            import threading
            threading.Thread(
                target=self._force_reannounce_in_thread,
                args=(service.instance, _hash, tags),
                daemon=True
            ).start()
            
        except Exception as e:
            logger.error(f"{LOG_TAG}处理下载事件时发生了错误: {str(e)}")

    # --- 配置表单 ---
    
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
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
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
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'downloaders',
                                            'label': '监听下载器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.downloader_helper.get_configs().values()]
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
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'announce_times',
                                            'label': '总汇报次数 (次)',
                                            'placeholder': '15'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '汇报间隔时间 (秒)',
                                            'placeholder': '330'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'first_delay',
                                            'label': '第一次汇报延迟 (秒)',
                                            'placeholder': '180'
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'skip_tag',
                                            'label': '跳过汇报的标签 (包含此标签则跳过, 默认: 辅种)',
                                            'placeholder': '辅种'
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '注意：此插件通过监听“下载任务添加”事件触发，并在新的线程中执行多次强制汇报。请确保您的下载器已配置正确并连接成功。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "downloaders": [],
            "announce_times": "15",
            "interval": "330",
            "first_delay": "180",
            "skip_tag": "辅种"
        }

    # 其他方法 (get_command, get_api, get_service, get_page, stop_service) 保持为空或不实现，因为此插件仅依赖事件
    def get_command(self) -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass
    
    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        此插件没有长时间运行的 BackgroundScheduler，所以 stop_service 保持简单
        """
        logger.info(f"{LOG_TAG}服务停止。")
        pass
