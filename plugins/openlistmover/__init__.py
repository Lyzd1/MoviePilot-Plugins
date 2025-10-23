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

# Task status definitions (simplified)
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
    plugin_desc = "监控本地目录，当有新视频文件生成时，自动通过 Openlist API 将其移动到指定的云盘目录。"
    # 插件图标
    plugin_icon = "Ombi_A.png"
    # 插件版本
    plugin_version = "1.0"
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
    _observer = []
    _scheduler: Optional[BackgroundScheduler] = None
    
    # {local_prefix: (openlist_src_prefix, openlist_dst_prefix)}
    _parsed_mappings: Dict[str, Tuple[str, str]] = {}
    
    # Task tracking list
    # Format: [{"id": str, "file": str, "src_dir": str, "dst_dir": str, "start_time": datetime, "status": int, "error": str}]
    _move_tasks: List[Dict[str, Any]] = []
    _max_task_duration = 60 * 60 # 60 minutes in seconds
    _task_check_interval = 60 # 1 minute in seconds

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
                
            # 解析映射
            self._parsed_mappings = self._parse_path_mappings()
            if not self._parsed_mappings:
                logger.error("Openlist Mover 路径映射配置无效")
                return
                
            logger.info(f"Openlist Mover 已加载 {len(self._parsed_mappings)} 条路径映射")

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
        # ... (Configuration form remains the same)
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
                                ],
                            },
                        ],
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
                                ],
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
                                ],
                            },
                        ],
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
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "路径映射规则 (本地:Openlist源:Openlist目标)",
                                            "rows": 6,
                                            "placeholder": "格式：本地监控目录:Openlist源目录:Openlist目标目录\n每行一条规则\n\n例如：\n/downloads/watch:/Local/watch:/YP/Video\n\n说明：\n当本地监控到 /downloads/watch/电影/S01/E01.mkv\nOpenlist 将会执行移动：\n源：/Local/watch/电影/S01/E01.mkv\n目标：/YP/Video/电影/S01/E01.mkv",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "title": "工作流程说明",
                            "text": "1. 插件监控 '本地监控目录' (例如 /downloads/watch)。\n2. '本地监控目录' 必须在 Openlist 中被添加为存储，其 Openlist 路径对应 'Openlist源目录' (例如 /Local/watch)。\n3. 当新文件 /downloads/watch/A/B.mkv 出现时，插件会查找映射规则。\n4. 插件命令 Openlist 将 /Local/watch/A/B.mkv 移动到 /YP/Video/A/B.mkv ('Openlist目标目录')。\n5. Openlist 执行此移动操作，本地文件 /downloads/watch/A/B.mkv 将被移动（即消失）。",
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
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，显示任务列表
        """
        
        with task_lock:
            active_tasks = [t for t in self._move_tasks if t['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]]
            finished_tasks = sorted(
                [t for t in self._move_tasks if t['status'] in [TASK_STATUS_SUCCESS, TASK_STATUS_FAILED]],
                key=lambda x: x['start_time'], reverse=True
            )[:50] # Only show latest 50 finished tasks

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
            return {
                'component': 'tr',
                'props': {'class': 'text-sm'},
                'content': [
                    {'component': 'td', 'text': task.get('id', 'N/A')},
                    {'component': 'td', 'text': task.get('file', 'N/A')},
                    {'component': 'td', 'text': task.get('src_dir', 'N/A')},
                    {'component': 'td', 'text': task.get('dst_dir', 'N/A')},
                    {'component': 'td', 'text': task['start_time'].strftime('%Y-%m-%d %H:%M:%S') if 'start_time' in task else 'N/A'},
                    {
                        'component': 'td', 
                        'props': {'class': get_status_color(task['status'])},
                        'text': get_status_text(task['status'])
                    },
                    {'component': 'td', 'text': task.get('error', '') if task['status'] == TASK_STATUS_FAILED else ''},
                ]
            }

        # Active Tasks Table
        active_trs = [task_to_tr(t) for t in active_tasks]
        
        # Finished Tasks Table
        finished_trs = [task_to_tr(t) for t in finished_tasks]

        table_headers = [
            {'text': '任务ID', 'class': 'text-start ps-4'},
            {'text': '文件名', 'class': 'text-start ps-4'},
            {'text': '源目录', 'class': 'text-start ps-4'},
            {'text': '目标目录', 'class': 'text-start ps-4'},
            {'text': '开始时间', 'class': 'text-start ps-4'},
            {'text': '状态', 'class': 'text-start ps-4'},
            {'text': '错误信息', 'class': 'text-start ps-4'},
        ]

        page_content = []
        
        # Active Tasks Section
        if active_trs:
            page_content.extend([
                {
                    'component': 'VCardTitle',
                    'text': '当前活跃任务'
                },
                {
                    'component': 'VTable',
                    'props': {'hover': True},
                    'content': [
                        {'component': 'thead', 'content': [{'component': 'th', **{'props': {'class': h['class']}, 'text': h['text']}} for h in table_headers]},
                        {'component': 'tbody', 'content': active_trs}
                    ]
                }
            ])

        # Finished Tasks Section
        page_content.extend([
            {
                'component': 'VCardTitle',
                'text': '最近完成任务'
            },
            {
                'component': 'VTable',
                'props': {'hover': True},
                'content': [
                    {'component': 'thead', 'content': [{'component': 'th', **{'props': {'class': h['class']}, 'text': h['text']}} for h in table_headers]},
                    {'component': 'tbody', 'content': finished_trs}
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
            # Assuming settings.TZ is available
            # from app.core.config import settings
            # timezone = settings.TZ
            timezone = 'Asia/Shanghai' # Fallback for snippet
            self._scheduler = BackgroundScheduler(timezone=timezone)
            self._scheduler.add_job(
                self._check_move_tasks, 
                "interval",
                seconds=self._task_check_interval,
                name="Openlist 移动任务监控"
            )
            self._scheduler.start()
            logger.info("Openlist Mover 任务监控服务已启动")
        except Exception as e:
            logger.error(f"启动 Openlist Mover 任务监控服务失败: {e}")

    def _check_move_tasks(self):
        """
        定期检查 Openlist 移动任务的状态
        """
        logger.debug("开始检查 Openlist 移动任务状态...")
        tasks_to_update = []
        tasks_to_keep = []
        
        with task_lock:
            # First, check tasks that are still running or waiting
            for task in self._move_tasks:
                if task['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]:
                    # Check timeout
                    if (datetime.now() - task['start_time']).total_seconds() > self._max_task_duration:
                        task['status'] = TASK_STATUS_FAILED
                        task['error'] = f"任务超时 ({int(self._max_task_duration / 60)} 分钟)"
                        tasks_to_update.append(task)
                        self._send_task_notification(task, "Openlist 移动超时", f"文件：{task['file']}\n源：{task['src_dir']}\n目标：{task['dst_dir']}\n错误：任务超时")
                        logger.error(f"Openlist 移动任务 {task['id']} 超时")
                        tasks_to_keep.append(task)
                        continue

                    # Check status
                    try:
                        task_info = self._call_openlist_task_api(task['id'])
                        
                        new_status = task_info.get('state') # state: 0-等待中, 1-进行中, 2-成功, 3-失败
                        error_msg = task_info.get('error')
                        
                        if new_status == TASK_STATUS_SUCCESS:
                            task['status'] = new_status
                            tasks_to_update.append(task)
                            self._send_task_notification(task, "Openlist 移动成功", f"文件：{task['file']}\n已移动到：{task['dst_dir']}")
                        elif new_status == TASK_STATUS_FAILED:
                            task['status'] = new_status
                            task['error'] = error_msg if error_msg else "Openlist 报告失败"
                            tasks_to_update.append(task)
                            self._send_task_notification(task, "Openlist 移动失败", f"文件：{task['file']}\n源：{task['src_dir']}\n目标：{task['dst_dir']}\n错误：{task['error']}")
                        elif new_status == TASK_STATUS_RUNNING:
                            task['status'] = new_status
                            
                    except Exception as e:
                        logger.error(f"查询 Openlist 任务 {task['id']} 状态失败: {e}")
                
                tasks_to_keep.append(task)
            
            self._move_tasks = tasks_to_keep
            
            logger.debug(f"Openlist Mover 任务检查完成，当前活跃任务数: {len([t for t in self._move_tasks if t['status'] in [TASK_STATUS_WAITING, TASK_STATUS_RUNNING]])}")


    def _send_task_notification(self, task: Dict[str, Any], title: str, text: str):
        """发送通知消息"""
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=title,
                text=text,
            )

    def _parse_path_mappings(self) -> Dict[str, Tuple[str, str]]:
        # ... (Same as original)
        """
        解析路径映射配置
        返回格式: {local_prefix: (openlist_src_prefix, openlist_dst_prefix)}
        """
        mappings = {}
        if not self._path_mappings:
            return mappings

        for line in self._path_mappings.split("\n"):
            line = line.strip()
            if not line or line.count(":") != 2:
                if line:
                    logger.warning(f"无效的路径映射格式: {line}")
                continue
            try:
                local_prefix, src_prefix, dst_prefix = line.split(":", 2)
                mappings[local_prefix.strip()] = (
                    src_prefix.strip(),
                    dst_prefix.strip(),
                )
            except ValueError:
                logger.warning(f"无效的路径映射格式: {line}")
        
        return mappings

    def _find_mapping(self, local_file_path: Path) -> Tuple[str, str, str, str]:
        # ... (Same as original)
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
        
        logger.info(f"开始处理新文件: {file_path}")
        
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
                    "error": ""
                }
                with task_lock:
                    self._move_tasks.append(new_task)
                    
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="Openlist 移动任务已启动",
                        text=f"文件：{name}\n源：{src_dir}\n目标：{dst_dir}\n任务ID: {task_id}",
                    )
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
        调用 Openlist API /api/fs/move
        返回任务ID (string) 或 None
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

            logger.info(f"调用 Openlist Move API: {api_url}")
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
                            # IMPORTANT: Assuming the response contains a task ID under 'tasks' key 
                            # similar to AList /api/fs/move, and that the original API only 
                            # moves one file, so the first task ID is sufficient.
                            tasks = response_data.get('data', {}).get('tasks')
                            if tasks and isinstance(tasks, list) and tasks[0].get('id'):
                                return str(tasks[0]['id'])
                            else:
                                # For Openlist implementations without task ID return on /api/fs/move
                                # We must assume success and return a simulated ID for monitoring
                                # For this context, we will treat it as a task-returning API
                                logger.warning("Openlist API 成功但未返回任务ID，模拟一个")
                                # This simulates a task ID return if the API is compliant with AList move task
                                # For the current context, to enable tracking, we must assume it returns a task list with IDs.
                                return f"sim_task_{int(time.time() * 1000)}" 
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
            
    # Simplified simulation for Openlist/AList task check API
    def _call_openlist_task_api(self, task_id: str) -> Dict[str, Any]:
        """
        调用 Openlist API 检查任务状态 (模拟 AList /api/admin/task/copy/info)
        返回: {'state': int, 'error': str}
        """
        
        api_url = f"{self._openlist_url}/api/admin/task/move/info?tid={task_id}" # Assuming a move task API exists
        
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
                        # state: 0-等待中, 1-进行中, 2-成功, 3-失败
                        state = task_info.get('state', TASK_STATUS_RUNNING)
                        error = task_info.get('error', '')
                        return {'state': state, 'error': error}
                    else:
                        logger.warning(f"Openlist Task API 报告失败: {response_data.get('message')} - {task_id}")
                        return {'state': TASK_STATUS_RUNNING, 'error': ''} # Keep running if API call succeeds but report is weird
                else:
                    logger.warning(f"Openlist Task API 返回非 200 状态码 {response_code}: {response_body}")
                    return {'state': TASK_STATUS_RUNNING, 'error': ''}

        except urllib.error.URLError as e:
            logger.error(f"Openlist Task API 调用失败 (URLError): {e}")
            # Assume it's still running if we can't connect, to avoid losing it
            return {'state': TASK_STATUS_RUNNING, 'error': ''} 
        except Exception as e:
            logger.error(f"调用 Openlist Task API 时出错: {e}")
            return {'state': TASK_STATUS_RUNNING, 'error': ''}
