import os
import platform
import threading
import time
import traceback
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from urllib.parse import quote
from datetime import datetime, timedelta
from threading import Lock

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from apscheduler.schedulers.background import BackgroundScheduler

# === 引用 StorageHelper 用于自动获取配置 ===
from app.helper.storage import StorageHelper
# ========================================

# --- 视频文件扩展名 ---
VIDEO_EXTENSIONS = [
    ".mkv",
    ".mp4",
    ".ts",
    ".avi",
    ".rmvb",
    ".wmv",
    ".mov",
    ".flv",
    ".mpg",
    ".mpeg",
    ".iso", # 蓝光原盘
    ".bdmv", # 蓝光原盘
    ".m2ts", # 蓝光原盘
]

# --- 临时文件后缀 ---
TEMP_EXTENSIONS = [".!qB", ".part", ".mp", ".tmp", ".temp", ".download"]

# Global lock for task list access
task_lock = Lock()

# Task status definitions (simplified, aligned with AList state: 0-等待中, 1-进行中, 2-成功, 3-失败)
TASK_STATUS_WAITING = 0
TASK_STATUS_RUNNING = 1
TASK_STATUS_SUCCESS = 2
TASK_STATUS_FAILED = 3

class NewFileMonitorHandler(FileSystemEventHandler):
    """
    目录监控处理 - 仅处理文件创建和移动（移入）
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(NewFileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync  # sync 是 OpenlistMover 插件实例

    def _is_target_file(self, file_path: Path) -> bool:
        """检查文件是否是目标视频文件，且不是临时文件"""
        file_suffix = file_path.suffix.lower()
        
        # 1. 检查是否为临时文件
        if file_suffix in TEMP_EXTENSIONS:
            return False
        
        # 2. 检查是否为视频文件
        if file_suffix in VIDEO_EXTENSIONS:
            return True
            
        return False

    def _process_event(self, file_path: Path):
        """处理文件事件"""
        if self._is_target_file(file_path):
            logger.debug(f"监测到新视频文件：{file_path}")
            # 使用线程处理，避免阻塞监控
            # 重复检查的逻辑移至 process_new_file 中，因为它在线程内
            threading.Thread(
                target=self.sync.process_new_file, args=(file_path,)
            ).start()
        else:
            logger.debug(f"忽略文件：{file_path} (非目标视频文件或临时文件)")

    def on_created(self, event):
        if event.is_directory:
            return
        file_path = Path(event.src_path)
        self._process_event(file_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        # 'on_moved' 捕获文件移入目录的事件
        file_path = Path(event.dest_path)
        self._process_event(file_path)


class OpenlistMover(_PluginBase):
    # 插件名称
    plugin_name = "Openlist 视频文件同步"
    # 插件描述
    plugin_desc = "监控本地目录，当有新视频文件生成时，自动通过 Openlist API 将其移动到指定的云盘目录。支持移动任务监控和 strm 文件同步。"
    # 插件图标
    plugin_icon = "Ombi_A.png"
    # 插件版本
    plugin_version = "4.3.2" 
    # 插件作者
    plugin_author = "Lyzd1"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "openlistmover_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 1

    # private property
    _enabled = False
    _notify = False
    _openlist_url = ""
    _openlist_token = ""
    _monitor_paths = ""
    _path_mappings = ""
    _strm_path_mappings = "" # 新增 strm 映射配置
    _observer = []
    _scheduler: Optional[BackgroundScheduler] = None
    
    # === 新增洗版配置 ===
    _wash_mode_enabled = False
    _wash_delay_seconds = 60
    # ======================
    
    # {local_prefix: (openlist_src_prefix, openlist_dst_prefix)}
    _parsed_mappings: Dict[str, Tuple[str, str]] = {}
    
    # {dst_prefix: (strm_src_prefix, strm_dst_prefix)}
    _parsed_strm_mappings: Dict[str, Tuple[str, str]] = {} # 新增 strm 映射解析结果
    
    # === 新增：用于防止重复处理 ===
    _processing_files: set = set()
    _processing_lock = Lock()
    # ==========================
    
    # Task tracking list
    # Format: [{"id": str, "file": str, "src_dir": str, "dst_dir": str, "start_time": datetime, "status": int, "error": str, "strm_status": str, "is_wash": bool}]
    _move_tasks: List[Dict[str, Any]] = []
    _max_task_duration = 60 * 60 # 60 minutes in seconds (最长 60min)
    _task_check_interval = 60 # 1 minute in seconds (每隔 1min)

    # === 新增属性用于任务计数和清空配置 ===
    _successful_moves_count = 0  # 累计成功移动次数
    _clear_api_threshold = 10    # 自动清空 Openlist API 任务记录的阈值 (已弃用，保留以兼容旧配置)
    _clear_panel_threshold = 30  # 自动清空成功任务面板记录的阈值 (默认 30 次成功)
    _keep_successful_tasks = 3   # 清空面板时保留的最新成功任务数量 (默认 3 个)
    # ======================================

    # === 新增全局扫描配置 ===
    _global_scan_enabled = False
    _global_scan_time = "02:00"
    _global_scan_scheduler: Optional[BackgroundScheduler] = None
    # ==========================

    @staticmethod
    def __choose_observer():
        """
        选择最优的监控模式
        """
        system = platform.system()
        try:
            if system == "Linux":
                from watchdog.observers.inotify import InotifyObserver
                return InotifyObserver()
            elif system == "Darwin":
                from watchdog.observers.fsevents import FSEventsObserver
                return FSEventsObserver()
            elif system == "Windows":
                from watchdog.observers.read_directory_changes import WindowsApiObserver
                return WindowsApiObserver()
        except Exception as error:
            logger.warn(f"导入模块错误：{error}，将使用 PollingObserver 监控目录")
        return PollingObserver()

    def init_plugin(self, config: dict = None):
        logger.info("初始化 Openlist 视频文件移动插件")

        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", False)
            self._openlist_url = config.get("openlist_url", "").rstrip('/')
            self._openlist_token = config.get("openlist_token", "")
            self._monitor_paths = config.get("monitor_paths", "")
            self._path_mappings = config.get("path_mappings", "")
            self._strm_path_mappings = config.get("strm_path_mappings", "") # 加载 strm 映射

            # === 加载洗版配置 ===
            self._wash_mode_enabled = config.get("wash_mode_enabled", False)
            try:
                self._wash_delay_seconds = int(config.get("wash_delay_seconds", 60))
            except ValueError:
                self._wash_delay_seconds = 60
            # =======================

            # === 加载新的配置项 ===
            # _clear_api_threshold 已弃用，但仍保留以兼容旧配置
            try:
                self._clear_api_threshold = int(config.get("clear_api_threshold", 10))
            except ValueError:
                self._clear_api_threshold = 10

            try:
                self._clear_panel_threshold = int(config.get("clear_panel_threshold", 30))
            except ValueError:
                self._clear_panel_threshold = 30

            try:
                self._keep_successful_tasks = int(config.get("keep_successful_tasks", 3))
            except ValueError:
                self._keep_successful_tasks = 3

            # === 加载视频后缀配置 ===
            video_extensions_config = config.get("video_extensions", "")
            if video_extensions_config:
                # 解析用户配置的视频后缀
                custom_extensions = [
                    ext.strip().lower()
                    for ext in video_extensions_config.split("\n")
                    if ext.strip() and ext.strip().startswith('.')
                ]
                if custom_extensions:
                    global VIDEO_EXTENSIONS
                    VIDEO_EXTENSIONS = custom_extensions
                    logger.info(f"已加载 {len(VIDEO_EXTENSIONS)} 个自定义视频后缀: {VIDEO_EXTENSIONS}")
            # =======================

            # === 加载全局扫描配置 ===
            self._global_scan_enabled = config.get("global_scan_enabled", False)
            self._global_scan_time = config.get("global_scan_time", "02:00")
            # =======================

        # === 加载持久化状态 ===
        # 加载任务列表
        saved_tasks = self.get_data('move_tasks') or []
        self._move_tasks = []
        for task in saved_tasks:
            try:
                # 反序列化 datetime 对象
                if 'start_time' in task and isinstance(task['start_time'], str):
                    task['start_time'] = datetime.fromisoformat(task['start_time'])
                self._move_tasks.append(task)
            except Exception as e:
                logger.warning(f"加载任务时出错，跳过该任务: {task.get('id', 'unknown')} - {e}")

        # 加载状态计数器
        state_data = self.get_data('plugin_state') or {}
        self._successful_moves_count = state_data.get('successful_moves_count', 0)

        logger.info(f"已加载 {len(self._move_tasks)} 个持久化任务，成功计数: {self._successful_moves_count}")
        # =====================

        # 停止现有任务
        self.stop_service()

        if self._enabled:
            # =========================================================
            # 自动配置逻辑：如果 URL 或 Token 未配置，尝试从系统存储中获取
            # =========================================================
            if not self._openlist_url or not self._openlist_token:
                try:
                    logger.debug("OpenlistMover: 插件配置中 URL 或 Token 为空，尝试从系统存储配置中自动读取...")
                    storage_configs = StorageHelper.get_storagies()
                    for s in storage_configs:
                        if s.type in ['alist', 'openlist']:
                            s_url = s.config.get('host') or s.config.get('url')
                            s_token = s.config.get('token') or s.config.get('password')
                            
                            if s_url and s_token:
                                logger.info(f"OpenlistMover: 自动检测到存储配置 [{s.name}]，将应用到插件配置。")
                                if not self._openlist_url:
                                    self._openlist_url = s_url.rstrip('/')
                                if not self._openlist_token:
                                    self._openlist_token = s_token
                                break # 找到第一个符合的即可
                except Exception as e:
                    logger.error(f"OpenlistMover: 自动读取系统存储配置失败: {e}")
            # =========================================================

            if not self._openlist_url or not self._openlist_token:
                logger.error("Openlist Mover 已启用，但 Openlist URL 或 Token 未配置（且未能自动获取）！")
                self.systemmessage.put(
                    "Openlist Mover 启动失败：Openlist URL 或 Token 未配置",
                    title="Openlist 视频文件移动",
                )
                return

            if not self._monitor_paths or not self._path_mappings:
                logger.error("Openlist Mover 已启用，但监控目录或路径映射未配置！")
                self.systemmessage.put(
                    "Openlist Mover 启动失败：监控目录或路径映射未配置",
                    title="Openlist 视频文件移动",
                )
                return
                
            # 解析本地移动映射
            self._parsed_mappings = self._parse_path_mappings()
            if not self._parsed_mappings:
                logger.error("Openlist Mover 路径映射配置无效")
                return
                
            # 解析 STRM 复制映射
            self._parsed_strm_mappings = self._parse_strm_path_mappings()
            
            logger.info(f"Openlist Mover 已加载 {len(self._parsed_mappings)} 条移动路径映射")
            logger.info(f"Openlist Mover 已加载 {len(self._parsed_strm_mappings)} 条 STRM 路径映射")
            logger.info(f"Openlist Mover 洗版模式: {'已启用' if self._wash_mode_enabled else '已禁用'}, 洗版延迟: {self._wash_delay_seconds} 秒")


            # 读取监控目录配置
            monitor_dirs = [
                d.strip() for d in self._monitor_paths.split("\n") if d.strip()
            ]
            logger.info(f"Openlist Mover 本地监控目录：{monitor_dirs}")

            # 启动监控
            for mon_path in monitor_dirs:
                if not os.path.exists(mon_path):
                    logger.warning(f"Openlist Mover 监控目录不存在：{mon_path}")
                    continue
                    
                if not mon_path:
                    continue
                try:
                    observer = self.__choose_observer()
                    self._observer.append(observer)
                    observer.schedule(
                        NewFileMonitorHandler(mon_path, self),
                        mon_path,
                        recursive=True,
                    )
                    observer.daemon = True
                    observer.start()
                    logger.info(f"Openlist Mover {mon_path} 的监控服务启动")
                except Exception as e:
                    err_msg = str(e)
                    logger.error(f"{mon_path} 启动监控失败：{err_msg}")
                    self.systemmessage.put(
                        f"{mon_path} 启动监控失败：{err_msg}",
                        title="Openlist 视频文件移动",
                    )
            
            # 移除初始化时的自动启动，改为按需启动
            # self._start_task_monitor()

            # 启动全局扫描定时器
            self._start_global_scan_scheduler()

            # === 任务恢复逻辑 ===
            active_tasks = [t for t in self._move_tasks if t['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]]
            if active_tasks:
                logger.info(f"发现 {len(active_tasks)} 个未完成的任务，将自动启动任务监控服务。")
                self._start_task_monitor()
            # ====================

            logger.info("Openlist 视频文件移动插件已启动 (待机模式)")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "title": "Openlist 视频文件移动",
                            "text": "本插件监控本地目录。当有新视频文件生成时，它会自动通过 Openlist API 将其移动到指定的云盘目录。这要求 Openlist 已经挂载了该本地目录作为存储。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                        ],
                    },
                    # Openlist API 配置
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "title": "Openlist API 配置",
                                            "text": "如果不填写，插件将尝试自动从系统存储配置 (Storage) 中读取类型为 'alist' 或 'openlist' 的配置。",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openlist_url",
                                            "label": "Openlist URL (留空自动获取)",
                                            "placeholder": "例如: http://127.0.0.1:5244",
                                        },
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openlist_token",
                                            "label": "Openlist Token (留空自动获取)",
                                            "type": "password",
                                            "placeholder": "Openlist 管理员 Token",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    # 监控和映射配置
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "monitor_paths",
                                            "label": "本地监控目录",
                                            "rows": 4,
                                            "placeholder": "填写 MoviePilot 可以访问到的绝对路径，每行一个\n例如：/downloads/watch",
                                        },
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "文件移动路径映射 (本地:Openlist源:Openlist目标)",
                                            "rows": 6,
                                            "placeholder": "格式：本地监控目录:Openlist源目录:Openlist目标目录\n每行一条规则\n\n例如：\n/downloads/watch:/Local/watch:/YP/Video\n\n说明：\n当本地监控到 /downloads/watch/电影/S01/E01.mkv\nOpenlist 将会执行移动：\n源：/Local/watch/电影/S01/E01.mkv\n目标：/YP/Video/电影/S01/E01.mkv",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    # STRM 复制配置 (新增)
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "strm_path_mappings",
                                            "label": "STRM 复制路径映射 (Openlist目标:Strm源:Strm本地目标)",
                                            "rows": 4,
                                            "placeholder": "格式：Openlist目标目录前缀:Strm驱动源目录前缀:Strm本地目标目录前缀\n每行一条规则\n\n例如：\n/YP/Video:/strm139:/strm\n\n说明：\n当文件成功移动到 /YP/Video/... 后，\n1. 插件将 list /strm139/... 触发 .strm 文件生成。\n2. 插件将 .strm 文件从 /strm139/... 复制到 /strm/...",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    # === 新增洗版配置 ===
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "洗版模式配置",
                                            "text": "当开启后，如果移动时发现目标文件已存在 (403 exists)，将自动使用覆盖模式 (overwrite: true) 重新移动。洗版成功后，会先删除旧的 STRM 文件，等待指定延迟后再重新生成。",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "wash_mode_enabled", "label": "启用洗版模式"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "wash_delay_seconds",
                                            "label": "洗版延迟 (秒)",
                                            "type": "number",
                                            "min": 0,
                                            "placeholder": "默认 60 (删除旧STRM后等待60秒再生效)",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    # =================================
                    # === 新增任务清空配置 ===
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "任务记录自动清空配置",
                                            "text": "成功完成的移动任务达到设定次数后，将自动清空插件面板记录和 Openlist 任务队列记录。清空后，计数器将重置。",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "clear_panel_threshold",
                                            "label": "清空面板成功记录阈值 (次)",
                                            "type": "number",
                                            "min": 1,
                                            "placeholder": "默认 30 (成功 30 次清空面板成功记录和API任务记录)",
                                        },
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "keep_successful_tasks",
                                            "label": "清空面板时保留数量",
                                            "type": "number",
                                            "min": 0,
                                            "placeholder": "默认 3 (清空时保留最新的 3 条成功记录)",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    # =================================
                    # === 新增视频文件后缀配置 ===
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "视频文件后缀配置",
                                            "text": "定义哪些文件扩展名被视为视频文件。用于文件监控和洗版模式的文件识别。",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "video_extensions",
                                            "label": "视频文件后缀",
                                            "rows": 3,
                                            "placeholder": "每行一个后缀，例如：\n.mkv\n.mp4\n.ts\n.avi\n.rmvb\n.wmv\n.mov\n.flv\n.mpg\n.mpeg\n.iso\n.bdmv\n.m2ts",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    # =================================
                    # === 新增全局扫描配置 ===
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "全局扫描配置",
                                            "text": "每天定时扫描本地监控目录，检查是否有未成功上传的文件并重新上传，防止网络波动导致的错误。",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "global_scan_enabled", "label": "启用全局扫描"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "global_scan_time",
                                            "label": "扫描时间 (HH:MM)",
                                            "placeholder": "例如: 02:00 (凌晨2点)",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                    # =================================
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "title": "工作流程说明",
                            "text": "1. 插件监控 '本地监控目录'。\n2. 成功移动到 'Openlist目标目录' 后，插件将根据 STRM 映射进行后续操作。\n3. STRM 映射旨在将云盘目标路径 (e.g., /YP/Video) 转换为 Strm 驱动路径 (e.g., /strm139) 用于 list/copy，并将 Strm 驱动路径复制到本地 Strm 目录 (e.g., /strm)。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "openlist_url": "",
            "openlist_token": "",
            "monitor_paths": "",
            "path_mappings": "",
            "strm_path_mappings": "", # 新增默认值
            # === 新增配置默认值 ===
            "wash_mode_enabled": False,
            "wash_delay_seconds": 60,
            "clear_panel_threshold": 30,
            "keep_successful_tasks": 3,
            "video_extensions": "",
            "global_scan_enabled": False,
            "global_scan_time": "02:00"
            # ======================
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，显示任务列表 (UI设计)
        """
        
        with task_lock:
            # 活跃任务（等待中或进行中）
            active_tasks = [t for t in self._move_tasks if t['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]]
            # 成功或失败任务 (仅用于显示，不含清空逻辑)
            finished_tasks_all = sorted(
                [t for t in self._move_tasks if t['status'] in [TASK_STATUS_SUCCESS, TASK_STATUS_FAILED]],
                key=lambda x: x['start_time'], reverse=True
            )
            # 最近完成任务（最多显示 50 条）
            finished_tasks = finished_tasks_all[:50]
            current_success_count = self._successful_moves_count # 用于显示当前计数

        def get_status_text(status: int) -> str:
            if status == TASK_STATUS_WAITING:
                return '等待中'
            elif status == TASK_STATUS_RUNNING:
                return '进行中'
            elif status == TASK_STATUS_SUCCESS:
                return '成功'
            elif status == TASK_STATUS_FAILED:
                return '失败'
            return '未知'

        def get_status_color(status: int) -> str:
            if status == TASK_STATUS_WAITING:
                return 'text-info'
            elif status == TASK_STATUS_RUNNING:
                return 'text-primary'
            elif status == TASK_STATUS_SUCCESS:
                return 'text-success'
            elif status == TASK_STATUS_FAILED:
                return 'text-error'
            return ''

        def task_to_tr(task: Dict[str, Any]) -> dict:
            strm_status = task.get('strm_status', '未执行')
            strm_color = 'text-warning' if strm_status.startswith('失败') else ('text-success' if strm_status == '成功' else 'text-muted')
            
            # 检查是否为洗版任务
            is_wash_task = task.get('is_wash', False)
            file_display = f"{task.get('file', 'N/A')} {'(洗版)' if is_wash_task else ''}"
            
            return {
                'component': 'tr',
                'props': {'class': 'text-sm'},
                'content': [
                    # 移除 任务ID 的显示
                    {'component': 'td', 'text': file_display}, # 显示是否为洗版
                    {'component': 'td', 'text': task.get('dst_dir', 'N/A')},
                    {'component': 'td', 'text': task['start_time'].strftime('%Y-%m-%d %H:%M:%S') if 'start_time' in task else 'N/A'},
                    {
                        'component': 'td', 
                        'props': {'class': get_status_color(task['status'])},
                        'text': get_status_text(task['status'])
                    },
                    {
                        'component': 'td', 
                        'props': {'class': strm_color},
                        'text': strm_status
                    },
                    {'component': 'td', 'text': task.get('error', '') if task['status'] == TASK_STATUS_FAILED else ''},
                ]
            }

        table_headers = [
            # 移除 任务ID 的表头
            {'text': '文件名', 'class': 'text-start ps-4'},
            {'text': '目标目录', 'class': 'text-start ps-4'},
            {'text': '开始时间', 'class': 'text-start ps-4'},
            {'text': '移动状态', 'class': 'text-start ps-4'},
            {'text': 'STRM状态', 'class': 'text-start ps-4'}, # 新增 STRM 状态列
            {'text': '错误信息', 'class': 'text-start ps-4'},
        ]

        page_content = []
        
        # 活跃任务区
        page_content.extend([
            {
                'component': 'VCardTitle',
                'text': '当前活跃任务'
            },
            {
                'component': 'VTable',
                'props': {'hover': True},
                'content': [
                    {'component': 'thead', 'content': [
                        {'component': 'th', **{'props': {'class': h['class']}, 'text': h['text']}} for h in table_headers
                    ]},
                    {'component': 'tbody', 'content': [task_to_tr(t) for t in active_tasks]}
                ]
            }
        ])
        
        # 最近完成任务区
        page_content.extend([
            {
                'component': 'VCardTitle',
                'text': f'最近完成任务 (累计成功: {current_success_count} 次)' # 显示当前计数
            },
            {
                'component': 'VTable',
                'props': {'hover': True},
                'content': [
                    {'component': 'thead', 'content': [
                        {'component': 'th', **{'props': {'class': h['class']}, 'text': h['text']}} for h in table_headers
                    ]},
                    {'component': 'tbody', 'content': [task_to_tr(t) for t in finished_tasks]}
                ]
            }
        ])
        
        return [
            {
                'component': 'VContainer',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': page_content
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        logger.debug("开始停止 Openlist Mover 服务")

        self._stop_task_monitor()
        self._stop_global_scan_scheduler()

        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    logger.error(f"停止目录监控失败：{str(e)}")
        self._observer = []
        logger.debug("Openlist Mover 服务停止完成")

    def _save_move_tasks(self):
        """
        保存任务列表到持久化存储
        """
        try:
            # 序列化 datetime 对象
            serializable_tasks = []
            for task in self._move_tasks:
                serializable_task = task.copy()
                if 'start_time' in serializable_task and isinstance(serializable_task['start_time'], datetime):
                    serializable_task['start_time'] = serializable_task['start_time'].isoformat()
                serializable_tasks.append(serializable_task)

            self.save_data('move_tasks', serializable_tasks)
            logger.debug(f"已保存 {len(serializable_tasks)} 个任务到持久化存储")
        except Exception as e:
            logger.error(f"保存任务列表时出错: {e}")

    def _save_plugin_state(self):
        """
        保存插件状态到持久化存储
        """
        try:
            state_data = {
                'successful_moves_count': self._successful_moves_count
            }
            self.save_data('plugin_state', state_data)
            logger.debug("已保存插件状态到持久化存储")
        except Exception as e:
            logger.error(f"保存插件状态时出错: {e}")

    def _start_task_monitor(self):
        """
        启动任务监控定时器 (按需启动)
        """
        # 如果调度器已存在且正在运行，则不需要重新启动
        if self._scheduler and self._scheduler.running:
            return

        try:
            timezone = 'Asia/Shanghai' # Fallback for snippet
            self._scheduler = BackgroundScheduler(timezone=timezone)
            self._scheduler.add_job(
                self._check_move_tasks, 
                "interval",
                seconds=self._task_check_interval, # 1 minute interval
                name="Openlist 移动任务监控"
            )
            self._scheduler.start()
            logger.debug("Openlist Mover 任务监控服务已启动 (有活跃任务)")
        except Exception as e:
            logger.error(f"启动 Openlist Mover 任务监控服务失败: {e}")

    def _stop_task_monitor(self):
        """
        停止任务监控定时器 (空闲时关闭)
        """
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
                logger.debug("Openlist Mover 任务监控服务已暂停 (无活跃任务)")
            except Exception as e:
                logger.error(f"停止任务监控失败：{str(e)}")
            
    def _send_task_notification(self, task: Dict[str, Any], title: str, text: str):
        """
        发送通知消息
        """
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=title,
                text=text,
            )

    def _check_move_tasks(self):
        """
        定期检查 Openlist 移动任务的状态，并处理清空逻辑
        """
        logger.debug("开始检查 Openlist 移动任务状态...")
        
        # 临时列表，用于存储在当前检查周期需要更新状态的任务
        tasks_to_update = []
        
        with task_lock:
            # 遍历所有任务，找出需要处理的活跃任务
            for task in self._move_tasks:
                if task['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]:
                    tasks_to_update.append(task)
        
        # 在锁外执行网络请求和耗时操作
        for task in tasks_to_update:
            # 检查超时 (需要在锁内更新状态，但我们现在只是检查时间)
            if (datetime.now() - task['start_time']).total_seconds() > self._max_task_duration:
                # 再次获取锁并更新状态
                with task_lock:
                    task['status'] = TASK_STATUS_FAILED
                    task['error'] = f"任务超时 ({int(self._max_task_duration / 60)} 分钟)"
                    logger.error(f"Openlist 移动任务 {task['id']} 超时")
                    self._save_move_tasks()  # 保存超时状态变更
                self._send_task_notification(task, "Openlist 移动超时", f"文件：{task['file']}\n源：{task['src_dir']}\n目标：{task['dst_dir']}\n错误：任务超时")
                continue

            # 查询状态 (网络请求，在锁外)
            try:
                task_info = self._call_openlist_task_api(task['id'])
                
                new_status = task_info.get('state') # state: 0-等待中, 1-进行中, 2-成功, 3-失败
                error_msg = task_info.get('error')
                
                # 在锁内更新状态
                with task_lock:
                    if new_status == TASK_STATUS_SUCCESS and task['status'] != TASK_STATUS_SUCCESS:
                        task['status'] = new_status
                        task['strm_status'] = '开始处理' # 标记开始 STRM 流程
                        self._save_move_tasks()  # 保存任务状态变更

                        # 增加成功计数
                        self._successful_moves_count += 1
                        self._save_plugin_state()  # 保存状态计数器

                        # 任务成功后，启动一个新的线程来处理 STRM
                        threading.Thread(
                            target=self._process_strm_creation,
                            args=(task,)
                        ).start()
                        
                    elif new_status == TASK_STATUS_FAILED and task['status'] != TASK_STATUS_FAILED:
                        task['status'] = new_status
                        task['error'] = error_msg if error_msg else "Openlist 报告失败"
                        self._send_task_notification(task, "Openlist 移动失败", f"文件：{task['file']}\n源：{task['src_dir']}\n目标：{task['dst_dir']}\n错误：{task['error']}")
                        self._save_move_tasks()  # 保存任务状态变更
                    elif new_status == TASK_STATUS_RUNNING:
                        task['status'] = new_status
                        self._save_move_tasks()  # 保存任务状态变更
                        
            except Exception as e:
                logger.error(f"查询 Openlist 任务 {task['id']} 状态失败: {e}")
        
        
        # 任务清空逻辑 (在锁内执行)
        with task_lock:
            clear_panel_triggered = False

            # 1. 检查 API 任务清空阈值 (倍数触发) - 已移除此功能，改为在面板清空时同时清空API任务记录
            # (此部分已被移除)


            # 2. 检查 插件面板 清空阈值 (达到设定值触发)
            if self._successful_moves_count >= self._clear_panel_threshold and self._clear_panel_threshold > 0:
                logger.debug(f"成功移动任务达到 {self._successful_moves_count} 次，满足插件面板清空阈值 ({self._clear_panel_threshold})，准备清空插件面板成功记录，保留最新 {self._keep_successful_tasks} 条。")

                tasks_to_keep = []
                # 提取活跃任务和失败任务
                tasks_to_keep.extend([t for t in self._move_tasks if t['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]])
                tasks_to_keep.extend([t for t in self._move_tasks if t['status'] == TASK_STATUS_FAILED])

                # 提取所有成功任务并排序
                successful_tasks = sorted(
                    [t for t in self._move_tasks if t['status'] == TASK_STATUS_SUCCESS],
                    key=lambda x: x['start_time'], reverse=True
                )

                # 保留最新的成功任务
                tasks_to_keep.extend(successful_tasks[:self._keep_successful_tasks])

                self._move_tasks = tasks_to_keep
                self._save_move_tasks()  # 保存清理后的任务列表

                logger.info(f"插件面板成功记录清空完毕，保留 {self._keep_successful_tasks} 条最新成功记录。")
                clear_panel_triggered = True

            # 3. 仅在插件面板清空被触发时，重置计数器并清空Openlist API任务记录
            if clear_panel_triggered:
                 self._successful_moves_count = 0
                 self._save_plugin_state()  # 保存重置后的计数器
                 logger.info("成功计数器已重置。")

                 # 同时清空Openlist API任务记录
                 try:
                    self._call_openlist_clear_tasks_api("copy")
                    self._call_openlist_clear_tasks_api("move")
                    logger.info("Openlist API 任务记录清空完毕。")
                 except Exception as e:
                    logger.error(f"执行 Openlist API 任务清空时发生错误: {e}")

            # --- 已移除挂起的 API 清空逻辑 ---
            # 原来的挂起机制已被移除，改为在面板清空时直接清空API任务记录

            # 获取当前活跃任务用于其他用途
            active_tasks = [t for t in self._move_tasks if t['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]]
            
            logger.debug(f"Openlist Mover 任务检查完成，当前活跃任务数: {len(active_tasks)}")

            # === 自动休眠：如果没有活跃任务，则停止监控 ===
            if not active_tasks:
                self._stop_task_monitor()
            
    def _update_task_strm_status(self, task_id: str, new_status: str, is_final: bool = False):
        """
        安全地更新任务列表中的 STRM 状态和发送通知。
        """
        with task_lock:
            found_task = None
            for task in self._move_tasks:
                if task['id'] == task_id:
                    task['strm_status'] = new_status
                    found_task = task
                    break
            self._save_move_tasks()  # 保存 STRM 状态变更
        
        # 仅在 STRM 流程最终完成后发送通知
        if is_final and found_task:
            is_wash_text = "(洗版)" if found_task.get("is_wash", False) else ""
            move_success_text = (
                f"✅ 文件移动成功 {is_wash_text}\n"
                f"🎬 视频文件：{found_task['dst_dir']}/{found_task['file']}\n"
                f"🔗 STRM状态：{new_status}"
            )
            self._send_task_notification(
                found_task,
                f"Openlist 移动完成 {is_wash_text}",
                move_success_text
            )


    def _process_strm_creation(self, task: Dict[str, Any]):
        """
        处理 STRM 文件生成和复制 (包含洗版逻辑)
        注意：此方法在独立线程中运行，不需要获取 task_lock，但需要通过 _update_task_strm_status 来更新状态。
        """
        task_id = task['id']
        self._update_task_strm_status(task_id, '开始执行 STRM 流程')
        
        # 1. 查找 STRM 路径映射
        dst_dir = task['dst_dir']
        file_name_ext = task['file']
        
        file_name_path = Path(file_name_ext)
        strm_file_name = file_name_path.with_suffix('.strm').name
        # 举例: "致不灭的你 S03E01-mediainfo.json"
        json_file_name = file_name_path.with_suffix('').name + "-mediainfo.json"


        # 查找最匹配的（最长的）Openlist目标前缀
        best_match = ""
        for dst_prefix in self._parsed_strm_mappings.keys():
            normalized_dst = os.path.normpath(dst_prefix)
            normalized_task_dir = os.path.normpath(dst_dir)
            if normalized_task_dir.startswith(normalized_dst):
                if len(dst_prefix) > len(best_match):
                    best_match = dst_prefix
        
        if not best_match:
            self._update_task_strm_status(task_id, '跳过 (无映射规则)', is_final=True)
            logger.debug(f"任务 {task_id} 移动成功，但未找到匹配的 STRM 映射规则，跳过 STRM 复制。")
            return
            
        try:
            dst_prefix = best_match
            strm_src_prefix, strm_dst_prefix = self._parsed_strm_mappings[dst_prefix]
            
            # 计算相对路径
            relative_dir_str = os.path.relpath(dst_dir, dst_prefix)
            relative_dir = relative_dir_str.replace(os.path.sep, '/')
            
            # 构建 List 路径 (需要 List 目录，而不是文件)
            list_path = f"{strm_src_prefix.rstrip('/')}/{relative_dir}"
            
            # 构建 Copy 路径 (源和目标目录)
            copy_src_dir = list_path
            copy_dst_dir = f"{strm_dst_prefix.rstrip('/')}/{relative_dir}"
            
            logger.debug(f"任务 {task_id} 成功，开始 STRM 处理:")
            logger.debug(f"  List 路径: {list_path}")
            logger.debug(f"  Copy 源: {copy_src_dir}")
            logger.debug(f"  Copy 目标: {copy_dst_dir}")
            logger.debug(f"  文件名: {strm_file_name}, {json_file_name}")
            
            self._update_task_strm_status(task_id, '删除旧 STRM 文件')

            # === 洗版逻辑：删除旧文件 ===
            if task.get("is_wash", False):
                logger.debug(f"洗版模式：任务 {task_id} 正在删除旧 STRM 文件于 {copy_dst_dir}...")
                
                names_to_delete = [strm_file_name, json_file_name]
                
                delete_success = self._call_openlist_remove_api(copy_dst_dir, names_to_delete)
                
                if delete_success:
                    self._update_task_strm_status(task_id, f'删除成功，等待 {self._wash_delay_seconds} 秒')
                    logger.debug(f"旧 STRM 文件删除成功，等待 {self._wash_delay_seconds} 秒延迟...")
                    # 关键：time.sleep 在锁外，不会阻塞 get_page()
                    time.sleep(self._wash_delay_seconds) 
                else:
                    logger.warning(f"旧 STRM 文件删除失败 (或文件不存在)，将继续尝试生成...")
            # =============================

            self._update_task_strm_status(task_id, '调用 List API 生成 STRM')

            # 2. 调用 /api/fs/list 强制生成 .strm
            list_success = self._call_openlist_list_api(list_path)
            if not list_success:
                self._update_task_strm_status(task_id, '失败 (List API 失败)', is_final=True)
                logger.error(f"任务 {task_id} STRM List API 失败，无法生成 .strm 文件。")
                return

            self._update_task_strm_status(task_id, '等待 STRM 文件生成')

            # 3. 稍作等待，确保 .strm 文件生成
            time.sleep(5)
            
            self._update_task_strm_status(task_id, '调用 Copy API 复制 STRM')

            # 4. 调用 /api/fs/copy 复制 .strm 文件
            copy_success = self._call_openlist_copy_api(
                src_dir=copy_src_dir,
                dst_dir=copy_dst_dir,
                names=[strm_file_name] # 仅复制 strm 文件
            )
            
            if copy_success:
                self._update_task_strm_status(task_id, '成功', is_final=True)
                logger.debug(f"任务 {task_id} STRM 文件复制成功：{strm_file_name} -> {copy_dst_dir}")
            else:
                self._update_task_strm_status(task_id, '失败 (Copy API 失败)', is_final=True)
                logger.error(f"任务 {task_id} STRM 文件复制失败。")
                
        except Exception as e:
            self._update_task_strm_status(task_id, f'失败 (异常: {str(e)})', is_final=True)
            logger.error(f"任务 {task_id} STRM 处理时发生异常: {e} - {traceback.format_exc()}")


    def _parse_path_mappings(self) -> Dict[str, Tuple[str, str]]:
        """
        解析文件移动路径映射配置 (本地:Openlist源:Openlist目标)
        返回格式: {local_prefix: (openlist_src_prefix, openlist_dst_prefix)}
        """
        mappings = {}
        if not self._path_mappings:
            return mappings

        for line in self._path_mappings.split("\n"):
            line = line.strip()
            if not line or line.count(":") != 2:
                if line:
                    logger.warning(f"无效的文件移动路径映射格式: {line}")
                continue
            try:
                local_prefix, src_prefix, dst_prefix = line.split(":", 2)
                mappings[local_prefix.strip()] = (
                    src_prefix.strip(),
                    dst_prefix.strip(),
                )
            except ValueError:
                logger.warning(f"无效的文件移动路径映射格式: {line}")
        
        return mappings

    def _parse_strm_path_mappings(self) -> Dict[str, Tuple[str, str]]:
        """
        解析 STRM 复制路径映射配置 (Openlist目标:Strm源:Strm本地目标)
        返回格式: {dst_prefix: (strm_src_prefix, strm_dst_prefix)}
        """
        mappings = {}
        if not self._strm_path_mappings:
            return mappings

        for line in self._strm_path_mappings.split("\n"):
            line = line.strip()
            if not line or line.count(":") != 2:
                if line:
                    logger.warning(f"无效的 STRM 路径映射格式: {line}")
                continue
            try:
                dst_prefix, strm_src_prefix, strm_dst_prefix = line.split(":", 2)
                mappings[dst_prefix.strip()] = (
                    strm_src_prefix.strip(),
                    strm_dst_prefix.strip(),
                )
            except ValueError:
                logger.warning(f"无效的 STRM 路径映射格式: {line}")
        
        return mappings

    def _find_mapping(self, local_file_path: Path) -> Tuple[str, str, str, str]:
        """
        根据本地文件路径查找 Openlist 路径
        返回 (openlist_src_dir, openlist_dst_dir, file_name, error_msg)
        """
        local_file_str = str(local_file_path)
        file_name = local_file_path.name
        
        # 查找最匹配的（最长的）前缀
        best_match = ""
        for local_prefix in self._parsed_mappings.keys():
            # 标准化路径比较
            normalized_local = os.path.normpath(local_prefix)
            normalized_file = os.path.normpath(local_file_str)
            if normalized_file.startswith(normalized_local):
                if len(local_prefix) > len(best_match):
                    best_match = local_prefix

        if not best_match:
            return None, None, None, f"文件 {local_file_str} 未找到匹配的路径映射规则"

        try:
            src_prefix, dst_prefix = self._parsed_mappings[best_match]
            
            # 计算相对路径
            relative_path = os.path.relpath(local_file_str, best_match)
            relative_dir = os.path.dirname(relative_path)
            
            # 构建Openlist路径
            def build_openlist_path(base_path, rel_path):
                if rel_path == '.':
                    return base_path.rstrip('/')
                else:
                    return f"{base_path.rstrip('/')}/{rel_path.replace(os.path.sep, '/')}"

            openlist_src_dir = build_openlist_path(src_prefix, relative_dir)
            openlist_dst_dir = build_openlist_path(dst_prefix, relative_dir)
            
            logger.debug(f"路径映射结果: 本地={local_file_str}")
            logger.debug(f"  匹配规则: {best_match} -> {src_prefix}:{dst_prefix}")
            logger.debug(f"  相对路径: {relative_path}")
            logger.debug(f"  Openlist源: {openlist_src_dir}")
            logger.debug(f"  Openlist目标: {openlist_dst_dir}")
            logger.debug(f"  文件名: {file_name}")
            
            return openlist_src_dir, openlist_dst_dir, file_name, None

        except Exception as e:
            logger.error(f"计算路径映射时出错: {e}")
            return None, None, None, f"计算路径映射时出错: {e}"

    def process_new_file(self, file_path: Path):
        """
        处理新文件（在线程中运行）
        """
        
        # === 重复处理检查 ===
        with self._processing_lock:
            if file_path in self._processing_files:
                logger.debug(f"文件 {file_path} 已在处理队列中，跳过此次触发。")
                return
            self._processing_files.add(file_path)
        # ====================

        try:
            max_wait_time = 60  # 最大等待60秒
            wait_interval = 3   # 每3秒检查一次
            
            # 日志级别调整为 DEBUG
            logger.debug(f"开始处理新文件: {file_path}")
            
            # 等待文件稳定
            file_ready = False
            for i in range(max_wait_time // wait_interval):
                try:
                    if not file_path.exists():
                        logger.warning(f"文件 {file_path} 在处理前消失了")
                        return # 最终会进入 finally
                        
                    file_size = file_path.stat().st_size
                    time.sleep(wait_interval)
                    
                    if not file_path.exists():
                        logger.warning(f"文件 {file_path} 在等待稳定时消失了")
                        return # 最终会进入 finally
                        
                    new_size = file_path.stat().st_size
                    
                    # 文件大小稳定且大于0，认为文件就绪
                    if file_size == new_size and file_size > 0:
                        logger.debug(f"文件 {file_path} 已稳定，大小: {file_size} 字节")
                        file_ready = True
                        break
                    else:
                        logger.debug(f"文件 {file_path} 仍在写入中... ({file_size} -> {new_size})")
                        
                except OSError as e:
                    logger.warning(f"检查文件 {file_path} 状态时出错: {e}")
                    time.sleep(wait_interval)
            
            if not file_ready:
                logger.warning(f"文件 {file_path} 在 {max_wait_time} 秒后仍不稳定或大小为0，放弃处理。")
                return # 最终会进入 finally

            # 1. 查找路径映射
            src_dir, dst_dir, name, error = self._find_mapping(file_path)
            
            if error:
                logger.error(f"处理失败: {error}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="Openlist 移动失败",
                        text=f"文件：{file_path}\n错误：{error}",
                    )
                return # 最终会进入 finally

            # 2. 检查是否需要洗版（主动检查类似文件）
            is_wash = False
            if self._wash_mode_enabled:
                is_wash = self._check_and_clean_similar_files(dst_dir, name)
                if is_wash:
                    logger.debug(f"洗版模式：已清理类似文件，准备覆盖移动 {name}")

            # 3. 准备 Payload
            payload = {"src_dir": src_dir, "dst_dir": dst_dir, "names": [name]}

            # 如果是洗版模式，添加覆盖参数
            if is_wash:
                payload["overwrite"] = True

            logger.debug(f"准备调用 Openlist API 移动文件: {payload}")

            # 4. 调用 API
            # 返回: (task_id, code, message, is_wash_applied)
            task_id, err_code, err_msg, is_wash_applied = self._call_openlist_move_api(payload, is_wash=is_wash)

            task_started = False

            if task_id:
                logger.info(f"移动任务:  {name} 移动到 {dst_dir}")
                task_started = True

            # 5. 检查是否需要传统洗版（基于 403 错误）
            elif self._wash_mode_enabled and err_code == 403 and err_msg and "exists" in err_msg:
                logger.info(f"文件 {name} 已存在，启动传统洗版模式 (覆盖)...")
                payload["overwrite"] = True

                # 再次调用 API (洗版模式)
                task_id, err_code, err_msg, is_wash_applied = self._call_openlist_move_api(payload, is_wash=True)

                if task_id:
                    logger.info(f"传统洗版移动任务: {name} (覆盖) 到 {dst_dir}")
                    task_started = True
                else:
                    logger.error(f"Openlist API 洗版移动失败: {name} (Code: {err_code}, Msg: {err_msg})")
                    # 记录原始 payload 以供调试
                    payload.pop("overwrite", None) # 移除 overwrite 字段以便日志清晰
                    logger.error(f"Openlist API 报告失败: {err_msg} (Payload: {payload})")

            # 6. 处理最终结果
            if task_started:
                # Add task to monitor list
                new_task = {
                    "id": task_id,
                    "file": name,
                    "src_dir": src_dir,
                    "dst_dir": dst_dir,
                    "start_time": datetime.now(),
                    "status": TASK_STATUS_RUNNING,
                    "error": "",
                    "strm_status": "未执行",
                    "is_wash": is_wash_applied # 记录这是否是一个洗版任务
                }
                with task_lock:
                    self._move_tasks.append(new_task)
                    self._save_move_tasks()  # 保存任务列表

                # === 关键修改：添加任务后，确保监控服务已启动 ===
                self._start_task_monitor()
            else:
                # 移到此处，仅在标准和洗版都失败时才记录
                if err_code != 403 or "exists" not in str(err_msg):
                     logger.error(f"Openlist API 报告失败: {err_msg} (Payload: {payload})")
                
                logger.error(f"Openlist API 移动失败: {name}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="Openlist 移动失败",
                        text=f"文件：{name}\n源：{src_dir}\n目标：{dst_dir}\n错误：{err_msg}",
                    )
        except Exception as e:
            logger.error(f"处理文件 {file_path} 时发生意外错误: {e} - {traceback.format_exc()}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="Openlist 移动错误",
                    text=f"文件：{file_path}\n错误：{str(e)}",
                )
        finally:
            # === 确保从处理队列中移除 ===
            with self._processing_lock:
                self._processing_files.discard(file_path)
            logger.debug(f"文件 {file_path} 处理完毕，已移出处理队列。")
            # ========================

    def _call_openlist_move_api(self, payload: dict, is_wash: bool = False) -> Tuple[Optional[str], Optional[int], Optional[str], bool]:
        """
        调用 Openlist API /api/fs/move。
        此方法被修改为假设 Openlist/AList API 成功时会返回任务ID。
        返回 (task_id, error_code, error_message, is_wash_applied)
        """
        api_url = f"{self._openlist_url}/api/fs/move"
        try:
            data = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": self._openlist_token,
                "User-Agent": "MoviePilot-OpenlistMover-Plugin",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            logger.debug(f"调用 Openlist Move API: {api_url}")
            logger.debug(f"API Payload: {payload}")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                logger.debug(f"Openlist API 响应状态: {response_code}")
                logger.debug(f"Openlist API 响应内容: {response_body}")

                if response_code == 200:
                    try:
                        response_data = json.loads(response_body)
                        response_data_code = response_data.get("code")
                        response_data_msg = response_data.get('message', '未知错误')
                        
                        if response_data_code == 200:
                            tasks = response_data.get('data', {}).get('tasks')
                            if tasks and isinstance(tasks, list) and tasks[0].get('id'):
                                task_id = str(tasks[0]['id'])
                            else:
                                logger.warning("Openlist API 成功但未返回任务ID，生成一个模拟ID启用追踪。")
                                task_id = f"sim_task_{int(time.time() * 1000)}_{os.getpid()}"
                            
                            return task_id, 200, "Success", is_wash
                        
                        # 检查 403 exists (即使在 200 响应中)
                        elif not is_wash and response_data_code == 403 and "exists" in response_data_msg:
                            logger.debug(f"检测到文件已存在 (Code {response_data_code}): {response_data_msg}")
                            return None, 403, response_data_msg, False
                        
                        else:
                            # 其他 API 错误
                            return None, response_data_code, response_data_msg, is_wash

                    except json.JSONDecodeError:
                        logger.error(f"Openlist API 响应JSON解析失败: {response_body}")
                        return None, response_code, "JSON 解析失败", is_wash
                else:
                    logger.warning(f"Openlist API 返回非 200 状态码 {response_code}: {response_body}")
                    return None, response_code, response_body, is_wash

        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
                # 尝试解析 JSON
                error_data = json.loads(error_body)
                err_code = error_data.get("code", e.code)
                err_msg = error_data.get("message", error_body)
            except Exception:
                err_code = e.code
                err_msg = error_body or str(e)
            
            # 关键：捕获 403 exists
            if not is_wash and err_code == 403 and "exists" in err_msg:
                logger.debug(f"检测到文件已存在 (HTTP {e.code}): {err_msg}")
                return None, 403, err_msg, False
                
            logger.error(f"Openlist API 调用失败 (HTTPError {e.code}): {err_msg}")
            return None, err_code, err_msg, is_wash
            
        except urllib.error.URLError as e:
            logger.error(f"Openlist API 调用失败 (URLError): {e}")
            return None, 500, str(e), is_wash
        except Exception as e:
            logger.error(f"调用 Openlist API 时出错: {e} - {traceback.format_exc()}")
            return None, 500, str(e), is_wash
            
    def _call_openlist_task_api(self, task_id: str) -> Dict[str, Any]:
        """
        调用 Openlist API 检查任务状态 (模拟 AList /api/admin/task/copy/info)
        返回: {'state': int, 'error': str}
        """
        
        # 针对模拟的任务ID进行特殊处理，以避免频繁失败
        if task_id.startswith('sim_task_'):
             # 模拟任务运行一段时间后成功
             with task_lock:
                for task in self._move_tasks:
                    if task['id'] == task_id:
                        if (datetime.now() - task['start_time']).total_seconds() > 120:
                            return {'state': TASK_STATUS_SUCCESS, 'error': ''}
                        break
             return {'state': TASK_STATUS_RUNNING, 'error': ''}

        # 假设 Openlist 支持 AList 风格的任务查询 API
        api_url = f"{self._openlist_url}/api/admin/task/move/info?tid={task_id}" 
        
        headers = {
            "Authorization": self._openlist_token,
            "User-Agent": "MoviePilot-OpenlistMover-Plugin",
        }
        
        try:
            req = urllib.request.Request(api_url, headers=headers, method="POST")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    response_data = json.loads(response_body)
                    if response_data.get("code") == 200:
                        task_info = response_data.get('data', {})
                        state = task_info.get('state', TASK_STATUS_RUNNING)
                        error = task_info.get('error', '')
                        return {'state': state, 'error': error}
                    else:
                        logger.warning(f"Openlist Task API 报告失败: {response_data.get('message')} - {task_id}")
                        return {'state': TASK_STATUS_RUNNING, 'error': ''} 
                else:
                    logger.warning(f"Openlist Task API 返回非 200 状态码 {response_code}: {response_body}")
                    return {'state': TASK_STATUS_RUNNING, 'error': ''}

        except urllib.error.URLError as e:
            logger.error(f"Openlist Task API 调用失败 (URLError): {e}")
            return {'state': TASK_STATUS_RUNNING, 'error': ''} 
        except Exception as e:
            logger.error(f"调用 Openlist Task API 时出错: {e}")
            return {'state': TASK_STATUS_RUNNING, 'error': ''}

    def _call_openlist_list_api(self, path: str) -> bool:
        """
        调用 Openlist API /api/fs/list 强制生成 .strm 文件
        """
        payload = {
            "path": path,
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": True # 强制刷新
        }
        
        try:
            data = json.dumps(payload).encode("utf-8")
            api_url = f"{self._openlist_url}/api/fs/list"

            headers = {
                "Content-Type": "application/json",
                "Authorization": self._openlist_token,
                "User-Agent": "MoviePilot-OpenlistMover-StrmList",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            # 日志级别调整为 DEBUG
            logger.debug(f"调用 Openlist List API (STRM): {api_url}")
            logger.debug(f"List API Payload: {payload}")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    response_data = json.loads(response_body)
                    if response_data.get("code") == 200:
                        logger.debug(f"Openlist List API 成功触发 .strm 文件生成：{path}")
                        return True
                    else:
                        error_msg = response_data.get('message', '未知错误')
                        logger.warning(f"Openlist List API 报告失败: {error_msg} (Path: {path})")
                        return False
                else:
                    logger.warning(f"Openlist List API 返回非 200 状态码 {response_code}: {response_body}")
                    return False
        except Exception as e:
            logger.error(f"调用 Openlist List API 时出错: {e} - {traceback.format_exc()}")
            return False

    def _call_openlist_copy_api(self, src_dir: str, dst_dir: str, names: List[str]) -> bool:
        """
        调用 Openlist API /api/fs/copy 复制 .strm 文件
        """
        payload = {
            "src_dir": src_dir,
            "dst_dir": dst_dir,
            "names": names
        }
        
        try:
            data = json.dumps(payload).encode("utf-8")
            api_url = f"{self._openlist_url}/api/fs/copy"

            headers = {
                "Content-Type": "application/json",
                "Authorization": self._openlist_token,
                "User-Agent": "MoviePilot-OpenlistMover-StrmCopy",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            # 日志级别调整为 DEBUG
            logger.debug(f"调用 Openlist Copy API (STRM): {api_url}")
            logger.debug(f"Copy API Payload: {payload}")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    response_data = json.loads(response_body)
                    if response_data.get("code") == 200:
                        # 日志级别调整为 DEBUG
                        logger.debug(f"Openlist Copy API 成功复制 .strm 文件：{names} -> {dst_dir}")
                        return True
                    else:
                        error_msg = response_data.get('message', '未知错误')
                        logger.warning(f"Openlist Copy API 报告失败: {error_msg} (Names: {names})")
                        return False
                else:
                    logger.warning(f"Openlist Copy API 返回非 200 状态码 {response_code}: {response_body}")
                    return False
        except Exception as e:
            logger.error(f"调用 Openlist Copy API 时出错: {e} - {traceback.format_exc()}")
            return False

    def _call_openlist_remove_api(self, dir_path: str, names: List[str]) -> bool:
        """
        (新增) 调用 Openlist API /api/fs/remove 删除 .strm 和 .json 文件
        """
        payload = {
            "dir": dir_path,
            "names": names
        }
        
        try:
            data = json.dumps(payload).encode("utf-8")
            api_url = f"{self._openlist_url}/api/fs/remove"

            headers = {
                "Content-Type": "application/json",
                "Authorization": self._openlist_token,
                "User-Agent": "MoviePilot-OpenlistMover-WashRemove",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            logger.debug(f"调用 Openlist Remove API (Wash): {api_url}")
            logger.debug(f"Remove API Payload: {payload}")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    response_data = json.loads(response_body)
                    if response_data.get("code") == 200:
                        logger.debug(f"Openlist Remove API 成功删除文件：{names} 从 {dir_path}")
                        return True
                    else:
                        error_msg = response_data.get('message', '未知错误')
                        # 如果文件本身不存在，也算“成功”
                        if "not exist" in error_msg:
                             logger.debug(f"Openlist Remove API：文件不存在，视为删除成功。 (Msg: {error_msg})")
                             return True
                        
                        logger.warning(f"Openlist Remove API 报告失败: {error_msg} (Payload: {payload})")
                        return False
                else:
                    logger.warning(f"Openlist Remove API 返回非 200 状态码 {response_code}: {response_body}")
                    return False
        except Exception as e:
            logger.error(f"调用 Openlist Remove API 时出错: {e} - {traceback.format_exc()}")
            return False


    def _call_openlist_clear_tasks_api(self, task_type: str) -> bool:
        """
        调用 Openlist API 清空成功任务 (/api/admin/task/{task_type}/clear_succeeded)
        task_type 应该是 'copy' 或 'move'
        """
        if task_type not in ["copy", "move"]:
            logger.error(f"无效的 Openlist 任务类型: {task_type}")
            return False
            
        api_url = f"{self._openlist_url}/api/admin/task/{task_type}/clear_succeeded"
        
        headers = {
            "Authorization": self._openlist_token,
            "User-Agent": f"MoviePilot-OpenlistMover-ClearTasks-{task_type.capitalize()}",
        }
        
        try:
            req = urllib.request.Request(api_url, headers=headers, method="POST")

            # 日志级别调整为 debug
            logger.debug(f"调用 Openlist 清空 {task_type} 任务 API: {api_url}")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    response_data = json.loads(response_body)
                    if response_data.get("code") == 200:
                        logger.debug(f"Openlist {task_type.capitalize()} 成功任务记录清空成功。")
                        return True
                    else:
                        error_msg = response_data.get('message', '未知错误')
                        logger.warning(f"Openlist 清空 {task_type} 任务 API 报告失败: {error_msg}")
                        return False
                else:
                    logger.warning(f"Openlist 清空 {task_type} 任务 API 返回非 200 状态码 {response_code}: {response_body}")
                    return False

        except urllib.error.URLError as e:
            logger.error(f"Openlist 清空 {task_type} 任务 API 调用失败 (URLError): {e}")
            return False
        except Exception as e:
            logger.error(f"调用 Openlist 清空 {task_type} 任务 API 时出错: {e} - {traceback.format_exc()}")
            return False

    def _call_openlist_get_api(self, path: str) -> Tuple[Optional[bool], Optional[Dict[str, Any]]]:
        """
        调用 Openlist API /api/fs/get 检查文件或目录是否存在
        返回 (exists, file_info)
        exists: True=存在, False=不存在, None=结果不明确（应取消操作）
        """
        api_url = f"{self._openlist_url}/api/fs/get"

        payload = {
            "path": path,
            "password": ""
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": self._openlist_token,
                "User-Agent": "MoviePilot-OpenlistMover-FileCheck",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            logger.debug(f"调用 Openlist Get API: {api_url}")
            logger.debug(f"Get API Payload: {payload}")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    response_data = json.loads(response_body)
                    if response_data.get("code") == 200:
                        logger.debug(f"Openlist Get API 成功: {path} 存在")
                        return True, response_data.get('data', {})
                    else:
                        error_msg = response_data.get('message', '未知错误')
                        if "not exist" in error_msg.lower() or "not found" in error_msg.lower():
                            logger.debug(f"Openlist Get API: {path} 不存在")
                            return False, None
                        else:
                            logger.warning(f"Openlist Get API 报告失败: {error_msg} (Path: {path})")
                            return None, None  # 结果不明确，应取消操作
                else:
                    logger.warning(f"Openlist Get API 返回非 200 状态码 {response_code}: {response_body}")
                    return None, None  # 结果不明确，应取消操作
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.debug(f"Openlist Get API: {path} 不存在 (HTTP 404)")
                return False, None
            else:
                logger.error(f"Openlist Get API 调用失败 (HTTPError {e.code}): {e}")
                return None, None  # 结果不明确，应取消操作
        except Exception as e:
            logger.error(f"调用 Openlist Get API 时出错: {e} - {traceback.format_exc()}")
            return None, None  # 结果不明确，应取消操作

    def _check_and_clean_similar_files(self, dst_dir: str, target_file: str) -> bool:
        """
        检查目标目录中是否存在类似文件（相同文件名但不同后缀），如果存在则删除
        返回 True 表示需要洗版，False 表示不需要
        """
        if not self._wash_mode_enabled:
            return False

        target_path = Path(target_file)
        target_name_without_ext = target_path.stem  # 获取文件名（不含后缀）

        logger.debug(f"检查目标目录 {dst_dir} 中是否存在类似文件: {target_name_without_ext}.*")

        # 构建目录路径进行检查
        dir_exists, dir_info = self._call_openlist_get_api(dst_dir)
        if dir_exists is None:
            logger.warning(f"洗版模式：目标目录 {dst_dir} 存在性检查结果不明确，取消移动操作")
            return False  # 结果不明确，取消操作
        if not dir_exists:
            logger.debug(f"目标目录 {dst_dir} 不存在，无需检查类似文件")
            return False

        # 假设目录存在，我们需要列出目录内容来检查类似文件
        # 由于没有直接的 list API，我们通过尝试检查每个可能的视频文件来模拟
        files_to_delete = []

        # 检查所有视频扩展名的文件是否存在
        for ext in VIDEO_EXTENSIONS:
            if ext == target_path.suffix.lower():
                continue  # 跳过目标文件本身的后缀

            check_file_path = f"{dst_dir.rstrip('/')}/{target_name_without_ext}{ext}"
            file_exists, file_info = self._call_openlist_get_api(check_file_path)

            if file_exists is None:
                logger.warning(f"洗版模式：文件 {check_file_path} 存在性检查结果不明确，取消移动操作")
                return False  # 结果不明确，取消操作
            elif file_exists:
                logger.debug(f"发现类似文件需要删除: {check_file_path}")
                files_to_delete.append(f"{target_name_without_ext}{ext}")

        # 如果发现需要删除的文件，执行删除操作
        if files_to_delete:
            logger.debug(f"洗版模式：删除 {len(files_to_delete)} 个类似文件: {files_to_delete}")
            delete_success = self._call_openlist_remove_api(dst_dir, files_to_delete)

            if delete_success:
                logger.debug(f"洗版模式：成功删除类似文件")
                return True
            else:
                logger.warning(f"洗版模式：删除类似文件失败，取消移动操作")
                return False  # 删除失败时取消移动操作

        logger.debug(f"目标目录 {dst_dir} 中未发现类似文件")
        return False

    def _scan_local_directories(self):
        """
        扫描本地监控目录，检查是否有未成功上传的视频文件
        """
        if not self._enabled or not self._global_scan_enabled:
            return

        logger.info("开始全局扫描本地监控目录...")

        # 获取监控目录列表
        monitor_dirs = [
            d.strip() for d in self._monitor_paths.split("\n") if d.strip()
        ]

        if not monitor_dirs:
            logger.warning("全局扫描：未配置监控目录")
            return

        total_files_found = 0
        total_files_processed = 0

        for monitor_dir in monitor_dirs:
            if not os.path.exists(monitor_dir):
                logger.warning(f"全局扫描：监控目录不存在 - {monitor_dir}")
                continue

            try:
                logger.info(f"全局扫描：扫描目录 {monitor_dir}")

                # 递归扫描目录中的所有视频文件
                for root, dirs, files in os.walk(monitor_dir):
                    for file in files:
                        file_path = Path(root) / file
                        file_suffix = file_path.suffix.lower()

                        # 检查是否为视频文件
                        if file_suffix in VIDEO_EXTENSIONS:
                            total_files_found += 1

                            # 检查是否为临时文件
                            if file_suffix in TEMP_EXTENSIONS:
                                logger.debug(f"全局扫描：跳过临时文件 {file_path}")
                                continue

                            # 检查文件是否正在处理中
                            with self._processing_lock:
                                if file_path in self._processing_files:
                                    logger.debug(f"全局扫描：文件正在处理中，跳过 {file_path}")
                                    continue

                            # 检查文件是否已经在任务列表中
                            file_already_in_tasks = False
                            with task_lock:
                                for task in self._move_tasks:
                                    if task['file'] == file and task['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]:
                                        file_already_in_tasks = True
                                        break

                            if file_already_in_tasks:
                                logger.debug(f"全局扫描：文件已在任务列表中，跳过 {file_path}")
                                continue

                            # 检查文件是否稳定（大小不再变化）
                            try:
                                initial_size = file_path.stat().st_size
                                time.sleep(2)  # 等待2秒
                                final_size = file_path.stat().st_size

                                if initial_size == final_size and initial_size > 0:
                                    # 文件稳定，触发处理
                                    logger.info(f"全局扫描：发现未上传文件 {file_path}")
                                    threading.Thread(
                                        target=self.process_new_file, args=(file_path,)
                                    ).start()
                                    total_files_processed += 1
                                else:
                                    logger.debug(f"全局扫描：文件仍在写入中，跳过 {file_path}")
                            except OSError as e:
                                logger.warning(f"全局扫描：检查文件状态失败 {file_path}: {e}")

            except Exception as e:
                logger.error(f"全局扫描：扫描目录 {monitor_dir} 时出错: {e}")

        logger.info(f"全局扫描完成：发现 {total_files_found} 个视频文件，处理了 {total_files_processed} 个文件")

    def _start_global_scan_scheduler(self):
        """
        启动全局扫描定时器
        """
        if not self._global_scan_enabled:
            return

        # 停止现有的全局扫描定时器
        self._stop_global_scan_scheduler()

        try:
            # 解析扫描时间
            hour, minute = map(int, self._global_scan_time.split(":"))

            timezone = 'Asia/Shanghai'
            self._global_scan_scheduler = BackgroundScheduler(timezone=timezone)

            # 添加每天定时扫描任务
            self._global_scan_scheduler.add_job(
                self._scan_local_directories,
                "cron",
                hour=hour,
                minute=minute,
                name="Openlist 全局文件扫描"
            )

            self._global_scan_scheduler.start()
            logger.info(f"全局扫描定时器已启动，每天 {self._global_scan_time} 执行扫描")

        except Exception as e:
            logger.error(f"启动全局扫描定时器失败: {e}")

    def _stop_global_scan_scheduler(self):
        """
        停止全局扫描定时器
        """
        if self._global_scan_scheduler:
            try:
                self._global_scan_scheduler.shutdown(wait=False)
                self._global_scan_scheduler = None
                logger.debug("全局扫描定时器已停止")
            except Exception as e:
                logger.error(f"停止全局扫描定时器失败: {e}")