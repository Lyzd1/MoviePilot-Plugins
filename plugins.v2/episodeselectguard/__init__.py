"""
剧集选集守卫插件（EpisodeSelectGuard，MoviePilot V2）

针对「预订阅 / 试看」场景：当用户只对一部剧订阅了部分集数
（通过订阅的 start_episode ~ total_episode 表达，例如"只看前 3 集"），
而 MoviePilot 最终选中了覆盖更多集数的大包种子下载时，
本插件在 DownloadAdded 事件后接管 qBittorrent 下载任务：

1. 识别下载来源是否为订阅（source 前缀 Subscribe| 或能匹配到订阅记录）；
2. 解析目标订阅记录的集数范围 start_episode ~ total_episode，
   得到"本次应当下载的集"集合；
3. 读取 qBittorrent 种子文件清单，逐文件按文件名解析集号：
   - 集号完全不在目标范围内的文件 -> priority=0（不下载，保留在任务中便于恢复）；
   - 命中目标范围的视频文件保留；
4. 打上自定义标签（默认 Losing），标识"被选集过（任务不完整）"并防自动辅种；
5. 登记"选集状态"（含种子实际覆盖的集号全集），详情面板展示。

面板"补全下载"按钮按以下逻辑自动决策：
- 若该种子自身覆盖 TMDB 整季全集（种子文件解析出的集号 >= TMDB 整季集数范围）
  -> 直接"恢复整包"：全部文件 priority=1 并移除 Losing，用现有种子补齐全集；
- 若该种子本身就不是全集包（只是缺集/部分集）
  -> 自动"重新全集订阅"：不带 start/total 重新发起该剧原生订阅（MoviePilot 自动按全集搜索缺失集）。
决策前若记录的 TMDB 总集数缺失或 < 5（TMDB 对连载中剧集更新不及时），会重拉一次 TMDB 总集数再判断。

设计约束（源码核实，MoviePilot v2）：
- v2 下载链对"整季缺失 + 只设总集数"不会下发 episodes 给下载器做原生拆包，
  因此采用 DownloadAdded 事后纠正（暂停 -> 读文件 -> 剔除 -> 恢复）；
- 只处理订阅来源下载；手动搜索/辅种/刷流等非订阅下载不做改动；
- 事件 hash 即 qBittorrent infohash；
- 对多集合并单文件/无法解析集号的文件保守保留（避免漏下目标集），面板标记。
"""

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app import schemas
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.core.metainfo import MetaInfo
from app.db.subscribe_oper import SubscribeOper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType

_EPISODE_LOCK = threading.Lock()


class EpisodeSelectGuard(_PluginBase):
    # 插件名称
    plugin_name = "剧集选集守卫"
    # 插件描述
    plugin_desc = ("拦截订阅触发的电视剧下载，按订阅的集数范围对 qBittorrent 大包种子自动执行文件级选集"
                   "（仅下载订阅范围内的集，超范围文件剔除并打标防辅种）；详情面板展示当前处于选集状态的种子，"
                   "「补全下载」自动判断：种子覆盖 TMDB 全集则恢复整包，否则重新全集订阅。")
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "Lyzd1"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "episodeselectguard_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    # 日志前缀
    _LOG_TAG = "[剧集选集守卫] "

    # ---- 配置项 ----
    _enabled: bool = False
    # 仅处理订阅来源（默认 True）
    _only_subscribe: bool = True
    # 自定义标签，空表示不打标签
    _tag: str = "Losing"
    # 是否发送通知
    _notify: bool = False
    # 选集后是否自动恢复下载
    _auto_resume: bool = True
    # 事件延迟处理秒数（给 qBittorrent 任务元数据就绪留时间）
    _delay_seconds: int = 3

    # 运行时
    _downloader_helper: Optional[DownloaderHelper] = None
    _subscribe_oper: Optional[SubscribeOper] = None
    _subscribe_chain: Optional[SubscribeChain] = None
    _media_chain: Optional[MediaChain] = None

    # 锁与延迟任务登记
    _lock = _EPISODE_LOCK
    _delay_lock = threading.Lock()
    _delay_jobs: Dict[str, Dict[str, Any]] = {}

    # 数据键
    _SELECTED_KEY = "selected_records"

    # 视频文件后缀
    _VIDEO_SUFFIXES = {".mkv", ".mp4", ".ts", ".avi", ".rmvb", ".mov", ".wmv", ".flv", ".m2ts", ".iso"}

    # TMDB 总集数重拉阈值：小于该值的记录在补全前需重拉一次 TMDB
    _TMDB_REFRESH_THRESHOLD = 5

    def init_plugin(self, config: dict = None):
        """初始化配置，读取插件数据。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._only_subscribe = bool(config.get("only_subscribe", True))
        self._tag = str(config.get("tag") or "Losing").strip()
        self._notify = bool(config.get("notify"))
        self._auto_resume = bool(config.get("auto_resume", True))
        self._delay_seconds = max(0, min(30, int(config.get("delay_seconds") or 3)))

        self._downloader_helper = DownloaderHelper()
        self._subscribe_oper = SubscribeOper()
        self._subscribe_chain = SubscribeChain()
        self._media_chain = MediaChain()

        if not self._enabled:
            return
        logger.info(
            f"{self._LOG_TAG}插件已启用 | 仅订阅来源: {self._only_subscribe} | "
            f"标签: {self._tag or '无'} | 延迟: {self._delay_seconds}s | 自动恢复: {self._auto_resume}"
        )

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件详情面板使用的动作 API。"""
        return [
            {
                "path": "/complete",
                "endpoint": self.complete,
                "methods": ["GET"],
                "summary": "补全下载：种子覆盖 TMDB 全集则恢复整包，否则自动重新全集订阅",
            },
            {
                "path": "/restore_full",
                "endpoint": self.restore_full,
                "methods": ["GET"],
                "summary": "恢复整包下载（该种子全部文件 priority=1 并移除标签）",
            },
            {
                "path": "/resubscribe_full",
                "endpoint": self.resubscribe_full,
                "methods": ["GET"],
                "summary": "重新全集订阅（不带集数限制，按 TMDB 全集自动订阅）",
            },
            {
                "path": "/records",
                "endpoint": self.get_records,
                "methods": ["GET"],
                "summary": "查询选集状态记录",
            },
            {
                "path": "/clear_record",
                "endpoint": self.clear_record,
                "methods": ["GET"],
                "summary": "删除单条选集记录",
            },
        ]

    def get_form(self):
        """拼装插件配置页面。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "only_subscribe",
                                            "label": "仅处理订阅来源下载",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_resume",
                                            "label": "选集后自动恢复下载",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "tag",
                                            "label": "选集标签",
                                            "placeholder": "Losing",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay_seconds",
                                            "label": "处理延迟(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "处理完成发送通知",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "拦截「订阅」触发的电视剧下载：当订阅只设置了部分集数"
                                                   "（start_episode ~ total_episode，例如只看前 3 集），而选中的种子"
                                                   "是大包时，自动把不在订阅范围内的集对应的文件设为不下载，并打上"
                                                   "自定义标签（默认 Losing，防自动辅种）。单集单文件包可直接剔除；"
                                                   "多集合并单文件/无法识别集号的文件会保守保留并标记。面板「补全下载」"
                                                   "会自动判断：该种子覆盖 TMDB 全集则直接恢复整包补全；否则自动重新"
                                                   "全集订阅（不带集数限制）。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {
            "enabled": self._enabled,
            "only_subscribe": self._only_subscribe,
            "tag": self._tag,
            "auto_resume": self._auto_resume,
            "delay_seconds": self._delay_seconds,
            "notify": self._notify,
        }

    def get_page(self):
        """拼装插件详情页面：展示当前处于选集状态的种子。"""
        if not self._enabled:
            return [{"component": "div", "text": "插件未启用", "props": {"class": "text-center"}}]
        records = self.get_data(self._SELECTED_KEY) or []
        if not records:
            return [{"component": "div", "text": "暂无选集记录", "props": {"class": "text-center"}}]
        records = sorted(records, key=lambda x: x.get("time") or "", reverse=True)
        vrows = []
        for rec in records:
            title = rec.get("title") or rec.get("name") or ""
            season = rec.get("season")
            torrent = rec.get("torrent_name") or rec.get("hash") or ""
            downloader = rec.get("downloader") or ""
            start = rec.get("start_episode")
            total = rec.get("total_episode")
            range_text = f"第{season}季"
            if start and total:
                range_text += f" E{start}-E{total}"
            status = "已选集" if rec.get("selected") else "已恢复"
            kept = rec.get("kept_episodes") or []
            seed_eps = rec.get("seed_episodes") or []
            tmdb_total = rec.get("tmdb_total")
            excluded = rec.get("excluded_files") or 0
            # 判断种子覆盖与 TMDB 是否一致
            verdict = self._coverage_verdict(seed_eps, tmdb_total)
            text_lines = [
                f"{title} {range_text}",
                f"种子: {torrent}",
                f"下载器: {downloader}",
                f"状态: {status}",
            ]
            if kept:
                text_lines.append(f"保留集: {self._format_episodes(kept)}")
            if seed_eps:
                text_lines.append(f"种子实际覆盖: {self._format_episodes(seed_eps)}")
            if tmdb_total:
                text_lines.append(f"TMDB 整季: 共 {tmdb_total} 集")
            if excluded:
                text_lines.append(f"已剔除文件数: {excluded}")
            if rec.get("unresolved_files"):
                text_lines.append(f"⚠ 无法按集拆分的文件: {rec.get('unresolved_files')} 个（保守保留，需人工确认）")
            text_lines.append(f"判断: {verdict}")
            actions = [
                {
                    "component": "VBtn",
                    "props": {"size": "small", "color": "primary", "class": "mx-1"},
                    "text": "补全下载",
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/complete",
                            "method": "get",
                            "params": {
                                "hash": rec.get("hash"),
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                },
                {
                    "component": "VBtn",
                    "props": {"size": "small", "color": "secondary", "variant": "text", "class": "mx-1"},
                    "text": "恢复整包",
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/restore_full",
                            "method": "get",
                            "params": {
                                "hash": rec.get("hash"),
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                },
                {
                    "component": "VBtn",
                    "props": {"size": "small", "color": "secondary", "variant": "text", "class": "mx-1"},
                    "text": "重新全集订阅",
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/resubscribe_full",
                            "method": "get",
                            "params": {
                                "hash": rec.get("hash"),
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                },
                {
                    "component": "VBtn",
                    "props": {"size": "small", "color": "error", "variant": "text", "class": "mx-1"},
                    "text": "删除记录",
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/clear_record",
                            "method": "get",
                            "params": {
                                "hash": rec.get("hash"),
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                },
            ]
            vrows.append({
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-body-2 text-pre-wrap"},
                                        "text": "\n".join(text_lines),
                                    },
                                    {
                                        "component": "VCardActions",
                                        "content": actions,
                                    },
                                ],
                            }
                        ],
                    }
                ],
            })
        return [
            {"component": "div", "props": {"class": "text-subtitle-2 mb-2"},
             "text": f"共 {len(records)} 条选集状态记录（下载完成后记录仍保留，可手动删除或点击「补全下载」）"},
            *vrows,
        ]

    def stop_service(self):
        """停止服务：清理延迟任务表。"""
        with self._delay_lock:
            self._delay_jobs.clear()

    # ------------------------------------------------------------------ 事件

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event = None):
        """DownloadAdded 事件：识别订阅来源剧集下载，按订阅集数范围对大包种子做文件级选集。

        事件线程不执行阻塞 API 调用，登记后由新线程延迟处理。
        """
        if not self._enabled:
            return
        if not event:
            return
        event_data = event.event_data or {}
        if not isinstance(event_data, dict):
            try:
                event_data = dict(event_data)
            except Exception:
                return
        download_hash = str(event_data.get("hash") or "").strip()
        if not download_hash:
            return
        source = str(event_data.get("source") or "").strip()
        context = event_data.get("context")
        downloader = str(event_data.get("downloader") or "").strip()

        if self._only_subscribe:
            if not self._is_subscribe_source(source, context):
                logger.debug(f"{self._LOG_TAG}非订阅来源，跳过 {download_hash}（source={source}）")
                return

        with self._delay_lock:
            if download_hash in self._delay_jobs:
                return
            self._delay_jobs[download_hash] = {
                "hash": download_hash,
                "source": source,
                "context": context,
                "downloader": downloader,
                "ts": time.time(),
            }
        threading.Thread(target=self._delayed_process, args=(download_hash,), daemon=True).start()

    # ------------------------------------------------------------------ 处理

    def _delayed_process(self, download_hash: str):
        """延迟执行选集处理（新线程）。"""
        try:
            if self._delay_seconds > 0:
                time.sleep(self._delay_seconds)
            with self._delay_lock:
                job = self._delay_jobs.pop(download_hash, None)
            if not job:
                return
            self._process_download(job)
        except Exception as err:
            logger.error(f"{self._LOG_TAG}处理下载 {download_hash} 异常：{err}")

    def _is_subscribe_source(self, source: str, context: Any) -> bool:
        """判断下载是否为订阅来源：source 前缀 或 能匹配订阅记录。"""
        if source and str(source).startswith("Subscribe|"):
            return True
        return bool(self._find_subscribe(context))

    def _find_subscribe(self, context: Any):
        """从事件 context 解析媒体身份，返回匹配的订阅记录（无则 None）。"""
        try:
            if not context:
                return None
            media = getattr(context, "media_info", None)
            if media is None:
                return None
            mtype = getattr(media, "type", None)
            type_value = getattr(mtype, "value", mtype) if mtype is not None else None
            if type_value not in (MediaType.TV.value, "tv"):
                return None
            tmdbid = getattr(media, "tmdb_id", None)
            doubanid = getattr(media, "douban_id", None)
            season = getattr(media, "season", None)
            if season is None:
                meta = getattr(context, "meta_info", None)
                season = getattr(meta, "begin_season", None) if meta else None

            def _prefer_active(sub_list):
                """优先返回订阅中/新建状态记录，其次任意。"""
                if not sub_list:
                    return None
                active = [s for s in sub_list if getattr(s, "state", None) in ("R", "N")]
                return (active or sub_list)[0]

            if tmdbid:
                sub = _prefer_active(self._subscribe_oper.list_by_tmdbid(tmdbid=tmdbid, season=season))
                if sub:
                    return sub
                # season 不匹配时按 tmdbid 全局再找一次
                sub = _prefer_active(self._subscribe_oper.list_by_tmdbid(tmdbid=tmdbid))
                if sub:
                    return sub
            if doubanid:
                matched = []
                for s in self._subscribe_oper.list(state=None) or []:
                    if getattr(s, "doubanid", None) == str(doubanid):
                        matched.append(s)
                if matched:
                    # 优先季匹配，其次任意
                    exact = [s for s in matched if season is None or getattr(s, "season", None) == season]
                    return (exact or matched)[0]
            return None
        except Exception as err:
            logger.warning(f"{self._LOG_TAG}查找订阅记录失败：{err}")
            return None

    def _process_download(self, job: Dict[str, Any]):
        """执行选集处理主逻辑。"""
        download_hash = job.get("hash")
        context = job.get("context")
        downloader_name = job.get("downloader")

        subscribe = self._find_subscribe(context)
        if not subscribe:
            logger.debug(f"{self._LOG_TAG}未匹配到订阅记录，跳过 {download_hash}")
            return
        media, meta, season, tmdb_total = self._resolve_media_context(context)
        start_episode = int(getattr(subscribe, "start_episode", None) or 1)
        total_episode = int(getattr(subscribe, "total_episode", None) or 0)
        if not total_episode or total_episode < start_episode:
            logger.debug(f"{self._LOG_TAG}订阅 {subscribe.name} 未设置有效集数范围，跳过")
            return
        # 仅拦截“部分集数”订阅：start>1 或订阅范围小于 TMDB 当前整季。
        # 全集订阅（start=1 且 total>=TMDB 整季）交给 MoviePilot 原生处理，
        # 避免本插件误剔除连载剧随 TMDB 总集数增长后仍需下载的集。
        if start_episode <= 1 and (not tmdb_total or total_episode >= tmdb_total):
            logger.debug(
                f"{self._LOG_TAG}订阅 {subscribe.name} 第{season}季为全集订阅"
                f"（E{start_episode}-E{total_episode} >= TMDB 共 {tmdb_total} 集），交给 MoviePilot 原生，跳过"
            )
            return
        target_episodes = set(range(start_episode, total_episode + 1))
        logger.info(
            f"{self._LOG_TAG}订阅 {subscribe.name} 第{season}季 目标集范围 E{start_episode}-E{total_episode}"
            f"（TMDB 整季共 {tmdb_total} 集），检查下载 {download_hash} ..."
        )

        downloader = self._get_qbittorrent(downloader_name)
        if not downloader:
            logger.error(f"{self._LOG_TAG}未找到可用 qBittorrent 下载器（{downloader_name or '默认'}），跳过")
            return

        try:
            torrent_files = downloader.get_files(download_hash)
        except Exception as err:
            logger.error(f"{self._LOG_TAG}读取种子文件失败 {download_hash}: {err}")
            return
        if torrent_files is None:
            logger.warning(f"{self._LOG_TAG}种子 {download_hash} 文件清单为空，跳过")
            return

        # 解析每个视频文件的集号与当前 priority
        file_decisions = []  # (file_id, name, episodes, priority)
        excluded_ids = []
        kept_episodes: Set[int] = set()
        seed_episodes: Set[int] = set()
        excluded_episodes: Set[int] = set()
        unresolved_files = 0
        for torrent_file in torrent_files or []:
            try:
                file_id = torrent_file.get("id")
                file_name = str(torrent_file.get("name") or "")
                priority = torrent_file.get("priority")
                if file_id is None or not file_name:
                    continue
                suffix = Path(file_name).suffix.lower()
                if suffix not in self._VIDEO_SUFFIXES:
                    continue
                meta_info = MetaInfo(Path(file_name).stem)
                episodes = set(meta_info.episode_list or [])
                if episodes:
                    file_decisions.append((file_id, file_name, episodes, priority))
                    seed_episodes |= episodes
                else:
                    unresolved_files += 1
            except Exception:
                continue

        currently_selected = any((p == 0) for _, _, _, p in file_decisions)

        # 需剔除的文件 = 有集号、完全不在目标范围、且当前 priority != 0
        for file_id, file_name, episodes, priority in file_decisions:
            in_target = bool(episodes & target_episodes)
            if not in_target:
                if priority != 0:
                    excluded_ids.append(file_id)
                    excluded_episodes |= episodes
            else:
                kept_episodes |= (episodes & target_episodes)

        if not file_decisions and unresolved_files == 0:
            logger.info(f"{self._LOG_TAG}种子 {download_hash} 没有可解析的视频文件，跳过")
            return

        if not excluded_ids:
            logger.info(
                f"{self._LOG_TAG}种子 {download_hash} 无需剔除（无超范围文件），"
                f"保留集：{self._format_episodes(sorted(kept_episodes)) or '全部'}"
            )
            if currently_selected:
                self._save_record(download_hash, subscribe, media, meta, season, downloader_name,
                                  target_episodes, kept_episodes, seed_episodes, excluded_episodes,
                                  excluded=0, selected=True, tmdb_total=tmdb_total,
                                  unresolved=unresolved_files)
            return

        logger.info(
            f"{self._LOG_TAG}种子 {download_hash} 需要剔除 {len(excluded_ids)} 个超范围文件"
            f"（保留集：{self._format_episodes(sorted(kept_episodes)) or '无'}）"
        )
        try:
            try:
                downloader.stop_torrents(download_hash)
            except Exception:
                pass
            if excluded_ids:
                downloader.set_files(torrent_hash=download_hash, file_ids=excluded_ids, priority=0)
            if self._tag:
                try:
                    downloader.set_torrents_tag([download_hash], [self._tag])
                except Exception as err:
                    logger.warning(f"{self._LOG_TAG}打标签失败：{err}")
            if self._auto_resume:
                try:
                    downloader.start_torrents(download_hash)
                except Exception as err:
                    logger.warning(f"{self._LOG_TAG}恢复下载失败：{err}")
        except Exception as err:
            logger.error(f"{self._LOG_TAG}执行选集失败 {download_hash}: {err}")
            return

        self._save_record(download_hash, subscribe, media, meta, season, downloader_name,
                          target_episodes, kept_episodes, seed_episodes, excluded_episodes,
                          excluded=len(excluded_ids), selected=True, tmdb_total=tmdb_total,
                          unresolved=unresolved_files)
        logger.info(
            f"{self._LOG_TAG}完成选集：{subscribe.name} 第{season}季，种子 {download_hash}，"
            f"剔除 {len(excluded_ids)} 个超范围文件，保留 {self._format_episodes(sorted(kept_episodes)) or '全部'}"
        )
        if self._notify:
            try:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title=f"剧集选集守卫 - {subscribe.name}",
                    text=f"第{season}季订阅范围 E{start_episode}-E{total_episode}\n"
                         f"已剔除 {len(excluded_ids)} 个超范围文件\n"
                         f"保留集：{self._format_episodes(sorted(kept_episodes)) or '全部'}",
                )
            except Exception:
                pass

    def _resolve_media_context(self, context: Any):
        """从事件 context 解析 media/meta/season/tmdb_total。"""
        media = getattr(context, "media_info", None) if context else None
        meta = getattr(context, "meta_info", None) if context else None
        season = None
        if media is not None:
            season = getattr(media, "season", None)
        if season is None and meta is not None:
            season = getattr(meta, "begin_season", None) or getattr(meta, "season", None)
        if season is None and media is not None:
            seasons = getattr(media, "seasons", None) or {}
            if seasons:
                season = min(seasons.keys())
        tmdb_total = 0
        if media is not None:
            seasons = getattr(media, "seasons", None) or {}
            if season is not None and seasons:
                tmdb_total = len(seasons.get(season) or [])
            elif seasons:
                tmdb_total = len(next(iter(seasons.values())) or [])
        return media, meta, season, tmdb_total

    def _get_qbittorrent(self, name: Optional[str] = None):
        """获取 qBittorrent 下载器 raw 实例（DownloaderHelper -> ServiceInfo.instance）。"""
        try:
            helper = self._downloader_helper or DownloaderHelper()
            if name:
                service = helper.get_service(name=name)
                if service and helper.is_downloader(service_type="qbittorrent", service=service):
                    instance = service.instance
                    if instance and not instance.is_inactive():
                        return instance
                return None
            services = helper.get_services()
            for _, service in (services or {}).items():
                if helper.is_downloader(service_type="qbittorrent", service=service):
                    instance = service.instance
                    if instance and not instance.is_inactive():
                        return instance
            return None
        except Exception as err:
            logger.error(f"{self._LOG_TAG}获取下载器失败：{err}")
            return None

    # ------------------------------------------------------------------ 记录

    def _save_record(self, download_hash: str, subscribe: Any, media: Any, meta: Any,
                     season: Optional[int], downloader: Optional[str],
                     target_episodes: Set[int], kept_episodes: Set[int], seed_episodes: Set[int],
                     excluded_episodes: Set[int], excluded: int, selected: bool,
                     tmdb_total: int, unresolved: int = 0):
        """保存/更新选集状态记录。"""
        try:
            records = self.get_data(self._SELECTED_KEY) or []
            name = getattr(media, "title", None) or getattr(subscribe, "name", None) or ""
            year = getattr(media, "year", None)
            torrent_name = ""
            if meta is not None:
                torrent_name = getattr(meta, "org_string", "") or ""
            record = {
                "hash": download_hash,
                "title": f"{name} ({year})" if year else name,
                "year": str(year or ""),
                "season": season,
                "tmdbid": getattr(media, "tmdb_id", None) or getattr(subscribe, "tmdbid", None),
                "subscribe_id": getattr(subscribe, "id", None),
                "start_episode": min(target_episodes) if target_episodes else None,
                "total_episode": max(target_episodes) if target_episodes else None,
                "downloader": downloader,
                "torrent_name": torrent_name,
                "kept_episodes": sorted(kept_episodes) if kept_episodes else [],
                "seed_episodes": sorted(seed_episodes) if seed_episodes else [],
                "excluded_episodes": sorted(excluded_episodes) if excluded_episodes else [],
                "excluded_files": excluded,
                "tmdb_total": tmdb_total or 0,
                "unresolved_files": unresolved,
                "selected": selected,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            }
            records = [r for r in records if r.get("hash") != download_hash]
            records.append(record)
            records = records[-200:]
            self.save_data(self._SELECTED_KEY, records)
        except Exception as err:
            logger.error(f"{self._LOG_TAG}保存选集记录失败：{err}")

    @staticmethod
    def _format_episodes(episodes) -> str:
        """将集号列表格式化为易读的压缩范围字符串，如 '1-3, 7'。"""
        if not episodes:
            return ""
        episodes = sorted(set(int(e) for e in episodes))
        ranges = []
        start = prev = episodes[0]
        for ep in episodes[1:]:
            if ep == prev + 1:
                prev = ep
                continue
            ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = ep
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        return ", ".join(ranges)

    # ------------------------------------------------------------------ 补全决策

    def _coverage_verdict(self, seed_episodes, tmdb_total) -> str:
        """返回种子的覆盖判断文案。"""
        seed_eps = set(int(e) for e in (seed_episodes or []))
        try:
            total = int(tmdb_total or 0)
        except (TypeError, ValueError):
            total = 0
        if not seed_eps:
            return "种子集号无法解析，无法判断（建议重新全集订阅）"
        if total <= 0:
            return "未取得 TMDB 总集数，无法判断（建议重新全集订阅）"
        full_target = set(range(1, total + 1))
        if full_target.issubset(seed_eps):
            return f"种子覆盖全集（含 E{total}）→ 点「补全下载」将直接恢复整包"
        return (f"种子未覆盖全集（最大到 E{max(seed_eps)}，TMDB 共 {total} 集）"
                f"→ 点「补全下载」将自动重新全集订阅")

    def _resolve_tmdb_total(self, rec: Dict[str, Any]) -> int:
        """解析记录对应剧集整季总集数。

        若记录中 TMDB 总集数缺失或 < 阈值（连载剧 TMDB 更新不及时），主动重拉一次 TMDB。
        """
        try:
            stored = int(rec.get("tmdb_total") or 0)
        except (TypeError, ValueError):
            stored = 0
        if stored >= self._TMDB_REFRESH_THRESHOLD:
            return stored
        tmdbid = rec.get("tmdbid")
        season = rec.get("season")
        if not tmdbid:
            return stored
        try:
            title = (rec.get("title") or "").split(" (")[0]
            meta = MetaInfo(title)
            mediainfo = self._media_chain.recognize_media(
                meta=meta,
                mtype=MediaType.TV,
                tmdbid=int(tmdbid),
                cache=False,
            )
            if mediainfo and getattr(mediainfo, "seasons", None):
                if season is not None and season in mediainfo.seasons:
                    total = len(mediainfo.seasons[season] or [])
                else:
                    total = len(next(iter(mediainfo.seasons.values())) or [])
                if total:
                    logger.info(f"{self._LOG_TAG}重拉 TMDB 总集数：{rec.get('title')} 第{season}季 = {total} 集")
                    rec["tmdb_total"] = total
                    return total
        except Exception as err:
            logger.warning(f"{self._LOG_TAG}重拉 TMDB 总集数失败：{err}")
        return stored

    # ------------------------------------------------------------------ API

    def _check_api(self, apikey: Optional[str]):
        """校验插件 API 密钥。"""
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        return None

    def _update_record(self, hash: str, updater):
        """读取记录列表，对匹配记录执行 updater 后写回。"""
        records = self.get_data(self._SELECTED_KEY) or []
        changed = False
        for i, r in enumerate(records):
            if r.get("hash") == hash:
                updater(records[i])
                changed = True
        if changed:
            self.save_data(self._SELECTED_KEY, records)
        return records

    def get_records(self, apikey: str):
        """查询选集状态记录。"""
        err = self._check_api(apikey)
        if err:
            return err
        records = self.get_data(self._SELECTED_KEY) or []
        records = sorted(records, key=lambda x: x.get("time") or "", reverse=True)
        return schemas.Response(success=True, data=records)

    def clear_record(self, hash: str, apikey: str):
        """删除单条选集记录。"""
        err = self._check_api(apikey)
        if err:
            return err
        if not hash:
            return schemas.Response(success=False, message="缺少 hash 参数")
        records = self.get_data(self._SELECTED_KEY) or []
        new_records = [r for r in records if r.get("hash") != hash]
        if len(new_records) == len(records):
            return schemas.Response(success=False, message="未找到记录")
        self.save_data(self._SELECTED_KEY, new_records)
        return schemas.Response(success=True, message="记录已删除")

    def _do_restore_full(self, rec: Dict[str, Any]) -> Tuple[bool, str]:
        """恢复整包的具体实现：全部文件 priority=1 并移除标签。"""
        hash_val = rec.get("hash")
        downloader = self._get_qbittorrent(rec.get("downloader") or None)
        if not downloader:
            return False, "未找到可用 qBittorrent 下载器"
        try:
            torrent_files = downloader.get_files(hash_val)
            if torrent_files is None:
                return False, "读取种子文件失败"
            restore_ids = [f.get("id") for f in torrent_files
                           if f.get("id") is not None and int(f.get("priority") or 0) == 0]
            if restore_ids:
                downloader.set_files(torrent_hash=hash_val, file_ids=restore_ids, priority=1)
            if self._tag:
                try:
                    downloader.remove_torrents_tag(hash_val, [self._tag])
                except Exception:
                    pass
            try:
                downloader.start_torrents(hash_val)
            except Exception:
                pass
        except Exception as err:
            logger.error(f"{self._LOG_TAG}恢复整包失败 {hash_val}: {err}")
            return False, f"恢复失败：{err}"
        rec["selected"] = False
        rec["restore_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        return True, "已恢复整包下载"

    def restore_full(self, hash: str, apikey: str):
        """恢复整包：该种子全部文件 priority=1 并移除标签。"""
        err = self._check_api(apikey)
        if err:
            return err
        if not hash:
            return schemas.Response(success=False, message="缺少 hash 参数")
        records = self.get_data(self._SELECTED_KEY) or []
        rec = next((r for r in records if r.get("hash") == hash), None)
        if not rec:
            return schemas.Response(success=False, message="未找到该种子的选集记录")
        ok, msg = self._do_restore_full(rec)
        if not ok:
            return schemas.Response(success=False, message=msg)
        self._update_record(hash, lambda r: r.update(rec))
        return schemas.Response(success=True, message=msg)

    def _do_resubscribe_full(self, rec: Dict[str, Any]) -> Tuple[bool, str]:
        """重新全集订阅：不带 start/total，按 TMDB 全集原生订阅该剧（季）。"""
        tmdbid = rec.get("tmdbid")
        season = rec.get("season")
        title = (rec.get("title") or "").split(" (")[0]
        year = rec.get("year") or ""
        if not tmdbid or not title:
            return False, "记录缺少 tmdbid/标题，无法重新订阅"
        try:
            sid, err_msg = self._subscribe_chain.add(
                title=title,
                year=str(year),
                mtype=MediaType.TV,
                tmdbid=int(tmdbid),
                season=season,
                source="Subscribe",
                username="EpisodeSelectGuard",
                message=False,
            )
        except Exception as err:
            logger.error(f"{self._LOG_TAG}重新订阅失败：{err}")
            return False, f"重新订阅失败：{err}"
        if not sid:
            return False, f"重新订阅失败：{err_msg or '未知错误'}"
        # 新订阅刚创建不足 1 分钟时主流程 search 会跳过，延迟到创建时间超过 1 分钟后再触发
        threading.Thread(target=self._run_subscribe_search, args=(sid,), daemon=True).start()
        return True, f"已重新发起全集订阅（订阅ID {sid}，1 分钟后自动搜索）"

    def resubscribe_full(self, hash: str, apikey: str):
        """重新全集订阅：按 TMDB 全集重新发起该剧（季）的完整订阅。"""
        err = self._check_api(apikey)
        if err:
            return err
        if not hash:
            return schemas.Response(success=False, message="缺少 hash 参数")
        records = self.get_data(self._SELECTED_KEY) or []
        rec = next((r for r in records if r.get("hash") == hash), None)
        if not rec:
            return schemas.Response(success=False, message="未找到该种子的选集记录")
        ok, msg = self._do_resubscribe_full(rec)
        if not ok:
            return schemas.Response(success=False, message=msg)
        return schemas.Response(success=True, message=msg)

    def complete(self, hash: str, apikey: str):
        """补全下载（智能决策按钮）。

        1. 解析 TMDB 整季总集数（缺失/<阈值时重拉 TMDB）；
        2. 若该种子自身覆盖 TMDB 全集 -> 恢复整包（现有种子补全）；
        3. 否则 -> 自动重新全集订阅（MoviePilot 原生补搜缺集）。
        """
        err = self._check_api(apikey)
        if err:
            return err
        if not hash:
            return schemas.Response(success=False, message="缺少 hash 参数")
        records = self.get_data(self._SELECTED_KEY) or []
        rec = next((r for r in records if r.get("hash") == hash), None)
        if not rec:
            return schemas.Response(success=False, message="未找到该种子的选集记录")
        total = self._resolve_tmdb_total(rec)
        self._update_record(hash, lambda r: r.update({"tmdb_total": total}))
        seed_eps = set(int(e) for e in (rec.get("seed_episodes") or []))
        full_target = set(range(1, total + 1)) if total > 0 else set()
        if seed_eps and full_target and full_target.issubset(seed_eps):
            # 种子覆盖全集 -> 恢复整包
            ok, msg = self._do_restore_full(rec)
            if not ok:
                return schemas.Response(success=False, message=msg)
            self._update_record(hash, lambda r: r.update(rec))
            return schemas.Response(success=True, message=f"种子覆盖全集，{msg}")
        # 种子不是全集包 -> 重新全集订阅
        ok, msg = self._do_resubscribe_full(rec)
        if not ok:
            return schemas.Response(success=False, message=msg)
        return schemas.Response(success=True, message=msg)

    def _run_subscribe_search(self, sid: int):
        """后台触发指定订阅立即搜索。

        主流程 search() 会跳过创建不足 1 分钟的订阅，这里等待 70 秒后手动搜索，
        确保新订阅创建时间已超过 1 分钟门槛、能立即进入搜索下载缺集。
        """
        try:
            time.sleep(70)
            self._subscribe_chain.search(sid=sid, state="N", manual=True)
        except Exception as err:
            logger.warning(f"{self._LOG_TAG}触发订阅搜索失败：{err}")
