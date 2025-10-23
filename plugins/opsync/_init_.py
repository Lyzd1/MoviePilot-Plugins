import os
import platform
import threading
import time
import traceback
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Tuple, Dict, Any
from urllib.parse import quote

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# --- 新增：视频文件扩展名 ---
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


class NewFileMonitorHandler(FileSystemEventHandler):
    """
    目录监控处理 - 仅处理文件创建和移动（移入）
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(NewFileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync  # sync 是 AlistMover 插件实例

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


class AlistMover(_PluginBase):
    # 插件名称
    plugin_name = "Openlist视频同步"
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
    plugin_config_prefix = "alistmover_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 1

    # private property
    _enabled = False
    _notify = False
    _alist_url = ""
    _alist_token = ""
    _monitor_paths = ""
    _path_mappings = ""
    _observer = []
    
    # {local_prefix: (alist_src_prefix, alist_dst_prefix)}
    _parsed_mappings: Dict[str, Tuple[str, str]] = {}

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
        logger.info("初始化 Alist 视频文件移动插件")

        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", False)
            self._alist_url = config.get("alist_url", "").rstrip('/')
            self._alist_token = config.get("alist_token", "")
            self._monitor_paths = config.get("monitor_paths", "")
            self._path_mappings = config.get("path_mappings", "")

        # 停止现有任务
        self.stop_service()

        if self._enabled:
            if not self._alist_url or not self._alist_token:
                logger.error("Alist Mover 已启用，但 Alist URL 或 Token 未配置！")
                self.systemmessage.put(
                    "Alist Mover 启动失败：Alist URL 或 Token 未配置",
                    title="Alist 视频文件移动",
                )
                return

            if not self._monitor_paths or not self._path_mappings:
                logger.error("Alist Mover 已启用，但监控目录或路径映射未配置！")
                self.systemmessage.put(
                    "Alist Mover 启动失败：监控目录或路径映射未配置",
                    title="Alist 视频文件移动",
                )
                return
                
            # 解析映射
            self._parsed_mappings = self._parse_path_mappings()
            if not self._parsed_mappings:
                logger.error("Alist Mover 路径映射配置无效")
                return
                
            logger.info(f"Alist Mover 已加载 {len(self._parsed_mappings)} 条路径映射")

            # 读取监控目录配置
            monitor_dirs = [
                d.strip() for d in self._monitor_paths.split("\n") if d.strip()
            ]
            logger.info(f"Alist Mover 本地监控目录：{monitor_dirs}")

            # 启动监控
            for mon_path in monitor_dirs:
                if not os.path.exists(mon_path):
                    logger.warning(f"Alist Mover 监控目录不存在：{mon_path}")
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
                    logger.info(f"Alist Mover {mon_path} 的监控服务启动")
                except Exception as e:
                    err_msg = str(e)
                    logger.error(f"{mon_path} 启动监控失败：{err_msg}")
                    self.systemmessage.put(
                        f"{mon_path} 启动监控失败：{err_msg}",
                        title="Alist 视频文件移动",
                    )
            
            logger.info("Alist 视频文件移动插件已启动")

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
                            "title": "Alist 视频文件移动",
                            "text": "本插件监控本地目录。当有新视频文件生成时，它会自动通过 Alist API 将其移动到指定的云盘目录。这要求 Alist 已经挂载了该本地目录作为“本地存储”。",
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
                    # Alist API 配置
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
                                            "title": "Alist API 配置",
                                            "text": "用于调用 Alist 移动文件 API。URL 必须包含 http/https 协议头。",
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
                                            "model": "alist_url",
                                            "label": "Alist URL",
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
                                            "model": "alist_token",
                                            "label": "Alist Token",
                                            "type": "password",
                                            "placeholder": "Alist 管理员 Token",
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
                                            "label": "路径映射规则 (本地:Alist源:Alist目标)",
                                            "rows": 6,
                                            "placeholder": "格式：本地监控目录:Alist源目录:Alist目标目录\n每行一条规则\n\n例如：\n/downloads/watch:/Local/watch:/YP/Video\n\n说明：\n当本地监控到 /downloads/watch/电影/S01/E01.mkv\nAlist 将会执行移动：\n源：/Local/watch/电影/S01/E01.mkv\n目标：/YP/Video/电影/S01/E01.mkv",
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
                            "text": "1. 插件监控 '本地监控目录' (例如 /downloads/watch)。\n2. '本地监控目录' 必须在 Alist 中被添加为 '本地存储'，其 Alist 路径对应 'Alist源目录' (例如 /Local/watch)。\n3. 当新文件 /downloads/watch/A/B.mkv 出现时，插件会查找映射规则。\n4. 插件命令 Alist 将 /Local/watch/A/B.mkv 移动到 /YP/Video/A/B.mkv ('Alist目标目录')。\n5. Alist 执行此移动操作，本地文件 /downloads/watch/A/B.mkv 将被移动（即消失）。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "alist_url": "",
            "alist_token": "",
            "monitor_paths": "",
            "path_mappings": "",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        logger.debug("开始停止 Alist Mover 服务")
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    logger.error(f"停止目录监控失败：{str(e)}")
        self._observer = []
        logger.debug("Alist Mover 服务停止完成")

    def _parse_path_mappings(self) -> Dict[str, Tuple[str, str]]:
        """
        解析路径映射配置
        返回格式: {local_prefix: (alist_src_prefix, alist_dst_prefix)}
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
        """
        根据本地文件路径查找 Alist 路径
        返回 (alist_src_dir, alist_dst_dir, file_name, error_msg)
        """
        local_file_str = str(local_file_path)
        file_name = local_file_path.name
        
        # 查找最匹配的（最长的）前缀
        best_match = ""
        for local_prefix in self._parsed_mappings.keys():
            if local_file_str.startswith(local_prefix):
                if len(local_prefix) > len(best_match):
                    best_match = local_prefix

        if not best_match:
            return None, None, None, f"文件 {local_file_str} 未找到匹配的路径映射规则"

        try:
            # 获取映射规则
            src_prefix, dst_prefix = self._parsed_mappings[best_match]
            
            # 计算相对路径
            # os.path.relpath 在 Windows 上会用 '\'，Alist 路径总是 '/'
            relative_path_str = local_file_str[len(best_match):].lstrip(os.path.sep)
            relative_dir_str = str(Path(relative_path_str).parent)

            # 替换为 Alist 路径分隔符
            relative_dir_str = relative_dir_str.replace(os.path.sep, '/')
            
            if relative_dir_str == ".":
                relative_dir_str = ""

            # 组合 Alist 路径
            # 确保路径以 / 开头，并且正确拼接
            alist_src_dir = '/' + '/'.join(
                [p for p in src_prefix.split('/') if p] + 
                [p for p in relative_dir_str.split('/') if p]
            )
            alist_dst_dir = '/' + '/'.join(
                [p for p in dst_prefix.split('/') if p] + 
                [p for p in relative_dir_str.split('/') if p]
            )
            
            return alist_src_dir, alist_dst_dir, file_name, None

        except Exception as e:
            logger.error(f"计算路径映射时出错: {e} - {traceback.format_exc()}")
            return None, None, None, f"计算路径映射时出错: {e}"

    def process_new_file(self, file_path: Path):
        """
        处理新文件（在线程中运行）
        """
        # 增加一个小的延迟，等待文件写入完成
        time.sleep(5) 
        
        try:
            logger.info(f"开始处理新文件: {file_path}")
            
            if not file_path.exists():
                logger.warning(f"文件 {file_path} 在处理前消失了，可能已被移动。")
                return

            # 1. 查找路径映射
            src_dir, dst_dir, name, error = self._find_mapping(file_path)
            
            if error:
                logger.error(f"处理失败: {error}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="Alist 移动失败",
                        text=f"文件：{file_path}\n错误：{error}",
                    )
                return

            # 2. 准备 Payload
            payload = {"src_dir": src_dir, "dst_dir": dst_dir, "names": [name]}
            
            logger.info(f"准备调用 Alist API 移动文件: {payload}")

            # 3. 调用 API
            if self._call_alist_move_api(payload):
                logger.info(f"成功移动文件: {name} 从 {src_dir} 到 {dst_dir}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="Alist 移动成功",
                        text=f"文件：{name}\n已移动到：{dst_dir}",
                    )
            else:
                logger.error(f"Alist API 移动失败: {name}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="Alist 移动失败",
                        text=f"文件：{name}\n源：{src_dir}\n目标：{dst_dir}\n请检查 Alist 日志。",
                    )
        except Exception as e:
            logger.error(f"处理文件 {file_path} 时发生意外错误: {e} - {traceback.format_exc()}")


    def _call_alist_move_api(self, payload: dict) -> bool:
        """
        调用 Alist API /api/fs/move
        """
        try:
            data = json.dumps(payload).encode("utf-8")
            api_url = f"{self._alist_url}/api/fs/move"

            headers = {
                "Content-Type": "application/json",
                "Authorization": self._alist_token,
                "User-Agent": "MoviePilot-AlistMover-Plugin",
            }

            req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

            logger.debug(f"Calling Alist Move API: {api_url} with payload: {payload}")

            with urllib.request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8")
                response_code = response.getcode()

                if response_code == 200:
                    response_data = json.loads(response_body)
                    if response_data.get("code") == 200:
                        logger.info(f"Alist API 成功移动: {payload.get('names')}")
                        return True
                    else:
                        logger.warning(f"Alist API 报告失败: {response_data.get('message')} (Payload: {payload})")
                        return False
                else:
                    logger.warning(f"Alist API 返回非 200 状态码 {response_code}: {response_body}")
                    return False

        except urllib.error.URLError as e:
            logger.error(f"Alist API 调用失败 (URLError): {e}")
            return False
        except Exception as e:
            logger.error(f"调用 Alist API 时出错: {e} - {traceback.format_exc()}")
            return False


