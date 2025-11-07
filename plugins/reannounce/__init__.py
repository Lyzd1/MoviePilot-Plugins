import time
import sys
import logging
import threading # 修正：将 threading 移到顶部
from typing import List, Dict, Any, Optional, Tuple # 修正：添加 Tuple

import requests

from app.core.event import eventmanager, Event
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.schemas import ServiceInfo

# 配置
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

    # 私有属性
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
            self._announce_times = self.str_to_number(config.get("announce_times"), 15)
            self._interval = self.str_to_number(config.get("interval"), 330)
            self._first_delay = self.str_to_number(config.get("first_delay"), 180)
            self._skip_tag = config.get("skip_tag") or "辅种"
            self._downloaders = config.get("downloaders")

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

        services = self.downloader_helper.get_services(name_filters=self._downloaders)
        if not services:
            logger.warning(f"{LOG_TAG}获取下载器实例失败，请检查配置")
            return None

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
        
    def _reannounce(self, downloader_obj: Any, torrent_hash: str) -> Tuple[bool, str]: # 修正返回值类型提示
        """
        执行单个种子的强制汇报操作。
        """
        try:
            # 根据下载器类型调用对应的客户端 API 接口
            if downloader_obj.type == "qbittorrent":
                 # qBittorrent 客户端 API 调用
                 downloader_obj.qbc.torrents_reannounce(hashes=torrent_hash)
                 return True, "Success via qBittorrent client API"
            elif downloader_obj.type == "transmission":
                 # Transmission 客户端 API 调用
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
            
            torrent_info = context.torrent_info
            
            # 使用 site_name 作为标签判断依据
            tags = torrent_info.site_name or "" 
            
            logger.info(f"{LOG_TAG}捕获到下载任务添加事件: 下载器={downloader_name}, Hash={_hash}, 标签={tags}")

            # 在单独的线程中运行长时间的汇报过程，避免阻塞事件管理器
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

    # 其他方法
    def get_command(self) -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass
    
    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        logger.info(f"{LOG_TAG}服务停止。")
        pass
