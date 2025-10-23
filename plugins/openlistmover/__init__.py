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
    
    # {local_prefix: (openlist_src_prefix, openlist_dst_prefix)}
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
        pass

    def stop_service(self):
        """
        退出插件
        """
        logger.debug("开始停止 Openlist Mover 服务")
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    logger.error(f"停止目录监控失败：{str(e)}")
        self._observer = []
        logger.debug("Openlist Mover 服务停止完成")

    def _parse_path_mappings(self) -> Dict[str, Tuple[str, str]]:
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
            if self._call_openlist_move_api(payload):
                logger.info(f"成功移动文件: {name} 从 {src_dir} 到 {dst_dir}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="Openlist 移动成功",
                        text=f"文件：{name}\n已移动到：{dst_dir}",
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

    def _call_openlist_move_api(self, payload: dict) -> bool:
        """
        调用 Openlist API /api/fs/move
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
                            logger.info(f"Openlist API 成功移动: {payload.get('names')}")
                            return True
                        else:
                            error_msg = response_data.get('message', '未知错误')
                            logger.warning(f"Openlist API 报告失败: {error_msg} (Payload: {payload})")
                            return False
                    except json.JSONDecodeError:
                        logger.error(f"Openlist API 响应JSON解析失败: {response_body}")
                        return False
                else:
                    logger.warning(f"Openlist API 返回非 200 状态码 {response_code}: {response_body}")
                    return False

        except urllib.error.URLError as e:
            logger.error(f"Openlist API 调用失败 (URLError): {e}")
            return False
        except Exception as e:
            logger.error(f"调用 Openlist API 时出错: {e} - {traceback.format_exc()}")
            return False