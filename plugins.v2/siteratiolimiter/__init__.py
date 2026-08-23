"""
站点分享率上传限速插件（MoviePilot V2）

基于「站点账号分享率」与配置的分享率下限/上限，带滞回地管理 qBittorrent 种子的上传限速。

档位与滞回规则（防止限速波动）：
1. 分享率 <= 下限 -> 🔻 低于下限：取消该站所有种子的上传速度上限；
   此后保持不限速，直到分享率 > 上限（而不是一到下限之上就立刻限速）；
2. 分享率 > 上限 -> 🔺 达到上限：将该站未限速的种子批量设置上传限速；
   此后保持限速，直到分享率 <= 下限（而不是一降到上限之下就立刻取消限速）；
3. 处于（下限, 上限] 中间区间 -> ⏸ 中间区间：不修改档位状态、不做任何限速调整
   （从哪边进入就维持哪边的状态，防止档位抖动导致的频繁限速/取消限速）。

功能：
1. 全局设置分享率下限/上限，并可对每个站点单独设置（未配置的站点回退全局值）；
2. 插件内部维护每个站点的档位状态，由「站点数据统计」插件的分享率刷新事件
   （SiteRefreshed，全站刷新完成 site_id == *）更新，启用插件时做一次静默基线；
3. 监听下载种子事件（DownloadAdded）：直接从插件内部维护的站点状态读取当前档位，
   仅当站点档位为「达到上限」时立即限制该种子上传速度（KB/s），否则不做任何操作
   （不在下载时实时查询 MoviePilot 站点数据，数据来源为插件维护的站点状态）；
4. 站点分享率刷新事件：
   - 档位变化到「低于下限」 -> 取消该站种子的上传速度上限；
   - 档位变化到「达到上限」 -> 将该站当前未限速的种子批量设置上传限速；
   - 档位在中间区间 -> 保持现状，不调用限速接口；
   - 保存配置时：仅当修改了上传限速大小、或修改阈值导致档位变化、或站点/下载器范围变化时
     才调用接口调整限速，否则只刷新状态与统计，不产生限速 API 调用；
5. 定时扫描下载器新增种子（与辅种定时任务相同的 cron 调度体系）：
   - 按配置的执行周期（cron 表达式）自动扫描所选下载器，与上次扫描快照对比
     找出「新增种子」（涵盖手动添加、其他工具辅种等非 MoviePilot 下载事件来源）；
   - 新增种子识别到所属站点且该站档位为「达到上限」且未限速 -> 按现有规则自动限速；
     已被限速的种子登记跳过、档位未达上限或不在筛选范围的种子跳过；
   - 无法识别站点的种子写入 warning 日志（种子名 / hash / tracker / 标签 / 分类），
     用于排查「同一站点部分种子识别不到」的问题；汇总结果记录在日志，不发送通知；
   - 支持「立即扫描一次」按钮（保存配置后执行，随后自动复位）；
6. 详情面板：展示每个配置站点的账号分享率、阈值（下限-上限）与档位状态（供下载时判定），
   以及该站已限速种子数，不再展示种子级明细；
7. 取消限速仅发生在运行中「分享率 <= 下限」档位：停用/卸载插件、移除下载器、
   升级/热重载等场景均不修改任何种子的上传限速（保持插件运行期设置的值）。
8. 新增种子自动重新宣告（可选）：下载事件与定时扫描发现的新增种子共用同一全局去重列表
   （跨会话持久化，同一种子只宣告一次），仅对「低于下限（不限速）」站点的种子在
   「全局总宣告种子数」批次限额内自动通过 qBittorrent 接口重新宣告
   （首次延迟后按间隔重复宣告指定次数），详情面板展示当前宣告种子与宣告进度。
"""

import datetime
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.downloader import DownloaderHelper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType


class SiteRatioLimiter(_PluginBase):
    """
    站点分享率上传限速插件。

    以「站点账号分享率」与配置的下限/上限为决策依据，带滞回地管理
    qBittorrent 种子的上传限速：低于下限取消限速、达到上限恢复限速、
    中间区间保持现状，并在详情面板展示每个站点的分享率与档位状态。
    """

    # 插件名称
    plugin_name = "站点分享率上传限速"
    # 插件描述
    plugin_desc = "基于站点账号分享率与分享率上下限自动管理 qBittorrent 种子上传限速：低于下限取消限速并保持到达到上限，达到上限恢复限速并保持到低于下限，中间区间保持现状防波动；下载种子时按插件维护的站点档位状态限速；支持定时扫描下载器新增种子（手动添加/辅种等非 MoviePilot 下载事件来源），识别站点并按档位自动限速，未识别种子记录日志；可选新增种子自动重新宣告（仅对不限速站点、全局批次限额、下载事件与扫描共用去重列表，详情面板展示宣告种子与进度）。"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.7.0"
    # 插件作者
    plugin_author = "Lyzd1"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "siteratiolimiter_"
    # 加载顺序
    plugin_order = 31
    # 可使用的用户级别
    auth_level = 1

    LOG_TAG = "[站点分享率上传限速] "

    # ---- 站点档位状态（滞回三档 + 无数据） ----
    _STATE_LOW = "low"          # 分享率 <= 下限：取消限速，并保持到分享率 > 上限
    _STATE_NORMAL = "normal"    # 中间区间（下限, 上限]：保持现状，不调整
    _STATE_HIGH = "high"        # 分享率 > 上限：限速，并保持到分享率 <= 下限
    _STATE_UNKNOWN = "unknown"  # 无分享率数据

    _STATE_LABELS = {
        "low": "🔻 不限速保持（曾低于下限）",
        "normal": "⏸ 中间区间（保持现状）",
        "high": "🔺 限速保持（达到上限）",
        "unknown": "⚪ 无数据",
    }

    # ---- 配置项默认值 ----
    _enabled = False
    # 已选择的下载器名称（仅 qBittorrent）
    _downloaders: List[str] = []
    # 已选择的站点名称，为空表示管理所有活动站点
    _sites: List[str] = []
    # 全局分享率下限/上限（正数，最多 1 位小数，上限需 >= 下限）
    _ratio_lower = 1.0
    _ratio_upper = 5.0
    # 按站点单独阈值文本（每行「站点=下限,上限」）与解析结果 {站名小写: (下限, 上限)}
    _site_conf_text = ""
    _site_confs: Dict[str, Tuple[float, float]] = {}
    # 上传速度 KB/s，0 表示不做限速处理
    _upload_limit = 2000
    # 定时扫描下载器新增种子：启用开关 / 执行周期（cron 表达式，与辅种定时任务保持一致）/ 立即扫描一次
    _scan_enable = False
    _scan_cron = ""
    _scan_onlyonce = False
    # 新增种子自动重新宣告（qBittorrent reannounce）：启用开关 / 全局总宣告种子数（同一批次时间窗口内最多宣告的种子数）
    _reannounce_enable = False
    _reannounce_limit = 5
    # 每个种子重复宣告次数 / 宣告间隔（秒）/ 首次宣告延迟（秒）
    _reannounce_times = 15
    _reannounce_interval = 330
    _reannounce_delay = 180

    # ---- 运行时状态 ----
    _scan_scheduler = None
    # 上次扫描时各下载器的种子 hash 快照 {下载器名称: {种子Hash}}，跨会话持久化
    _scanned_hashes: Dict[str, Set[str]] = {}
    # 新增种子宣告调度器与批次窗口计数（同一时间窗口内仅宣告前 N 个新种子）
    _reannounce_scheduler = None
    _reannounce_batch_ts = 0.0
    _reannounce_batch_count = 0
    _REANNOUNCE_BATCH_WINDOW = 10.0   # 秒
    # 新增种子宣告登记列表 {下载器: {种子Hash}}：下载事件与扫描共用（跨会话持久化，去重：同一种子只登记一次）
    _reannounce_hashes: Dict[str, Set[str]] = {}
    # 宣告进行中进度 {下载器: {种子Hash: {"name","site","total","done","next_ts"}}}，跨会话持久化
    _reannounce_progress: Dict[str, Dict[str, Dict[str, Any]]] = {}
    # 站点域名(小写) -> 站点名称；站点名称(小写) -> 站点名称
    _site_domains: Dict[str, str] = {}
    _site_names: Dict[str, str] = {}
    # 站点账号分享率最新快照 {站点名称: 分享率} 与数据时间 {站点名称: "日期 时间"}
    # 数据来源：站点数据统计插件写入的 SiteUserData（与流量管理/魔力兑换一致）
    _site_ratios: Dict[str, float] = {}
    _site_ratio_times: Dict[str, str] = {}
    # 站点档位状态 {站点名称: low/normal/unknown}，跨会话持久化
    _site_states: Dict[str, str] = {}
    # 站点统计（页面展示 + 下载时判定依据）{站点名称: {ratio, lower, upper, state, updated, limited, total}}
    _site_stats: Dict[str, Dict[str, Any]] = {}
    # 本插件本轮/会话内限速的种子 {下载器名称: {种子Hash}}（防抖动登记与统计）
    _limited_hashes: Dict[str, Set[str]] = {}

    # 持久化数据键
    _SITE_STATES_KEY = "site_states"
    _SIG_KEY = "config_signature"
    _SCANNED_HASHES_KEY = "scanned_hashes"
    _REANNOUNCE_HASHES_KEY = "reannounce_hashes"
    _REANNOUNCE_PROGRESS_KEY = "reannounce_progress"

    # ---------------------------------------------------------------- 生命周期

    def init_plugin(self, config: dict = None):
        """
        读取配置、构建站点映射、加载持久化状态并在启用时做一次静默基线。

        保存配置时的限速动作控制（避免无意义的限速 API 调用）：
        - 仅当「首次启用 / 上传限速大小变化 / 分享率下限阈值变化 / 下载器或站点范围变化」之一发生时，
          按新档位调用接口调整限速；
        - 否则只刷新站点分享率、档位状态与统计（面板数据），不调用任何限速接口。
        """
        was_enabled = self._enabled
        self._stop_scan_scheduler()
        self._stop_reannounce_scheduler()

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._downloaders = self._normalize_config_list(config.get("downloaders"))
        self._sites = self._normalize_config_list(config.get("sites"))
        self._ratio_lower = self._to_ratio(config.get("ratio_lower_limit"), 1.0)
        self._ratio_upper = self._to_ratio(config.get("ratio_upper_limit"), 5.0)
        if self._ratio_upper < self._ratio_lower:
            logger.warning(f"{self.LOG_TAG}分享率上限小于下限，已重置为默认值（下限 1.0 / 上限 5.0）")
            self._ratio_lower, self._ratio_upper = 1.0, 5.0
        self._site_confs, self._site_conf_text = self._normalize_site_confs(config.get("site_confs"))
        self._upload_limit = max(self._to_int(config.get("upload_limit"), 2000), 0)
        self._scan_enable = bool(config.get("scan_enable"))
        self._scan_cron = str(config.get("scan_cron") or "").strip()
        self._scan_onlyonce = bool(config.get("scan_onlyonce"))
        self._reannounce_enable = bool(config.get("reannounce_enable"))
        self._reannounce_limit = max(self._to_int(config.get("reannounce_limit"), 5), 1)
        self._reannounce_times = max(self._to_int(config.get("reannounce_times"), 15), 1)
        self._reannounce_interval = max(self._to_int(config.get("reannounce_interval"), 330), 10)
        self._reannounce_delay = max(self._to_int(config.get("reannounce_delay"), 180), 0)

        # 站点映射（域名/名称）只构建一次
        self._site_domains = self._load_site_domains()
        self._site_names = {name.lower(): name for name in self._site_domains.values() if name}

        # 规范化持久化：修正历史遗留的非法配置，避免非法值回显到表单
        try:
            if self._current_config() != config:
                self.update_config(self._current_config())
        except Exception:
            pass

        # 加载跨会话持久化的站点档位状态与上次生效的配置签名
        # 上次扫描快照（用于定时扫描识别「新增种子」）
        self._scanned_hashes = self._load_set_map(self._SCANNED_HASHES_KEY)
        # 新增种子宣告登记列表（下载事件与扫描共用去重）与进行中宣告进度（跨会话恢复）
        self._reannounce_hashes = self._load_set_map(self._REANNOUNCE_HASHES_KEY)
        self._reannounce_progress = self._load_reannounce_progress()
        try:
            raw_states = self.get_data(self._SITE_STATES_KEY) or {}
            self._site_states = {
                str(name): str(state)
                for name, state in raw_states.items()
                if name and state in (self._STATE_LOW, self._STATE_NORMAL, self._STATE_HIGH, self._STATE_UNKNOWN)
            } if isinstance(raw_states, dict) else {}
        except Exception as err:
            logger.error(f"{self.LOG_TAG}读取持久化站点状态失败：{err}")
            self._site_states = {}
        try:
            old_sig = self.get_data(self._SIG_KEY) or {}
            if not isinstance(old_sig, dict):
                old_sig = {}
        except Exception:
            old_sig = {}
        new_sig = self._config_signature()

        # 每次重新初始化时清空会话级记录；站点档位状态保留
        self._limited_hashes = {}
        self._site_stats = {}
        self._site_ratios = {}
        self._site_ratio_times = {}
        self._reannounce_batch_ts = 0.0
        self._reannounce_batch_count = 0

        # 停用插件时：不取消任何限速（取消限速仅发生在运行中「分享率 <= 下限」档位）
        if was_enabled and not self._enabled:
            logger.info(f"{self.LOG_TAG}插件已停用，保持现有种子上传限速不变（不取消限速）")
            return

        if not self._enabled:
            return

        # ---- 启用状态：判断是否需要调用限速接口 ----
        downloaders_changed = set(old_sig.get("downloaders") or []) != set(new_sig["downloaders"])
        sites_changed = set(old_sig.get("sites") or []) != set(new_sig["sites"])
        upload_changed = old_sig and old_sig.get("upload_limit") != new_sig["upload_limit"]
        threshold_changed = old_sig and (
            old_sig.get("ratio_lower") != new_sig["ratio_lower"]
            or (old_sig.get("site_confs") or []) != new_sig["site_confs"]
        )
        # 上传速度为 0 的切换也算「限速大小变化」，需要按最新档位重新执行
        apply_changed = bool(old_sig) and (upload_changed or threshold_changed or downloaders_changed or sites_changed)
        first_run = bool(old_sig) is False

        # 首次启用或相关配置变化：以静默基线按新档位执行限速/取消限速动作；
        # 上传限速大小变化时强制按新值刷新已限速种子
        if first_run or apply_changed:
            self._process_site_ratios(apply=True, force=bool(upload_changed))
            logger.info(
                f"{self.LOG_TAG}已完成站点状态基线构建（{'首次' if first_run else '配置变化'}"
                f"{'，上传限速大小变化，已按新值刷新' if upload_changed else ''}）"
            )
        else:
            # 配置未实质变化：只刷新状态与统计（不调用限速接口）
            self._process_site_ratios(apply=False)
            logger.info(f"{self.LOG_TAG}配置未发生实质变化，仅刷新站点分享率/档位/统计，不调整限速")

        # 记录本次生效的配置签名
        try:
            self.save_data(self._SIG_KEY, new_sig)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}持久化配置签名失败：{err}")

        # 立即扫描一次（保存配置时勾选「立即扫描」开关）：延迟 3 秒执行，避免与基线流程重叠
        if self._scan_onlyonce:
            self._scan_onlyonce = False
            try:
                self.update_config(self._current_config())
            except Exception:
                pass
            logger.info(f"{self.LOG_TAG}已勾选「立即扫描下载器新增种子」，将在 3 秒后执行")
            self._start_scan_once()

        # 新增种子重新宣告调度：启用后周期宣告（有宣告任务时按期宣告，空闲时轮询）
        self._start_reannounce_scheduler()

    def _start_scan_once(self):
        """启动一次性的下载器新增种子扫描任务（与定时任务共用扫描逻辑）。"""
        try:
            self._stop_scan_scheduler()
            self._scan_scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scan_scheduler.add_job(
                func=self.scan_new_torrents,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                name="站点分享率上传限速-立即扫描",
            )
            self._scan_scheduler.start()
        except Exception as err:
            logger.error(f"{self.LOG_TAG}启动立即扫描任务失败：{err}")

    def _stop_scan_scheduler(self):
        """停止立即扫描任务（定时任务由 MoviePilot 系统调度，无需在此维护）。"""
        try:
            if getattr(self, "_scan_scheduler", None):
                if self._scan_scheduler.running:
                    self._scan_scheduler.shutdown(wait=False)
                self._scan_scheduler = None
        except Exception as err:
            logger.error(f"{self.LOG_TAG}停止立即扫描任务失败：{err}")

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册定时扫描服务：按配置的 cron 表达式周期扫描所选下载器中的新增种子
        （识别站点 + 按档位自动限速），与辅种定时任务保持同一调度体系。
        """
        if self._enabled and self._scan_enable and self._scan_cron:
            try:
                trigger = CronTrigger.from_crontab(self._scan_cron)
            except Exception as err:
                logger.error(f"{self.LOG_TAG}定时扫描 cron 表达式无效：{err}")
                return []
            return [
                {
                    "id": "SiteRatioLimiterScan",
                    "name": "扫描下载器新增种子",
                    "trigger": trigger,
                    "func": self.scan_new_torrents,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        """
        停止插件（升级/热重载/卸载时由 MoviePilot 调用）。

        注意：取消限速仅发生在运行中「分享率 <= 下限」档位；停用/卸载/升级/热重载
        都不会取消任何种子的上传限速。
        """
        self._stop_scan_scheduler()
        self._stop_reannounce_scheduler()

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """不注册额外 API。"""
        return []

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool(self._enabled)

    # ---------------------------------------------------------------- 事件监听

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event = None):
        """
        下载种子事件：直接读取插件内部维护的站点档位状态，
        仅当站点档位为「达到上限」（分享率 > 上限）时立即限制该种子的上传速度；
        站点档位为「低于下限」（不限速）且启用新增种子宣告时，登记并自动重新宣告
        （与定时扫描共用同一全局去重列表）。
        """
        if not self._enabled:
            return
        if not event:
            logger.error(f"{self.LOG_TAG}下载事件数据为空")
            return
        event_data = event.event_data or {}
        download_hash = str(event_data.get("hash") or "").strip()
        torrent_name = str(event_data.get("name") or "").strip()
        context = event_data.get("context")
        site_name = ""
        if context and hasattr(context, "torrent_info") and context.torrent_info:
            site_name = str(getattr(context.torrent_info, "site_name", "") or "").strip()
        if not download_hash:
            logger.warning(f"{self.LOG_TAG}下载事件缺少种子 hash，跳过")
            return
        if not site_name:
            logger.warning(f"{self.LOG_TAG}下载事件未识别到站点名称，种子 [{download_hash}] 跳过（不做限速处理）")
            return

        # 站点筛选：勾选了站点且不属于所选站点时跳过
        selected = self._selected_sites()
        if selected and site_name.lower() not in selected:
            logger.info(f"{self.LOG_TAG}站点 {site_name} 不在筛选范围，种子 [{download_hash}] 不做处理")
            return

        # 直接从插件内部站点状态判定（不实时查询站点数据）
        stats = self._site_stats.get(site_name) or self._site_stats.get(site_name.lower())
        state = (stats or {}).get("state")
        # 新增种子宣告：仅不限速站点（档位「低于下限」）登记并宣告（与扫描共用同一全局去重列表）
        if self._reannounce_enable and state == self._STATE_LOW:
            self._register_reannounce(
                torrent_hash=download_hash, site_name=site_name, state=state,
                torrent_name=torrent_name or download_hash,
            )
        if state != self._STATE_HIGH:
            ratio = (stats or {}).get("ratio")
            ratio_text = f"{ratio:g}" if ratio is not None else "无数据"
            logger.info(
                f"{self.LOG_TAG}站点 {site_name} 档位状态：{self._STATE_LABELS.get(state or self._STATE_UNKNOWN)}"
                f"（分享率 {ratio_text}），种子 [{download_hash}] 不做限速处理"
            )
            return

        # 站点档位为达到上限（分享率高于上限）：直接限制该种子上传速度
        if self._upload_limit <= 0:
            logger.info(f"{self.LOG_TAG}上传速度为 0（不做限速处理），种子 [{download_hash}] 跳过")
            return
        self._limit_torrent_by_hash(download_hash, site_name)

    @eventmanager.register(EventType.SiteRefreshed)
    def on_site_refreshed(self, event: Event = None):
        """
        站点分享率数据刷新事件：仅在全站刷新完成（site_id == *）时处理，
        刷新内部站点状态并按新档位执行批量限速/取消限速。
        """
        if not self._enabled:
            return
        if event:
            event_data = event.event_data or {}
            site_id = event_data.get("site_id")
            # 兼容 event_data 缺失 site_id 的情况；单站刷新（site_id 为具体站点）不处理
            if site_id not in (None, "*"):
                return
            logger.info(f"{self.LOG_TAG}站点数据刷新完成事件（site_id={site_id}），开始按最新分享率调整上传限速")
        self._process_site_ratios()

    # ---------------------------------------------------------------- 核心流程

    def _process_site_ratios(self, apply: bool = True, force: bool = False):
        """
        刷新站点账号分享率快照，重算每个管理站点的档位状态（带滞回的三档）：
        - 分享率 <= 下限 -> 🔻 低于下限：取消该站种子的上传限速，并保持到分享率 > 上限；
        - 分享率 > 上限  -> 🔺 达到上限：将该站未限速种子批量补限速（force=True 时按新值刷新已限速种子），
                           并保持到分享率 <= 下限；
        - （下限, 上限] 中间区间 -> ⏸ 中间区间：保持原档位状态（从哪边进入维持哪边），不做任何限速调整，
                           防止档位抖动导致频繁限速/取消限速；
        - 更新内部站点统计（供下载时判定与详情面板展示）。

        :param apply: 是否执行限速/取消限速动作；False 时仅刷新状态与统计（不调用限速接口）
        :param force: 是否强制按当前上传速度刷新已限速种子（上传限速大小变化时使用）
        """
        self._refresh_site_ratios()
        managed_names = self._managed_site_names()
        new_states: Dict[str, str] = {}

        if not self._site_ratios:
            logger.warning(
                f"{self.LOG_TAG}未获取到任何站点的分享率数据，请确认已安装并启用「站点数据统计」插件，"
                "并已完成至少一次站点刷新"
            )

        # 重置站点统计的限速计数
        for name in managed_names:
            stats = self._site_stats.setdefault(name, {})
            stats["limited"] = 0
            stats["total"] = 0

        for name in managed_names:
            lower, upper = self._site_thresholds(name)
            ratio = self._site_ratios.get(name)
            if ratio is None:
                ratio = self._site_ratios.get(name.lower())
            updated = self._site_ratio_times.get(name) or self._site_ratio_times.get(name.lower())
            stats = self._site_stats.setdefault(name, {})
            stats["lower"] = lower
            stats["upper"] = upper
            stats["ratio"] = ratio
            stats["updated"] = updated
            if ratio is None:
                stats["state"] = self._STATE_UNKNOWN
                new_states[name] = self._site_states.get(name, self._STATE_UNKNOWN)
                logger.info(f"{self.LOG_TAG}站点 {name} 无分享率数据（等待站点数据统计），保持原档位")
                continue

            # 带滞回的档位状态机：
            # 跨过下限（<=）-> 低于下限；跨过上限（>）-> 达到上限；中间区间保持原档位（防波动）
            prev_state = self._site_states.get(name)
            if ratio <= lower:
                new_state = self._STATE_LOW
            elif ratio > upper:
                new_state = self._STATE_HIGH
            else:
                # 中间区间：从哪边进入就维持哪边（LOW 保持不限速 / HIGH 保持限速），
                # 无历史时进入中性「中间区间」态，不做任何调整
                new_state = prev_state if prev_state in (self._STATE_LOW, self._STATE_HIGH) else self._STATE_NORMAL
            stats["state"] = new_state
            new_states[name] = new_state
            logger.info(
                f"{self.LOG_TAG}站点 {name} 分享率 {ratio:g}（阈值 {lower:g} - {upper:g}）"
                f"档位：{self._STATE_LABELS.get(new_state)}（数据时间：{updated or '—'}）"
            )

        self._site_states = new_states
        try:
            self.save_data(self._SITE_STATES_KEY, new_states)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}持久化站点档位状态失败：{err}")

        # 读取下载器种子并按站点分组，用于执行动作与刷新统计
        services = self._get_services()
        summary = []
        if not services:
            logger.warning(f"{self.LOG_TAG}没有可用的 qBittorrent 下载器，跳过限速调整（状态已刷新）")
            return
        for service_name, service_info in services.items():
            downloader = service_info.instance
            downloader_type = getattr(service_info, "type", "")
            by_site, torrents = self._torrents_by_site(service_name, downloader)
            # 清理已不在下载器中的种子记录（已删除的种子），避免会话登记无限驻留
            current_hashes = {self._torrent_hash(t) for t in torrents if self._torrent_hash(t)}
            self._limited_hashes.get(service_name, set()).intersection_update(current_hashes)
            self._cleanup_reannounce_progress(service_name, current_hashes)
            # 执行动作：仅当 apply=True 时才调用限速/取消限速接口
            if apply:
                for name in managed_names:
                    hit_site = next((s for s in by_site if s.lower() == name.lower()), None)
                    site_torrents = by_site.get(hit_site, []) if hit_site else []
                    state = self._site_states.get(name)
                    if state == self._STATE_LOW:
                        canceled = self._cancel_site_limits(service_name, downloader, name, site_torrents)
                        summary.append(f"{service_name}：站点 {name} 取消限速 {canceled} 个种子")
                    elif state == self._STATE_HIGH:
                        limit = self._effective_upload_limit(downloader, downloader_type, self._upload_limit)
                        if limit <= 0:
                            summary.append(f"{service_name}：站点 {name} 上传速度为 0，不执行限速")
                        else:
                            applied = self._apply_site_limits(
                                service_name, downloader, name, site_torrents, limit, force=force
                            )
                            summary.append(f"{service_name}：站点 {name} 限速 {applied} 个种子")
                    else:
                        # 中间区间：保持现状，不调用限速接口
                        summary.append(f"{service_name}：站点 {name} 处于中间区间，保持现状不调整")
                # 动作后重新拉取一次种子列表（限速值已刷新），避免用动作前的旧快照统计
                by_site, _ = self._torrents_by_site(service_name, downloader)
            # 统计：已限速种子数按当前实际限速统计（含外部限速），作用于动作之后不受时序影响
            for name in managed_names:
                hit_site = next((s for s in by_site if s.lower() == name.lower()), None)
                site_torrents = by_site.get(hit_site, []) if hit_site else []
                stats = self._site_stats.setdefault(name, {})
                stats["total"] = stats.get("total", 0) + len(site_torrents)
                stats["limited"] = stats.get("limited", 0) + sum(
                    1 for torrent in site_torrents if self._torrent_current_limit_kb(torrent) > 0
                )
        if summary:
            logger.info(f"{self.LOG_TAG}" + "；".join(summary))

    def _limit_torrent_by_hash(self, download_hash: str, site_name: str) -> bool:
        """
        按种子 hash 在选中的下载器中定位种子并设置上传限速；
        种子尚未出现在下载器中时记日志跳过（后续刷新事件正常档位时会批量补限速）。
        """
        services = self._get_services()
        if not services:
            logger.warning(f"{self.LOG_TAG}没有可用的 qBittorrent 下载器，种子 [{download_hash}] 限速失败")
            return False
        total_limit = self._upload_limit
        for service_name, service_info in services.items():
            downloader = service_info.instance
            try:
                torrents, error = downloader.get_torrents()
                if error:
                    continue
                target = next((t for t in (torrents or []) if self._torrent_hash(t) == download_hash), None)
                if not target:
                    continue
                effective_limit = self._effective_upload_limit(
                    downloader, getattr(service_info, "type", ""), total_limit
                )
                if effective_limit <= 0:
                    logger.info(f"{self.LOG_TAG}站点 {site_name} 上传速度为 0，种子 [{download_hash}] 不做限速处理")
                    return False
                ok = downloader.change_torrent(hash_string=download_hash, upload_limit=effective_limit)
                if not ok:
                    logger.error(f"{self.LOG_TAG}[{service_name}] 种子 [{download_hash}] 设置上传限速失败")
                    return False
                self._limited_hashes.setdefault(service_name, set()).add(download_hash)
                # 同步站点统计中的限速计数
                stats = self._site_stats.setdefault(site_name, {})
                stats["limited"] = stats.get("limited", 0) + 1
                stats["total"] = stats.get("total", 0) + 1
                logger.info(f"{self.LOG_TAG}[{service_name}] 种子 [{self._torrent_name(target) or download_hash}] 所属站点 {site_name} 档位正常（分享率高于下限），已限速 {self._format_limit(effective_limit)}")
                return True
            except Exception as err:
                logger.error(f"{self.LOG_TAG}[{service_name}] 定位种子 [{download_hash}] 失败：{err}")
        logger.warning(f"{self.LOG_TAG}种子 [{download_hash}] 未在已选下载器中发现（可能尚未添加完成），跳过")
        return False

    def scan_new_torrents(self):
        """
        定时/立即扫描所选下载器中的「新增种子」（与上次扫描快照对比，跨会话持久化）：

        - 识别每个新增种子所属站点（下载历史 -> tracker 域名 -> 标签 -> 分类）；
        - 识别到站点且该站档位为「达到上限」且种子未限速 -> 复用现有规则自动限速；
          已被限速的种子登记为「本插件维护限速中」跳过接口调用（防抖动/防误恢复）；
        - 无法识别站点的种子记录 warning 日志（种子名/hash/tracker/标签/分类），
          并统计到汇总日志，便于排查「同一站点部分种子识别不到」的问题；
        - 扫描完成后更新并持久化扫描快照，下次只处理新增种子。

        运行方式：MoviePilot 系统调度（get_service 注册的 cron 定时任务）
        或「立即扫描」一次性任务（BackgroundScheduler date 任务）。
        """
        if not self._enabled:
            logger.info(f"{self.LOG_TAG}插件未启用，跳过下载器种子扫描")
            return
        logger.info(f"{self.LOG_TAG}开始扫描下载器新增种子 ...")

        services = self._get_services()
        if not services:
            logger.warning(f"{self.LOG_TAG}扫描失败：没有可用的 qBittorrent 下载器")
            return

        # 上次扫描快照 {下载器: {hash}}，首次为空时全部种子视为新增
        last_scanned = {name: set(hash_list) for name, hash_list in (self._scanned_hashes or {}).items()}
        # 本轮扫描快照 {下载器: {hash}}：先保留本次未参与扫描的下载器旧快照，避免断连期被清空后误判
        current_scanned: Dict[str, Set[str]] = {name: set(hash_list) for name, hash_list in last_scanned.items()}
        # 汇总统计
        total_new = 0
        identified = 0
        unidentified = 0
        limited_new = 0
        already_limited = 0
        skipped = 0

        for service_name, service_info in services.items():
            downloader = service_info.instance
            downloader_type = getattr(service_info, "type", "")
            try:
                torrents, error = downloader.get_torrents()
            except Exception as err:
                logger.error(f"{self.LOG_TAG}[{service_name}] 获取种子列表失败：{err}")
                continue
            if error:
                logger.warning(f"{self.LOG_TAG}[{service_name}] 获取种子列表失败")
                continue
            torrents = torrents or []
            current_hashes = {self._torrent_hash(t) for t in torrents if self._torrent_hash(t)}
            self._cleanup_reannounce_progress(service_name, current_hashes)
            current_scanned[service_name] = current_hashes
            # 新增种子 = 当前快照 - 上次快照
            new_hashes = current_hashes - (last_scanned.get(service_name) or set())
            if not new_hashes:
                logger.info(f"{self.LOG_TAG}[{service_name}] 无新增种子（当前共 {len(current_hashes)} 个）")
                continue
            new_torrents = [t for t in torrents if self._torrent_hash(t) in new_hashes]
            total_new += len(new_torrents)
            logger.info(f"{self.LOG_TAG}[{service_name}] 扫描到 {len(new_torrents)} 个新增种子，开始识别站点 ...")

            # 站点识别：批量查询一次下载历史（新增种子）
            history_sites = self._load_history_sites(list(new_hashes))
            site_cache: Dict[str, str] = {}
            for torrent in new_torrents:
                torrent_hash = self._torrent_hash(torrent)
                torrent_name = self._torrent_name(torrent) or torrent_hash
                # 识别站点（下载历史 -> tracker 域名 -> 标签 -> 分类）
                site = self._resolve_site(torrent, torrent_hash, history_sites, site_cache)
                if not site:
                    unidentified += 1
                    logger.warning(
                        f"{self.LOG_TAG}[{service_name}] 新增种子未识别到站点：{torrent_name}"
                        f"（hash={torrent_hash}，tracker={self._torrent_tracker_urls(torrent) or '无'}，"
                        f"标签={self._torrent_tags(torrent) or '无'}，分类={self._torrent_category(torrent) or '无'}）"
                    )
                    continue
                identified += 1
                # 站点筛选：勾选了站点且不属于所选站点时跳过
                selected = self._selected_sites()
                if selected and site.lower() not in selected:
                    skipped += 1
                    logger.info(f"{self.LOG_TAG}[{service_name}] 新增种子 {torrent_name} 站点 {site} 不在筛选范围，跳过")
                    continue
                # 复用现有规则：仅当站点档位为「达到上限」且种子未限速时自动限速
                stats = self._site_stats.get(site) or self._site_stats.get(site.lower()) or {}
                state = stats.get("state")
                # 新增种子宣告登记：下载事件与扫描共用同一全局去重列表（同一种子只登记一次），
                # 仅「低于下限（不限速）」站点的种子在全局宣告种子数额度内实际宣告
                self._register_reannounce(
                    torrent_hash=torrent_hash, site_name=site, state=state,
                    torrent_name=torrent_name, service_name=service_name,
                )
                if state != self._STATE_HIGH:
                    ratio = stats.get("ratio")
                    ratio_text = f"{ratio:g}" if ratio is not None else "无数据"
                    skipped += 1
                    logger.info(
                        f"{self.LOG_TAG}[{service_name}] 新增种子 {torrent_name} 站点 {site} 档位："
                        f"{self._STATE_LABELS.get(state or self._STATE_UNKNOWN)}（分享率 {ratio_text}），不做限速"
                    )
                    continue
                if self._upload_limit <= 0:
                    skipped += 1
                    logger.info(f"{self.LOG_TAG}上传速度为 0（不做限速处理），新增种子 [{torrent_name}] 跳过")
                    continue
                # 已被限速（含外部限速）：登记为「本插件维护限速中」，跳过接口调用（防误恢复）
                if self._torrent_current_limit_kb(torrent) > 0:
                    already_limited += 1
                    self._limited_hashes.setdefault(service_name, set()).add(torrent_hash)
                    logger.info(f"{self.LOG_TAG}[{service_name}] 新增种子 {torrent_name} 站点 {site} 已被限速，登记跳过")
                    continue
                # 站点档位达到上限且未限速：设置上传限速
                limit = self._effective_upload_limit(downloader, downloader_type, self._upload_limit)
                if limit <= 0:
                    skipped += 1
                    continue
                try:
                    ok = downloader.change_torrent(hash_string=torrent_hash, upload_limit=int(limit))
                except Exception as err:
                    ok = False
                    logger.error(f"{self.LOG_TAG}[{service_name}] 新增种子 [{torrent_name}] 设置上传限速失败：{err}")
                if ok:
                    self._limited_hashes.setdefault(service_name, set()).add(torrent_hash)
                    limited_new += 1
                    # 同步站点统计的限速计数
                    site_stats = self._site_stats.setdefault(site, {})
                    site_stats["limited"] = site_stats.get("limited", 0) + 1
                    site_stats["total"] = site_stats.get("total", 0) + 1
                    logger.info(
                        f"{self.LOG_TAG}[{service_name}] 新增种子 {torrent_name} 站点 {site} 档位达到上限，"
                        f"已限速 {self._format_limit(limit)}"
                    )
                else:
                    skipped += 1

        # 更新并持久化扫描快照
        self._scanned_hashes = current_scanned
        self._save_set_map(self._SCANNED_HASHES_KEY, self._scanned_hashes)

        logger.info(
            f"{self.LOG_TAG}扫描完成：新增 {total_new} 个种子，识别站点 {identified} 个，"
            f"未识别站点 {unidentified} 个，自动限速 {limited_new} 个，已限速跳过 {already_limited} 个，"
            f"不做处理 {skipped} 个"
        )

    def _cancel_site_limits(self, service_name: str, downloader: Any, site_name: str, torrents: List[Any]) -> int:
        """取消指定站点所有种子的上传速度上限（恢复不限速）。"""
        canceled = 0
        for torrent in torrents:
            torrent_hash = self._torrent_hash(torrent)
            if not torrent_hash:
                continue
            current_kb = self._torrent_current_limit_kb(torrent)
            if current_kb <= 0:
                continue
            try:
                ok = downloader.change_torrent(hash_string=torrent_hash, upload_limit=0)
            except Exception as err:
                ok = False
                logger.error(f"{self.LOG_TAG}[{service_name}] 站点 {site_name} 种子 [{torrent_hash}] 取消限速失败：{err}")
            if ok:
                self._limited_hashes.get(service_name, set()).discard(torrent_hash)
                canceled += 1
                logger.info(f"{self.LOG_TAG}[{service_name}] 站点 {site_name} 分享率低于下限，已取消种子 [{torrent_hash}] 的上传限速")
            # 取消失败不登记：下次分享率刷新仍处于「低于下限」档位时会自然重试
        return canceled

    def _apply_site_limits(self, service_name: str, downloader: Any, site_name: str, torrents: List[Any], limit: float, force: bool = False) -> int:
        """
        对指定站点「达到上限」档位的种子设置上传限速：
        - force=False（常规）：仅对当前未限速（up_limit=0）的种子调用接口设置限速；
          已限速（up_limit>0）的种子跳过接口调用，但仍登记为「本插件维护限速中」，
          避免重复调用接口与限速/取消抖动；
        - force=True（上传限速大小变化）：当前值已等于目标值的跳过，其余（0 或旧值）按目标值刷新。
        """
        applied = 0
        target = int(limit)
        limited = self._limited_hashes.setdefault(service_name, set())
        for torrent in torrents:
            torrent_hash = self._torrent_hash(torrent)
            if not torrent_hash:
                continue
            current_kb = self._torrent_current_limit_kb(torrent)
            if force:
                # 上传限速大小变化：仅需要调整的种子才调用接口
                if abs(current_kb - target) < 1:
                    limited.add(torrent_hash)  # 当前值已等于目标：登记为限速中，跳过接口调用
                    continue
            else:
                # 常规：已限速（含外部限速与本插件历史限速）不重复干预，但登记为限速中
                if current_kb > 0:
                    limited.add(torrent_hash)
                    continue
            try:
                ok = downloader.change_torrent(hash_string=torrent_hash, upload_limit=target)
            except Exception as err:
                ok = False
                logger.error(f"{self.LOG_TAG}[{service_name}] 站点 {site_name} 种子 [{torrent_hash}] 设置上传限速失败：{err}")
            if ok:
                limited.add(torrent_hash)
                applied += 1
        return applied

    # ---------------------------------------------------------------- 站点识别

    def _managed_site_names(self) -> List[str]:
        """
        返回需要管理的站点名称列表：活动站点 + 配置了单独阈值的站点 + 有分享率数据的站点
        （按站点筛选过滤）。
        """
        names = list(dict.fromkeys(
            list(self._site_names.values())
            + list(self._site_confs.keys())
            + list(self._site_ratios.keys())
        ))
        selected = self._selected_sites()
        if selected:
            names = [name for name in names if name.lower() in selected]
        return names

    def _site_thresholds(self, site_name: str) -> Tuple[float, float]:
        """返回站点分享率（下限, 上限）；未配置单独阈值的站点使用全局值。"""
        conf = self._site_confs.get(str(site_name or "").strip().lower())
        if conf:
            lower, upper = conf
            # 单独阈值未配置上限（旧版/单值配置）时继承全局上限
            if upper <= 0:
                upper = self._ratio_upper
            return lower, upper
        return self._ratio_lower, self._ratio_upper

    def _load_site_domains(self) -> Dict[str, str]:
        """构建 站点域名(小写) -> 站点名称 映射，用于识别种子所属站点。"""
        domains = {}
        try:
            from app.helper.sites import SitesHelper
            for site in SitesHelper().get_indexers() or []:
                if not site.get("is_active"):
                    continue
                name = str(site.get("name") or "").strip()
                if not name:
                    continue
                domain = str(site.get("domain") or "").strip().lower()
                if domain:
                    domains[domain] = name
                url = str(site.get("url") or "").strip()
                if url:
                    url_domain = self._normalize_domain(url)
                    if url_domain:
                        domains[url_domain] = name
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取站点配置失败：{err}")
        return domains

    @staticmethod
    def _load_history_sites(hashes: List[str]) -> Dict[str, str]:
        """批量查询下载历史，返回 {种子Hash: 站点名称}，作为站点识别的权威依据。"""
        hashes = [h for h in dict.fromkeys(hashes) if h]
        if not hashes:
            return {}
        try:
            from app.db.downloadhistory_oper import DownloadHistoryOper
            histories = DownloadHistoryOper().get_by_hashes(hashes)
            return {
                history.download_hash: (history.torrent_site or "").strip()
                for history in histories.values()
                if history and (history.torrent_site or "").strip()
            }
        except Exception as err:
            logger.warning(f"查询下载历史站点失败：{err}")
            return {}

    def _torrents_by_site(self, service_name: str, downloader: Any) -> Tuple[Dict[str, List[Any]], List[Any]]:
        """按站点分组下载器中的种子，返回 ({站点名称: [种子]}, 种子列表)。"""
        try:
            torrents, error = downloader.get_torrents()
        except Exception as err:
            logger.error(f"{self.LOG_TAG}[{service_name}] 获取种子列表失败：{err}")
            return {}, []
        if error:
            logger.warning(f"{self.LOG_TAG}[{service_name}] 获取种子列表失败")
            return {}, []
        torrents = torrents or []
        hashes = [self._torrent_hash(t) for t in torrents]
        history_sites = self._load_history_sites(hashes)
        site_cache: Dict[str, str] = {}
        by_site: Dict[str, List[Any]] = {}
        for torrent in torrents:
            torrent_hash = self._torrent_hash(torrent)
            site = self._resolve_site(torrent, torrent_hash, history_sites, site_cache)
            if site:
                by_site.setdefault(site, []).append(torrent)
        return by_site, torrents

    def _resolve_site(self, torrent: Any, torrent_hash: str, history_sites: Dict[str, str], site_cache: Dict[str, str]) -> str:
        """识别种子所属站点（带缓存）：下载历史记录 -> tracker 域名 -> 标签 -> 分类。"""
        if torrent_hash in site_cache:
            return site_cache[torrent_hash]
        site = history_sites.get(torrent_hash) or self._torrent_site(torrent)
        site_cache[torrent_hash] = site or ""
        return site_cache[torrent_hash]

    def _torrent_site(self, torrent: Any) -> str:
        """识别种子所属站点：优先通过 tracker 域名匹配，其次匹配标签/分类中的站点名。"""
        for url in self._torrent_tracker_urls(torrent):
            domain = self._normalize_domain(url)
            if domain:
                hit = self._lookup_site_by_domain(domain)
                if hit:
                    return hit
        for tag in self._torrent_tags(torrent):
            hit = self._site_names.get(str(tag).strip().lower())
            if hit:
                return hit
        category = self._torrent_category(torrent)
        if category:
            hit = self._site_names.get(str(category).strip().lower())
            if hit:
                return hit
        return ""

    def _lookup_site_by_domain(self, host: str) -> str:
        """按域名（含子域名逐级回退）查找站点名称。"""
        host = (host or "").strip().lower()
        if not host:
            return ""
        if host.startswith("www."):
            host = host[4:]
        if host in self._site_domains:
            return self._site_domains[host]
        labels = host.split(".")
        for i in range(1, len(labels)):
            candidate = ".".join(labels[i:])
            if candidate in self._site_domains:
                return self._site_domains[candidate]
        return ""

    @staticmethod
    def _normalize_domain(url: str) -> str:
        """提取 URL 的域名部分（去除协议、端口与路径），统一转为小写。"""
        try:
            host = (urlparse(str(url or "")).hostname or "").strip().lower()
        except Exception:
            return ""
        if host.startswith("www."):
            host = host[4:]
        return host

    @staticmethod
    def _torrent_tracker_urls(torrent: Any) -> List[str]:
        """获取种子 tracker 地址列表（qBittorrent）。"""
        urls = []
        if isinstance(torrent, dict):
            tracker = torrent.get("tracker") or ""
            if tracker:
                urls.append(str(tracker))
        return urls

    @staticmethod
    def _torrent_tags(torrent: Any) -> List[str]:
        """获取种子标签列表（qBittorrent）。"""
        if not isinstance(torrent, dict):
            return []
        tags = torrent.get("tags") or ""
        return [str(tag).strip() for tag in str(tags).split(",") if str(tag).strip()]

    @staticmethod
    def _torrent_category(torrent: Any) -> str:
        """获取种子分类（qBittorrent）。"""
        if isinstance(torrent, dict):
            return str(torrent.get("category") or "").strip()
        return ""

    @staticmethod
    def _torrent_hash(torrent: Any) -> str:
        """获取种子哈希（qBittorrent）。"""
        return str(torrent.get("hash") or "").strip() if isinstance(torrent, dict) else ""

    @staticmethod
    def _torrent_name(torrent: Any) -> str:
        """获取种子名称。"""
        return str(torrent.get("name") or "") if isinstance(torrent, dict) else ""

    @staticmethod
    def _torrent_current_limit_kb(torrent: Any) -> float:
        """读取种子当前上传限速（KB/s），0 表示不限速；读取失败返回 0。"""
        if not isinstance(torrent, dict):
            return 0.0
        try:
            return float(torrent.get("up_limit") or 0) / 1024
        except (TypeError, ValueError):
            return 0.0

    # ---------------------------------------------------------------- 分享率数据

    def _refresh_site_ratios(self):
        """
        刷新各站点账号分享率快照（与流量管理/魔力兑换一致的数据源）：
        读取「站点数据统计」插件写入的 SiteUserData（按站点名称，今天数据优先，
        缺失回退昨天），写入 self._site_ratios（{站点名: 分享率}）与
        self._site_ratio_times（{站点名: 数据时间}）。
        """
        self._site_ratios = {}
        self._site_ratio_times = {}
        try:
            import pytz
            from app.db.site_oper import SiteOper
            siteoper = SiteOper()
            current_day = datetime.datetime.now(tz=pytz.timezone(settings.TZ)).date()
            previous_day = current_day - datetime.timedelta(days=1)
            current_rows = {row.name: row for row in (siteoper.get_userdata_by_date(date=str(current_day)) or []) if getattr(row, "name", None)}
            previous_rows = {row.name: row for row in (siteoper.get_userdata_by_date(date=str(previous_day)) or []) if getattr(row, "name", None)}

            def get_ratio(row: Any) -> Optional[float]:
                """提取有效分享率；err_msg、ratio<=0 视为无效返回 None。"""
                if not row:
                    return None
                data = row.to_dict() if hasattr(row, "to_dict") else {}
                if data.get("err_msg"):
                    return None
                try:
                    ratio = float(data.get("ratio") or 0)
                except (TypeError, ValueError):
                    return None
                return ratio if ratio > 0 else None

            for name in set(current_rows) | set(previous_rows):
                # 今天数据优先，无效时回退昨天
                row = current_rows.get(name)
                ratio = get_ratio(row)
                if ratio is None:
                    row = previous_rows.get(name)
                    ratio = get_ratio(row)
                if ratio is None:
                    continue
                self._site_ratios[name] = ratio
                updated = f"{getattr(row, 'updated_day', '') or ''} {getattr(row, 'updated_time', '') or ''}".strip()
                self._site_ratio_times[name] = updated or ""
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取站点账号分享率失败：{err}")

    # ---------------------------------------------------------------- 下载器

    def _selected_sites(self) -> Optional[Set[str]]:
        """返回已勾选站点的规范化（小写）集合；未勾选返回 None（表示不筛选站点）。"""
        sites = [str(site).strip() for site in (self._sites or []) if str(site).strip()]
        return {site.lower() for site in sites} if sites else None

    def _get_services(self, downloaders: Optional[List[str]] = None) -> Optional[Dict[str, ServiceInfo]]:
        """获取已启用且可连接的 qBittorrent 下载器实例，返回 {下载器名称: ServiceInfo}。"""
        names = downloaders if downloaders is not None else self._downloaders
        if not names:
            logger.warning(f"{self.LOG_TAG}尚未选择下载器")
            return None
        services = DownloaderHelper().get_services(name_filters=names)
        if not services:
            logger.warning(f"{self.LOG_TAG}获取下载器实例失败，请检查配置")
            return None
        helper = DownloaderHelper()
        active_services = {}
        for service_name, service_info in services.items():
            if not helper.is_downloader(service_type="qbittorrent", service=service_info):
                logger.warning(f"{self.LOG_TAG}下载器 [{service_name}] 不是 qBittorrent，已跳过")
                continue
            if not getattr(service_info, "instance", None):
                logger.warning(f"{self.LOG_TAG}下载器 [{service_name}] 实例不存在，已跳过")
                continue
            if service_info.instance.is_inactive():
                logger.warning(f"{self.LOG_TAG}下载器 [{service_name}] 未连接，已跳过")
                continue
            active_services[service_name] = service_info
        if not active_services:
            logger.warning(f"{self.LOG_TAG}没有可用的 qBittorrent 下载器")
            return None
        return active_services

    def _effective_upload_limit(self, downloader: Any, downloader_type: str, configured_limit: int) -> float:
        """
        返回下载器实际应使用的单种子上传限速（KB/s）。
        qBittorrent 启用了全局上传限速且低于插件配置时，采用全局值。
        """
        limit = max(self._to_int(configured_limit, 0), 0)
        if limit <= 0 or str(downloader_type or "").strip().lower() != "qbittorrent":
            return limit
        get_speed_limit = getattr(downloader, "get_speed_limit", None)
        if not callable(get_speed_limit):
            return limit
        try:
            speed_limits = get_speed_limit()
            if not isinstance(speed_limits, (tuple, list)) or len(speed_limits) < 2:
                raise ValueError("返回值格式无效")
            qb_upload_limit = float(speed_limits[1] or 0)
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取 qBittorrent 全局上传限速失败，使用插件配置 {self._format_limit(limit)}：{err}")
            return limit
        if qb_upload_limit <= 0 or qb_upload_limit != qb_upload_limit:
            return limit
        effective_limit = min(float(limit), qb_upload_limit)
        return int(effective_limit) if effective_limit.is_integer() else effective_limit

    # ---------------------------------------------------------------- 新增种子重新宣告

    def _register_reannounce(self, torrent_hash: str, site_name: str, state: Optional[str] = None,
                             torrent_name: str = "", service_name: str = ""):
        """
        新增种子宣告登记（下载事件与扫描共用同一全局去重列表）：
        - 无论档位，新种子都会进入全局去重列表（跨会话持久化），同一种子只登记一次；
        - 仅档位「低于下限」（🔻 不限速站点）的种子会实际宣告；
        - 全局总宣告种子数：同一批次时间窗口（约 10 秒）内最多宣告该数量个新种子，
          超出的加入列表跳过宣告，之后任何来源再次发现也不会宣告。
        """
        if not self._enabled or not self._reannounce_enable or not torrent_hash:
            return
        # 全局去重：任一已登记列表含该 hash 则不再处理（防止下载事件/扫描重复登记与重复宣告）
        if any(torrent_hash in hashes for hashes in self._reannounce_hashes.values()):
            return
        # 下载事件来源未提供下载器时按 hash 定位所属下载器
        if not service_name:
            service_name = self._locate_service_by_hash(torrent_hash)
            if not service_name:
                logger.info(
                    f"{self.LOG_TAG}种子 {torrent_name or torrent_hash} 尚未出现在下载器中，"
                    "宣告登记跳过（后续新增种子扫描会补登记）"
                )
                return
        # 所有新增种子加入全局去重列表（事件与扫描共用，跨会话持久化）
        self._reannounce_hashes.setdefault(service_name, set()).add(torrent_hash)
        self._save_set_map(self._REANNOUNCE_HASHES_KEY, self._reannounce_hashes)
        label = self._STATE_LABELS.get(state or self._STATE_UNKNOWN, state or self._STATE_UNKNOWN)
        # 宣告对象：仅「低于下限（不限速）」站点的种子
        if state != self._STATE_LOW:
            logger.info(
                f"{self.LOG_TAG}[{service_name}] 种子 {torrent_name or torrent_hash} 站点 {site_name}"
                f" 档位 {label}，加入宣告列表但跳过宣告（仅「低于下限」不限速站点宣告）"
            )
            return
        # 全局总宣告种子数：同一批次时间窗口内仅宣告前 N 个
        now = time.time()
        if now - self._reannounce_batch_ts > self._REANNOUNCE_BATCH_WINDOW:
            self._reannounce_batch_ts = now
            self._reannounce_batch_count = 0
        if self._reannounce_batch_count >= self._reannounce_limit:
            logger.info(
                f"{self.LOG_TAG}[{service_name}] 种子 {torrent_name or torrent_hash} 站点 {site_name}"
                f" 超过全局宣告种子数上限（{self._reannounce_limit} 个/批次窗口），加入列表跳过宣告"
            )
            return
        self._reannounce_batch_count += 1
        # 启动宣告：首次延迟后开始，共宣告 reannounce_times 次
        prog = self._reannounce_progress.setdefault(service_name, {})
        prog[torrent_hash] = {
            "name": torrent_name or torrent_hash,
            "site": site_name,
            "total": self._reannounce_times,
            "done": 0,
            "next_ts": now + self._reannounce_delay,
        }
        self._save_reannounce_progress()
        logger.info(
            f"{self.LOG_TAG}[{service_name}] 种子 {torrent_name or torrent_hash}（站点 {site_name}，"
            f"不限速）已加入宣告队列：首次宣告约 {self._reannounce_delay} 秒后，共宣告 {self._reannounce_times} 次"
        )
        self._start_reannounce_scheduler()

    def _locate_service_by_hash(self, torrent_hash: str) -> str:
        """在已选下载器中定位种子所属下载器名称（下载事件未提供下载器时使用）。"""
        services = self._get_services()
        if not services:
            return ""
        for service_name, service_info in services.items():
            downloader = service_info.instance
            try:
                torrents, error = downloader.get_torrents()
                if error:
                    continue
                if any(self._torrent_hash(t) == torrent_hash for t in (torrents or [])):
                    return service_name
            except Exception:
                continue
        return ""

    def _start_reannounce_scheduler(self):
        """启动新增种子宣告调度（每 5 秒轮询到期种子做一次重新宣告）。"""
        try:
            if getattr(self, "_reannounce_scheduler", None) and self._reannounce_scheduler.running:
                return
            if not self._enabled or not self._reannounce_enable:
                return
            self._reannounce_scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._reannounce_scheduler.add_job(
                func=self._reannounce_tick,
                trigger="interval",
                seconds=5,
                max_instances=1,
                name="站点分享率上传限速-新种子宣告",
            )
            self._reannounce_scheduler.start()
            logger.info(f"{self.LOG_TAG}新增种子宣告调度已启动")
        except Exception as err:
            logger.error(f"{self.LOG_TAG}启动新增种子宣告调度失败：{err}")

    def _stop_reannounce_scheduler(self):
        """停止新增种子宣告调度。"""
        try:
            if getattr(self, "_reannounce_scheduler", None):
                if self._reannounce_scheduler.running:
                    self._reannounce_scheduler.shutdown(wait=False)
                self._reannounce_scheduler = None
        except Exception as err:
            logger.error(f"{self.LOG_TAG}停止新增种子宣告调度失败：{err}")

    def _reannounce_tick(self):
        """周期宣告任务：对到期的种子调用 qBittorrent 重新宣告接口，并更新宣告进度。"""
        if not self._enabled or not self._reannounce_enable:
            return
        if not self._reannounce_progress:
            return
        now = time.time()
        due_by_service: Dict[str, List[str]] = {}
        for service_name, prog in self._reannounce_progress.items():
            due = [
                h for h, p in prog.items()
                if (p.get("next_ts") or 0) <= now and (p.get("done") or 0) < (p.get("total") or 1)
            ]
            if due:
                due_by_service[service_name] = due
        if not due_by_service:
            return
        services = self._get_services() or {}
        changed = False
        for service_name, due in due_by_service.items():
            prog = self._reannounce_progress.get(service_name) or {}
            service_info = services.get(service_name)
            qbc = getattr(getattr(service_info, "instance", None), "qbc", None)
            reannounce = getattr(qbc, "torrents_reannounce", None) if qbc else None
            if not callable(reannounce):
                logger.warning(
                    f"{self.LOG_TAG}[{service_name}] 无可用 qBittorrent 重新宣告接口，本轮 {len(due)} 个种子宣告跳过（下载器重连后将自动继续）"
                )
                continue
            try:
                reannounce(torrent_hashes=due)
            except Exception as err:
                logger.error(f"{self.LOG_TAG}[{service_name}] 重新宣告 {len(due)} 个种子失败：{err}")
                for torrent_hash in due:
                    p = prog.pop(torrent_hash, None)
                    if p:
                        logger.warning(
                            f"{self.LOG_TAG}[{service_name}] 种子 {p.get('name') or torrent_hash} 宣告失败，"
                            "已移出宣告队列（去重登记保留）"
                        )
                changed = True
                continue
            for torrent_hash in due:
                p = prog.get(torrent_hash) or {}
                total = p.get("total") or self._reannounce_times
                p["done"] = (p.get("done") or 0) + 1
                if p["done"] >= total:
                    prog.pop(torrent_hash, None)
                    logger.info(
                        f"{self.LOG_TAG}[{service_name}] 种子 {p.get('name') or torrent_hash}"
                        f"（站点 {p.get('site') or '—'}）宣告完成：{p['done']}/{total} 次"
                    )
                else:
                    p["next_ts"] = now + self._reannounce_interval
                    logger.info(
                        f"{self.LOG_TAG}[{service_name}] 种子 {p.get('name') or torrent_hash}"
                        f"（站点 {p.get('site') or '—'}）已宣告：{p['done']}/{total} 次"
                    )
                changed = True
            if not prog:
                self._reannounce_progress.pop(service_name, None)
        if changed:
            self._save_reannounce_progress()

    def _cleanup_reannounce_progress(self, service_name: str, current_hashes: Set[str]):
        """清理宣告进度中已不在下载器的种子（种子被删除），去重登记列表则长期保留。"""
        prog = self._reannounce_progress.get(service_name)
        if not prog:
            return
        removed = [h for h in prog if h not in current_hashes]
        if not removed:
            return
        for h in removed:
            prog.pop(h, None)
        if not prog:
            self._reannounce_progress.pop(service_name, None)
        logger.info(f"{self.LOG_TAG}[{service_name}] 清理 {len(removed)} 个已删除/消失种子的宣告进度")
        self._save_reannounce_progress()

    def _load_reannounce_progress(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """读取跨会话持久化的宣告进度 {下载器: {种子Hash: {name,site,total,done,next_ts}}}。"""
        try:
            raw = self.get_data(self._REANNOUNCE_PROGRESS_KEY) or {}
            if not isinstance(raw, dict):
                return {}
            loaded: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for service, hashes in raw.items():
                if not isinstance(hashes, dict):
                    continue
                entries: Dict[str, Dict[str, Any]] = {}
                for torrent_hash, item in hashes.items():
                    if not torrent_hash or not isinstance(item, dict):
                        continue
                    entries[str(torrent_hash)] = {
                        "name": str(item.get("name") or torrent_hash),
                        "site": str(item.get("site") or ""),
                        "total": max(int(item.get("total") or self._reannounce_times), 1),
                        "done": max(int(item.get("done") or 0), 0),
                        "next_ts": float(item.get("next_ts") or 0),
                    }
                if entries:
                    loaded[str(service)] = entries
            return loaded
        except Exception as err:
            logger.error(f"{self.LOG_TAG}读取宣告进度失败：{err}")
            return {}

    def _save_reannounce_progress(self):
        """持久化宣告进度。"""
        try:
            self.save_data(self._REANNOUNCE_PROGRESS_KEY, self._reannounce_progress)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}持久化宣告进度失败：{err}")

    # ---------------------------------------------------------------- 持久化

    def _load_set_map(self, key: str) -> Dict[str, set]:
        """从插件数据中加载 {下载器: {种子Hash}} 集合映射（JSON 兼容存储为列表）。"""
        try:
            raw = self.get_data(key) or {}
            if not isinstance(raw, dict):
                return {}
            return {
                str(service): set(str(hash_value) for hash_value in (hashes or []))
                for service, hashes in raw.items()
                if service and hashes
            }
        except Exception as err:
            logger.error(f"{self.LOG_TAG}读取持久化数据 {key} 失败：{err}")
            return {}

    def _save_set_map(self, key: str, mapping: Dict[str, set]):
        """将 {下载器: {种子Hash}} 集合映射持久化为 JSON 兼容格式，空集合不落盘。"""
        try:
            payload = {
                service: sorted(hashes)
                for service, hashes in mapping.items()
                if service and hashes
            }
            self.save_data(key, payload)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}持久化数据 {key} 失败：{err}")

    # ---------------------------------------------------------------- 配置与页面

    def _config_signature(self) -> Dict[str, Any]:
        """返回影响限速动作的配置签名（用于判断保存配置后是否需要调用限速接口）。"""
        return {
            "upload_limit": self._upload_limit,
            "ratio_lower": self._ratio_lower,
            "ratio_upper": self._ratio_upper,
            # 站点单独阈值确定性排序：{站点小写: 下限-上限}
            "site_confs": sorted(
                f"{key}={lower},{upper}" for key, (lower, upper) in self._site_confs.items()
            ),
            "downloaders": sorted(self._downloaders),
            "sites": sorted(self._sites),
        }

    def _current_config(self) -> Dict[str, Any]:
        """返回当前配置，供表单回填。"""
        return {
            "enabled": self._enabled,
            "downloaders": self._downloaders,
            "sites": self._sites,
            "ratio_lower_limit": self._ratio_lower,
            "ratio_upper_limit": self._ratio_upper,
            "site_confs": self._site_conf_text,
            "upload_limit": self._upload_limit,
            "scan_enable": self._scan_enable,
            "scan_cron": self._scan_cron,
            "scan_onlyonce": self._scan_onlyonce,
            "reannounce_enable": self._reannounce_enable,
            "reannounce_limit": self._reannounce_limit,
            "reannounce_times": self._reannounce_times,
            "reannounce_interval": self._reannounce_interval,
            "reannounce_delay": self._reannounce_delay,
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件设置表单：
        第一行：启用插件 / 定时扫描下载器新增种子（启用开关 + 执行周期 + 立即扫描一次）；
        第二行：下载器（多选，仅 qBittorrent）/ 站点（多选，按站点筛选）；
        第三行：全局分享率下限 / 全局分享率上限 / 上传速度（KB/s）；
        第四行：按站点单独分享率阈值（站点=下限,上限）；
        第五行：功能说明。
        """
        # 下载器下拉：MoviePilot 已配置并启用的 qBittorrent
        downloader_items = []
        try:
            for conf in (ServiceConfigHelper.get_downloader_configs() or []):
                if not getattr(conf, "enabled", False):
                    continue
                conf_name = getattr(conf, "name", "") or ""
                conf_type = getattr(conf, "type", "") or ""
                if conf_type == "qbittorrent" and conf_name:
                    downloader_items.append({"title": conf_name, "value": conf_name})
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取下载器配置失败：{err}")

        # 站点下拉：与站点管理排序一致（按优先级 pri 升序，同优先级保持原顺序）
        site_items = []
        try:
            from app.helper.sites import SitesHelper
            site_list = [
                site for site in (SitesHelper().get_indexers() or [])
                if site.get("is_active") and str(site.get("name") or "").strip()
            ]
            site_list.sort(key=lambda s: s.get("pri") or 0)
            site_items = [
                {"title": str(site.get("name") or "").strip(), "value": str(site.get("name") or "").strip()}
                for site in site_list
            ]
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取站点配置失败：{err}")

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
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "scan_enable",
                                            "label": "定时扫描下载器新增种子",
                                            "hint": "开启后按下方执行周期自动扫描所选下载器中的新增种子（与上次扫描快照对比）：识别所属站点并按现有档位规则自动限速，未识别站点的种子记录日志（不在筛选范围/档位未达上限的跳过）。",
                                            "persistent-hint": True,
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
                                            "model": "scan_onlyonce",
                                            "label": "立即扫描一次",
                                            "hint": "保存配置后立即扫描一次下载器新增种子（与定时任务共用同一套扫描逻辑），扫描完成后自动复位关闭。",
                                            "persistent-hint": True,
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
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "scan_cron",
                                            "label": "扫描执行周期（cron）",
                                            "placeholder": "0 0 0 ? *",
                                            "hint": "cron 表达式，默认定时扫描下载器新增种子；建议与辅种定时任务保持一致。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "本插件不发送任何通知；扫描结果（新增/未识别/已限速种子）均记录在 MoviePilot 日志中。",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "reannounce_enable",
                                            "label": "新增种子自动宣告",
                                            "hint": "开启后，下载事件或定时扫描发现的新增种子（两者共用同一全局去重列表，同一种子只宣告一次）自动通过 qBittorrent 接口重新宣告（reannounce）；仅对「低于下限（不限速）」站点的种子宣告。",
                                            "persistent-hint": True,
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
                                            "model": "reannounce_limit",
                                            "label": "全局总宣告种子数",
                                            "placeholder": "例如 5",
                                            "type": "number",
                                            "min": 1,
                                            "step": 1,
                                            "hide-spin-buttons": True,
                                            "hint": "同一时间多个新增种子时，所有种子都加入宣告列表，但同一批次时间窗口（约 10 秒）内最多宣告该数量个种子；其余加入列表跳过宣告，之后任何来源再次发现也不会宣告。",
                                            "persistent-hint": True,
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
                                            "model": "reannounce_times",
                                            "label": "每个种子宣告次数",
                                            "placeholder": "例如 15",
                                            "type": "number",
                                            "min": 1,
                                            "step": 1,
                                            "hide-spin-buttons": True,
                                            "hint": "每个种子累计重新宣告的次数（详情面板的宣告进度 = 已宣告次数/总次数）。",
                                            "persistent-hint": True,
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
                                            "model": "reannounce_interval",
                                            "label": "宣告间隔（秒）",
                                            "placeholder": "例如 330",
                                            "type": "number",
                                            "min": 10,
                                            "step": 1,
                                            "hide-spin-buttons": True,
                                            "hint": "同一个种子两次宣告之间的间隔秒数（与刷流插件默认一致 330 秒）。",
                                            "persistent-hint": True,
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
                                            "model": "reannounce_delay",
                                            "label": "首次宣告延迟（秒）",
                                            "placeholder": "例如 180",
                                            "type": "number",
                                            "min": 0,
                                            "step": 1,
                                            "hide-spin-buttons": True,
                                            "hint": "种子加入宣告队列后到首次宣告的延迟秒数，默认 180 秒（与刷流插件一致）。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "宣告对象为「低于下限（🔻 不限速）」站点的种子；所有新增种子先加入全局去重列表（下载事件与定时扫描共用，跨会话持久化），同一种子只登记一次、只参与一次宣告流程。详情面板可查看当前宣告种子与宣告进度。",
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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloaders",
                                            "label": "下载器",
                                            "items": downloader_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "请选择 MoviePilot 中已配置的 qBittorrent 下载器；留空时不会修改任何下载器。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "sites",
                                            "label": "站点（按站点筛选）",
                                            "items": site_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "留空表示管理所有活动站点；勾选站点后仅管理所选站点。",
                                            "persistent-hint": True,
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
                                            "model": "ratio_lower_limit",
                                            "label": "分享率下限",
                                            "placeholder": "分享率 <= 下限时取消该站种子限速",
                                            "type": "number",
                                            "min": 0.1,
                                            "step": 0.1,
                                            "hint": "正数（>0），最多 1 位小数。分享率小于等于该值时取消上传限速，并保持到分享率超过上限。",
                                            "persistent-hint": True,
                                            "onKeydown": "function (e) { if (e.key === '-') { e.preventDefault(); } }",
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
                                            "model": "ratio_upper_limit",
                                            "label": "分享率上限",
                                            "placeholder": "分享率 > 上限时恢复限速",
                                            "type": "number",
                                            "min": 0.1,
                                            "step": 0.1,
                                            "hint": "正数（>0），最多 1 位小数，需大于等于下限。分享率大于该值时新增上传限速，并保持到分享率小于等于下限（滞回防波动）。",
                                            "persistent-hint": True,
                                            "onKeydown": "function (e) { if (e.key === '-') { e.preventDefault(); } }",
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
                                            "model": "upload_limit",
                                            "label": "上传速度（KB/s）",
                                            "placeholder": "例如 2000；qB 全局上传限速更低时采用全局值；0 表示不做限速处理",
                                            "type": "number",
                                            "min": 0,
                                            "step": 1,
                                            "hide-spin-buttons": True,
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
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "site_confs",
                                            "label": "按站点单独分享率阈值",
                                            "placeholder": "一行一个，例如：\n馒头=0.8,2.0\nHDChina=1.0,3.5",
                                            "rows": 3,
                                            "auto-grow": True,
                                            "clearable": True,
                                            "hint": "格式：站点名称=分享率下限,分享率上限（正数>0，最多 1 位小数，上限需 >= 下限）。对应站点使用单独阈值；未配置或无法识别站点时回退使用全局分享率下限/上限。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
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
                                            "text": "数据来源：站点账号分享率由「站点数据统计」插件抓取，并通过分享率刷新事件（SiteRefreshed）同步到本插件内部状态。请提前安装并启用该插件。下载种子时本插件直接读取内部维护的站点档位状态判定是否限速（档位达到上限时限速，否则不操作）。",
                                        },
                                    }
                                ],
                            }
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
                                            "type": "error",
                                            "variant": "tonal",
                                            "text": "档位带滞回：分享率 <= 下限 -> 🔻 低于下限，取消该站所有种子的上传速度上限，并保持到分享率 > 上限；分享率 > 上限 -> 🔺 达到上限，新增上传限速，并保持到分享率 <= 下限；中间区间 -> ⏸ 保持现状，不调用限速接口（防止限速波动）。保存配置时仅在上传速度、阈值（导致档位变化）或站点/下载器范围变化时才调用接口调整限速。取消限速仅发生在运行中「分享率 <= 下限」档位：停用或卸载插件、移除下载器均不会取消任何种子的上传限速，种子保持插件运行期设置的上传限速不变。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], self._current_config()

    def get_page(self) -> List[dict]:
        """
        详情面板：
        1. 站点状态表——每个站点的账号分享率与档位状态（下载种子时判定限速的数据来源）；
        2. 新增种子宣告表——当前宣告中的种子及其宣告进度（已宣告次数/总次数）。
        """
        if not self._enabled:
            return [{'component': 'div', 'text': '插件未启用', 'props': {'class': 'text-center'}}]

        # ---- 站点状态表 ----
        site_rows = []
        for name in list(self._site_stats.keys()):
            stats = self._site_stats.get(name) or {}
            ratio = stats.get("ratio")
            lower = stats.get("lower")
            upper = stats.get("upper")
            state = stats.get("state") or self._STATE_UNKNOWN
            ratio_text = f"{ratio:g}" if ratio is not None else "—"
            threshold_text = f"{lower:g} - {upper:g}" if lower is not None and upper is not None else "—"
            updated = str(stats.get("updated") or "").strip() or "—"
            limited = stats.get("limited") or 0
            total = stats.get("total") or 0
            site_rows.append({
                "name": name,
                "ratio": ratio_text,
                "updated": updated,
                "threshold": threshold_text,
                "state": self._STATE_LABELS.get(state, state),
                "count": f"{limited}/{total}" if total else f"{limited}",
            })

        site_vrow = None
        if site_rows:
            site_trs = [
                {
                    'component': 'tr',
                    'props': {'class': 'text-sm'},
                    'content': [
                        {'component': 'td', 'props': {'class': 'whitespace-nowrap break-keep text-high-emphasis'}, 'text': row["name"]},
                        {'component': 'td', 'text': row["ratio"]},
                        {'component': 'td', 'props': {'class': 'whitespace-nowrap'}, 'text': row["updated"]},
                        {'component': 'td', 'text': row["threshold"]},
                        {'component': 'td', 'text': row["state"]},
                        {'component': 'td', 'text': row["count"]},
                    ]
                } for row in site_rows
            ]
            site_vrow = {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {'hover': True},
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '站点名称'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '账号分享率'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '数据日期'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '阈值（下限-上限）'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '档位状态'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '已限速种子'},
                                        ]
                                    },
                                    {'component': 'tbody', 'content': site_trs}
                                ]
                            }
                        ]
                    }
                ]
            }

        # ---- 新增种子宣告表（当前宣告中的种子与宣告进度） ----
        announce_rows = []
        now = time.time()
        for service_name in sorted(self._reannounce_progress.keys()):
            prog = self._reannounce_progress.get(service_name) or {}
            for torrent_hash, p in sorted(prog.items()):
                total = p.get("total") or 1
                done = p.get("done") or 0
                next_ts = p.get("next_ts") or 0
                wait_sec = max(int(next_ts - now), 0) if next_ts > now else 0
                if done <= 0:
                    status = f"等待首次宣告（约 {wait_sec} 秒后）" if wait_sec else "即将宣告"
                elif done < total:
                    status = f"宣告中（约 {wait_sec} 秒后下次宣告）"
                else:
                    status = "宣告完成"
                announce_rows.append({
                    "name": p.get("name") or torrent_hash,
                    "site": p.get("site") or "—",
                    "progress": f"{done}/{total}",
                    "status": status,
                })

        announce_vrow = None
        if announce_rows:
            announce_trs = [
                {
                    'component': 'tr',
                    'props': {'class': 'text-sm'},
                    'content': [
                        {'component': 'td', 'props': {'class': 'whitespace-nowrap break-keep text-high-emphasis'}, 'text': row["name"]},
                        {'component': 'td', 'text': row["site"]},
                        {'component': 'td', 'text': row["progress"]},
                        {'component': 'td', 'text': row["status"]},
                    ]
                } for row in announce_rows
            ]
            announce_vrow = {
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
                                    'text': f"新增种子自动宣告：当前宣告中 {len(announce_rows)} 个种子；"
                                            f"全局宣告种子数上限为每批次 {self._reannounce_limit} 个，"
                                            "仅对「🔻 低于下限（不限速）」站点的种子宣告；宣告进度为每个种子已宣告次数/总宣告次数。",
                                },
                            },
                            {
                                'component': 'VTable',
                                'props': {'hover': True},
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '种子名称'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '站点'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '宣告进度'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '状态'},
                                        ]
                                    },
                                    {'component': 'tbody', 'content': announce_trs}
                                ]
                            }
                        ]
                    }
                ]
            }

        result = []
        if site_vrow:
            result.append(site_vrow)
        if announce_vrow:
            result.append(announce_vrow)
        if not result:
            return [{'component': 'div', 'text': '暂无站点分享率数据（等待站点数据统计刷新）', 'props': {'class': 'text-center'}}]
        return result

    # ---------------------------------------------------------------- 工具方法

    def _normalize_site_confs(self, value: Any) -> Tuple[Dict[str, Tuple[float, float]], str]:
        """
        解析并规范化站点单独分享率阈值文本。

        表单使用每行「站点名称=分享率下限,分享率上限」格式；兼容冒号/中文冒号分隔。
        兼容旧版单值「站点=下限」格式：仅配置下限，上限继承全局值（内部以 0 标记）。
        站点名称按小写匹配，重复站点以最后一项为准，非法项会被忽略。
        """
        confs: Dict[str, Tuple[float, float]] = {}
        labels: Dict[str, str] = {}
        invalid_count = 0

        for raw_line in str(value or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            separator = next((item for item in ("=", "：", ":") if item in line), "")
            if not separator:
                invalid_count += 1
                continue
            name, rest = line.split(separator, 1)
            name = str(name).strip()
            parts = [p.strip() for p in re.split(r"[,，\-～~—]", rest) if p.strip()]
            if not name or not parts:
                invalid_count += 1
                continue
            if len(parts) == 1:
                # 旧版单值格式：仅配置下限，上限继承全局
                lower = self._to_ratio(parts[0], 0.0)
                upper = 0.0
            elif len(parts) == 2:
                lower = self._to_ratio(parts[0], 0.0)
                upper = self._to_ratio(parts[1], 0.0)
            else:
                invalid_count += 1
                continue
            if lower <= 0 or upper < 0:
                invalid_count += 1
                continue
            if upper > 0 and upper < lower:
                logger.warning(f"{self.LOG_TAG}站点 {name} 分享率上限小于下限，已忽略该行：{line}")
                invalid_count += 1
                continue
            key = name.lower()
            confs[key] = (lower, upper)
            labels[key] = name

        if invalid_count:
            logger.warning(f"{self.LOG_TAG}站点单独分享率阈值中有 {invalid_count} 项格式无效，已忽略")
        normalized_text = "\n".join(
            f"{labels[key]}={lower},{upper}" if upper > 0 else f"{labels[key]}={lower}"
            for key, (lower, upper) in sorted(confs.items())
        )
        return confs, normalized_text

    @staticmethod
    def _normalize_config_list(value: Any) -> List[str]:
        """将配置项规范化为去重后的字符串列表，兼容旧版单个字符串配置。"""
        if value is None:
            return []
        if isinstance(value, str):
            raw = [value]
        else:
            try:
                raw = list(value)
            except TypeError:
                raw = [value]
        items = []
        for item in raw:
            item = str(item or "").strip()
            if item and item not in items:
                items.append(item)
        return items

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        """安全转换为整数：仅接受整数或整数字符串，拒绝小数与科学计数法，转换失败时返回默认值。"""
        if value is None or isinstance(value, bool):
            return default
        try:
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value) if value.is_integer() else default
            text = str(value).strip()
            if not text or any(c in text for c in ".eE"):
                return default
            return int(text)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_ratio(value: Any, default: float = 1.0) -> float:
        """安全转换分享率阈值为正浮点数（最多 1 位小数）；非法值回退默认值。"""
        if value is None or isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number:  # NaN
            return default
        number = round(number, 1)
        if number <= 0:
            return default
        return number

    @staticmethod
    def _format_limit(limit: float) -> str:
        """格式化限速显示。"""
        if limit <= 0:
            return "不限速"
        value = float(limit)
        display = int(value) if value.is_integer() else round(value, 3)
        return f"{display} KB/s"