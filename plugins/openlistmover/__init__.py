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
# Assuming settings and StringUtils are available from the broader MoviePilot context
# from app.core.config import settings
# from app.utils.string import StringUtils

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
            logger.info(f"监测到新视频文件：{file_path}")
            # 使用线程处理，避免阻塞监控
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
    plugin_version = "3.3"
    # 插件作者
    plugin_author = "lyzd1"
    # 作者主页
    author_url = "https://github.com/lyzd1"
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
    
    # {local_prefix: (openlist_src_prefix, openlist_dst_prefix)}
    _parsed_mappings: Dict[str, Tuple[str, str]] = {}
    
    # {dst_prefix: (strm_src_prefix, strm_dst_prefix)}
    _parsed_strm_mappings: Dict[str, Tuple[str, str]] = {} # 新增 strm 映射解析结果
    
    # Task tracking list
    # Format: [{"id": str, "file": str, "src_dir": str, "dst_dir": str, "start_time": datetime, "status": int, "error": str, "strm_status": str}]
    _move_tasks: List[Dict[str, Any]] = []
    _max_task_duration = 60 * 60 # 60 minutes in seconds (最长 60min)
    _task_check_interval = 60 # 1 minute in seconds (每隔 1min)

    # === 新增属性用于任务计数和清空配置 ===
    _successful_moves_count = 0  # 累计成功移动次数
    _clear_api_threshold = 10    # 自动清空 Openlist API 任务记录的阈值 (默认 10 次成功)
    _clear_panel_threshold = 30  # 自动清空成功任务面板记录的阈值 (默认 30 次成功)
    _keep_successful_tasks = 3   # 清空面板时保留的最新成功任务数量 (默认 3 个)
    # ======================================

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
            
            # === 加载新的配置项 ===
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
            # =======================

        # 停止现有任务
        self.stop_service()

        if self._enabled:
            if not self._openlist_url or not self._openlist_token:
                logger.error("Openlist Mover 已启用，但 Openlist URL 或 Token 未配置！")
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
            
            self._start_task_monitor()
            logger.info("Openlist 视频文件移动插件已启动")

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
                                            "text": "用于调用 Openlist 移动文件 API。URL 必须包含 http/https 协议头。",
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
                                            "label": "Openlist URL",
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
                                            "label": "Openlist Token",
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
                                            "text": "成功完成的移动任务达到设定次数后，将自动清空 Openlist 任务队列记录或插件面板记录。清空后，计数器将重置。",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "clear_api_threshold",
                                            "label": "清空Openlist任务API阈值 (次)",
                                            "type": "number",
                                            "min": 1,
                                            "placeholder": "默认 10 (成功 10 次清空 Openlist 任务队列)",
                                        },
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "clear_panel_threshold",
                                            "label": "清空面板成功记录阈值 (次)",
                                            "type": "number",
                                            "min": 1,
                                            "placeholder": "默认 30 (成功 30 次清空面板成功记录)",
                                        },
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
            "clear_api_threshold": 10,
            "clear_panel_threshold": 30,
            "keep_successful_tasks": 3
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
            strm_color = 'text-warning' if strm_status == '失败' else ('text-success' if strm_status == '成功' else 'text-muted')
            
            return {
                'component': 'tr',
                'props': {'class': 'text-sm'},
                'content': [
                    # 移除 任务ID 的显示
                    {'component': 'td', 'text': task.get('file', 'N/A')},
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
        
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
                logger.debug("Openlist Mover 任务监控服务已停止")
            except Exception as e:
                logger.error(f"停止任务监控失败：{str(e)}")
            self._scheduler = None

        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    logger.error(f"停止目录监控失败：{str(e)}")
        self._observer = []
        logger.debug("Openlist Mover 服务停止完成")

    def _start_task_monitor(self):
        """
        启动任务监控定时器
        """
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
            logger.info("Openlist Mover 任务监控服务已启动")
        except Exception as e:
            logger.error(f"启动 Openlist Mover 任务监控服务失败: {e}")
            
    def _send_task_notification(self, task: Dict[str, Any], title: str, text: str):
        """
        发送通知消息 (修复: 重新添加此方法)
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
        tasks_to_keep = []
        
        with task_lock:
            # 遍历所有任务
            for task in self._move_tasks:
                if task['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]:
                    # 检查超时
                    if (datetime.now() - task['start_time']).total_seconds() > self._max_task_duration:
                        task['status'] = TASK_STATUS_FAILED
                        task['error'] = f"任务超时 ({int(self._max_task_duration / 60)} 分钟)"
                        self._send_task_notification(task, "Openlist 移动超时", f"文件：{task['file']}\n源：{task['src_dir']}\n目标：{task['dst_dir']}\n错误：任务超时")
                        logger.error(f"Openlist 移动任务 {task['id']} 超时")
                        tasks_to_keep.append(task)
                        continue

                    # 查询状态
                    try:
                        task_info = self._call_openlist_task_api(task['id'])
                        
                        new_status = task_info.get('state') # state: 0-等待中, 1-进行中, 2-成功, 3-失败
                        error_msg = task_info.get('error')
                        
                        if new_status == TASK_STATUS_SUCCESS:
                            task['status'] = new_status
                            task['strm_status'] = '开始处理'
                            self._process_strm_creation(task) # 任务成功后处理 STRM
                            
                            # 增加成功计数
                            self._successful_moves_count += 1
                            
                            move_success_text = (
                                f"✅ 文件移动成功\n"
                                f"🎬 视频文件：{task['dst_dir']}/{task['file']}\n"
                                f"🔗 STRM状态：{task.get('strm_status', '未处理')}"
                            )
                            self._send_task_notification(
                                task,
                                "Openlist 移动完成",
                                move_success_text
                            )
                        elif new_status == TASK_STATUS_FAILED:
                            task['status'] = new_status
                            task['error'] = error_msg if error_msg else "Openlist 报告失败"
                            self._send_task_notification(task, "Openlist 移动失败", f"文件：{task['file']}\n源：{task['src_dir']}\n目标：{task['dst_dir']}\n错误：{task['error']}")
                        elif new_status == TASK_STATUS_RUNNING:
                            task['status'] = new_status
                            
                    except Exception as e:
                        logger.error(f"查询 Openlist 任务 {task['id']} 状态失败: {e}")
                
                tasks_to_keep.append(task)
            
            # === 任务清空逻辑 ===
            
            # 1. 检查 API 任务清空阈值
            if self._successful_moves_count >= self._clear_api_threshold:
                logger.info(f"成功移动任务达到 {self._clear_api_threshold} 次，准备清空 Openlist 任务 API 记录。")
                
                # 调用清空 Openlist API 中的成功任务
                self._call_openlist_clear_tasks_api("copy") # 清空复制成功的任务 (Strm 任务)
                self._call_openlist_clear_tasks_api("move") # 清空移动成功的任务
                
                logger.info(f"Openlist API 任务记录清空完毕。")


            # 2. 检查 插件面板 清空阈值
            if self._successful_moves_count >= self._clear_panel_threshold:
                logger.info(f"成功移动任务达到 {self._clear_panel_threshold} 次，准备清空插件面板成功记录，保留最新 {self._keep_successful_tasks} 条。")
                
                # 提取活跃任务和失败任务
                active_tasks_panel = [t for t in tasks_to_keep if t['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]]
                failed_tasks_panel = [t for t in tasks_to_keep if t['status'] == TASK_STATUS_FAILED]

                # 提取所有成功任务并排序
                successful_tasks = sorted(
                    [t for t in tasks_to_keep if t['status'] == TASK_STATUS_SUCCESS],
                    key=lambda x: x['start_time'], reverse=True
                )
                
                # 保留最新的成功任务
                tasks_to_keep_panel = successful_tasks[:self._keep_successful_tasks]
                
                # 重新构建任务列表：活跃 + 失败 + 保留的成功任务
                tasks_to_keep = active_tasks_panel + failed_tasks_panel + tasks_to_keep_panel
                
                logger.info(f"插件面板成功记录清空完毕，保留 {len(tasks_to_keep_panel)} 条最新成功记录。")

            # 3. 如果任一清空操作被触发（即成功计数达到最小阈值），则重置计数器
            if self._successful_moves_count >= min(self._clear_api_threshold, self._clear_panel_threshold):
                 self._successful_moves_count = 0
                 logger.info("成功计数器已重置。")

            self._move_tasks = tasks_to_keep
            
            logger.debug(f"Openlist Mover 任务检查完成，当前活跃任务数: {len([t for t in self._move_tasks if t['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]])}")

    def _process_strm_creation(self, task: Dict[str, Any]):
        """
        处理 STRM 文件生成和复制
        """
        # 1. 查找 STRM 路径映射
        dst_dir = task['dst_dir']
        file_name_ext = task['file']
        # 确保文件名处理正确，只替换最后一个后缀
        file_name_path = Path(file_name_ext)
        strm_file_name = file_name_path.with_suffix('.strm').name

        # 查找最匹配的（最长的）Openlist目标前缀
        best_match = ""
        for dst_prefix in self._parsed_strm_mappings.keys():
            normalized_dst = os.path.normpath(dst_prefix)
            normalized_task_dir = os.path.normpath(dst_dir)
            if normalized_task_dir.startswith(normalized_dst):
                if len(dst_prefix) > len(best_match):
                    best_match = dst_prefix
        
        if not best_match:
            task['strm_status'] = '跳过 (无映射规则)'
            logger.debug(f"任务 {task['id']} 移动成功，但未找到匹配的 STRM 映射规则，跳过 STRM 复制。")
            return
            
        try:
            dst_prefix = best_match
            strm_src_prefix, strm_dst_prefix = self._parsed_strm_mappings[dst_prefix]
            
            # 计算相对路径
            # Path(dst_dir).relative_to(Path(dst_prefix)) 可能会在路径不规范时失败，使用 os.path.relpath
            # 确保路径都是绝对路径或规范化
            relative_dir_str = os.path.relpath(dst_dir, dst_prefix)
            relative_dir = relative_dir_str.replace(os.path.sep, '/')
            
            # 构建 List 路径 (需要 List 目录，而不是文件)
            list_path = f"{strm_src_prefix.rstrip('/')}/{relative_dir}"
            
            # 构建 Copy 路径 (源和目标目录)
            copy_src_dir = list_path
            copy_dst_dir = f"{strm_dst_prefix.rstrip('/')}/{relative_dir}"
            
            logger.debug(f"任务 {task['id']} 成功，开始 STRM 处理:")
            logger.debug(f"  List 路径: {list_path}")
            logger.debug(f"  Copy 源: {copy_src_dir}")
            logger.debug(f"  Copy 目标: {copy_dst_dir}")
            logger.debug(f"  文件名: {strm_file_name}")

            # 2. 调用 /api/fs/list 强制生成 .strm
            list_success = self._call_openlist_list_api(list_path)
            if not list_success:
                task['strm_status'] = '失败 (List API 失败)'
                logger.error(f"任务 {task['id']} STRM List API 失败，无法生成 .strm 文件。")
                return

            # 3. 稍作等待，确保 .strm 文件生成
            time.sleep(5)
            
            # 4. 调用 /api/fs/copy 复制 .strm 文件
            copy_success = self._call_openlist_copy_api(
                src_dir=copy_src_dir,
                dst_dir=copy_dst_dir,
                names=[strm_file_name]
            )
            
            if copy_success:
                task['strm_status'] = '成功'
                logger.debug(f"任务 {task['id']} STRM 文件复制成功：{strm_file_name} -> {copy_dst_dir}")
            else:
                task['strm_status'] = '失败 (Copy API 失败)'
                logger.error(f"任务 {task['id']} STRM 文件复制失败。")
                
        except Exception as e:
            task['strm_status'] = f'失败 (异常: {str(e)})'
            logger.error(f"任务 {task['id']} STRM 处理时发生异常: {e} - {traceback.format_exc()}")


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
        max_wait_time = 60  # 最大等待60秒
        wait_interval = 3   # 每3秒检查一次
        
        # 日志级别调整为 DEBUG
        logger.debug(f"开始处理新文件: {file_path}")
        
        # 等待文件稳定
        for i in range(max_wait_time // wait_interval):
            try:
                if not file_path.exists():
                    logger.warning(f"文件 {file_path} 在处理前消失了")
                    return
                    
                file_size = file_path.stat().st_size
                time.sleep(wait_interval)
                new_size = file_path.stat().st_size
                
                # 文件大小稳定且大于0，认为文件就绪
                if file_size == new_size and file_size > 0:
                    logger.info(f"文件 {file_path} 已稳定，大小: {file_size} 字节")
                    break
                else:
                    logger.debug(f"文件 {file_path} 仍在写入中... ({file_size} -> {new_size})")
                    
            except OSError as e:
                logger.warning(f"检查文件状态时出错: {e}")
                time.sleep(wait_interval)
        
        try:
            if not file_path.exists():
                logger.warning(f"文件 {file_path} 在等待过程中消失了")
                return

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
                return

            # 2. 准备 Payload
            payload = {"src_dir": src_dir, "dst_dir": dst_dir, "names": [name]}
            
            logger.info(f"准备调用 Openlist API 移动文件: {payload}")

            # 3. 调用 API
            task_id = self._call_openlist_move_api(payload)
            if task_id:
                logger.info(f"Openlist API 成功启动移动任务: {task_id} - {name} 从 {src_dir} 到 {dst_dir}")
                
                # Add task to monitor list
                new_task = {
                    "id": task_id,
                    "file": name,
                    "src_dir": src_dir,
                    "dst_dir": dst_dir,
                    "start_time": datetime.now(),
                    "status": TASK_STATUS_RUNNING,
                    "error": "",
                    "strm_status": "未执行" # 初始化 STRM 状态
                }
                with task_lock:
                    self._move_tasks.append(new_task)
            else:
                logger.error(f"Openlist API 移动失败: {name}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="Openlist 移动失败",
                        text=f"文件：{name}\n源：{src_dir}\n目标：{dst_dir}\n请检查 Openlist 日志。",
                    )
        except Exception as e:
            logger.error(f"处理文件 {file_path} 时发生意外错误: {e} - {traceback.format_exc()}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="Openlist 移动错误",
                    text=f"文件：{file_path}\n错误：{str(e)}",
                )

    def _call_openlist_move_api(self, payload: dict) -> Optional[str]:
        """
        调用 Openlist API /api/fs/move。
        此方法被修改为假设 Openlist/AList API 成功时会返回任务ID。
        返回任务ID (string) 或 None。
        """
        try:
            data = json.dumps(payload).encode("utf-8")
            api_url = f"{self._openlist_url}/api/fs/move"

            headers = {
                "Content-Type": "application/json",
                "Authorization": self._openlist_token,
                "User-Agent": "MoviePilot-OpenlistMover-Plugin",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            # 日志级别调整为 DEBUG
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
                        if response_data.get("code") == 200:
                            # 假设响应包含任务ID，类似 AList 的 /api/fs/move
                            tasks = response_data.get('data', {}).get('tasks')
                            if tasks and isinstance(tasks, list) and tasks[0].get('id'):
                                return str(tasks[0]['id'])
                            else:
                                # 生成一个模拟ID启用追踪
                                logger.warning("Openlist API 成功但未返回任务ID，生成一个模拟ID启用追踪。")
                                return f"sim_task_{int(time.time() * 1000)}_{os.getpid()}" 
                        else:
                            error_msg = response_data.get('message', '未知错误')
                            logger.warning(f"Openlist API 报告失败: {error_msg} (Payload: {payload})")
                            return None
                    except json.JSONDecodeError:
                        logger.error(f"Openlist API 响应JSON解析失败: {response_body}")
                        return None
                else:
                    logger.warning(f"Openlist API 返回非 200 状态码 {response_code}: {response_body}")
                    return None

        except urllib.error.URLError as e:
            logger.error(f"Openlist API 调用失败 (URLError): {e}")
            return None
        except Exception as e:
            logger.error(f"调用 Openlist API 时出错: {e} - {traceback.format_exc()}")
            return None
            
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

            # 日志级别调整为 INFO
            logger.info(f"调用 Openlist 清空 {task_type} 任务 API: {api_url}")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    response_data = json.loads(response_body)
                    if response_data.get("code") == 200:
                        logger.info(f"Openlist {task_type.capitalize()} 成功任务记录清空成功。")
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

