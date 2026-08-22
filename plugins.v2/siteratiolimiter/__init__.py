"""
站点分享率上传限速插件（MoviePilot V2）

基于「站点账号分享率」与配置的分享率下限自动管理 qBittorrent 种子的上传限速。

功能：
1. 全局设置分享率下限，并可对每个站点单独设置（未配置的站点回退全局值）；
2. 插件内部维护每个站点的分享率档位状态（🔻 低于下限 / ✅ 正常 / ⚪ 无数据），
   档位仅两档：分享率 <= 下限为「低于下限」，其余一律为「正常」；
   正常站点的种子全部限速，低于下限的站点取消限速；
3. 该状态由「站点数据统计」插件的分享率刷新事件（SiteRefreshed，全站刷新完成 site_id == *）更新，
   启用插件时做一次静默基线（不通知）；
4. 监听下载种子事件（DownloadAdded）：直接从插件内部维护的站点状态读取当前档位，
   站点档位为「正常」时立即限制该种子上传速度（KB/s），否则不做任何操作
   （不在下载时实时查询 MoviePilot 站点数据，数据来源为插件维护的站点状态）；
5. 站点分享率刷新事件：
   - 档位降到「低于下限」 -> 取消该站所有种子在 qBittorrent 中的上传速度上限；
   - 档位为「正常」 -> 将该站当前未限速的种子批量设置上传限速；
   - 站点档位发生变化时才发送通知（分享率过低 / 恢复限速）；
   - 保存配置时：仅当修改了上传限速大小、或修改阈值导致档位变化、或下载器/站点范围变化时
     才调用接口调整限速，否则只刷新状态与统计，不产生限速 API 调用；
6. 详情面板：展示每个配置站点的账号分享率与档位状态（供下载时判定），
   以及该站已限速种子数，不再展示种子级明细；
7. 停用/卸载时自动将本插件限速过的种子恢复为不限速（跨会话持久化 + 失败兜底重试）。
"""

import datetime
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.downloader import DownloaderHelper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType, MessageChannel


class SiteRatioLimiter(_PluginBase):
    """
    站点分享率上传限速插件。

    以「站点账号分享率」与配置的分享率下限为决策依据，事件驱动地管理
    qBittorrent 种子的上传限速，并在详情面板展示每个站点的分享率与档位状态。
    """

    # 插件名称
    plugin_name = "站点分享率上传限速"
    # 插件描述
    plugin_desc = "基于站点账号分享率与分享率下限自动管理 qBittorrent 种子上传限速：档位仅两档，低于下限取消限速、其余正常站点全部限速；下载种子时按插件维护的站点档位状态限速；档位变化时通知。"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.2.0"
    # 插件作者
    plugin_author = "Guo1"
    # 作者主页
    author_url = "https://github.com/Guo1"
    # 插件配置项ID前缀
    plugin_config_prefix = "siteratiolimiter_"
    # 加载顺序
    plugin_order = 31
    # 可使用的用户级别
    auth_level = 1

    LOG_TAG = "[站点分享率上传限速] "

    # ---- 站点档位状态（仅两档 + 无数据） ----
    _STATE_LOW = "low"          # 分享率 <= 下限：取消限速
    _STATE_NORMAL = "normal"    # 分享率 > 下限：全部限速
    _STATE_UNKNOWN = "unknown"  # 无分享率数据

    _STATE_LABELS = {
        "low": "🔻 低于下限",
        "normal": "✅ 正常",
        "unknown": "⚪ 无数据",
    }

    # ---- 配置项默认值 ----
    _enabled = False
    # 已选择的通知渠道类型（如 telegram / wechat），空表示不发送通知
    _notify_channel: List[str] = []
    # 已选择的下载器名称（仅 qBittorrent）
    _downloaders: List[str] = []
    # 已选择的站点名称，为空表示管理所有活动站点
    _sites: List[str] = []
    # 全局分享率下限（正数，最多 1 位小数）；档位：<= 下限为低于下限，否则正常
    _ratio_lower = 1.0
    # 按站点单独阈值文本（每行「站点=下限」）与解析结果 {站名小写: 下限}
    _site_conf_text = ""
    _site_confs: Dict[str, float] = {}
    # 上传速度 KB/s，0 表示不做限速处理
    _upload_limit = 2000

    # ---- 运行时状态 ----
    _retry_scheduler = None
    _retry_attempts = 0
    _MAX_RESTORE_RETRY = 60
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
    # 本插件本轮/会话内限速的种子 {下载器名称: {种子Hash}}，停用/卸载时必须恢复的种子 {下载器名称: {种子Hash}}
    _limited_hashes: Dict[str, Set[str]] = {}
    _restore_hashes: Dict[str, Set[str]] = {}

    # 持久化数据键
    _RESTORE_DATA_KEY = "restore_hashes"
    _SITE_STATES_KEY = "site_states"
    _SIG_KEY = "config_signature"

    # 通知渠道类型（MoviePilot 通知配置的 type）-> MessageChannel 枚举
    _NOTIFY_TYPE_MAP = {
        "telegram": MessageChannel.Telegram,
        "wechat": MessageChannel.Wechat,
        "feishu": MessageChannel.Feishu,
        "wechatclawbot": MessageChannel.WechatClawBot,
        "slack": MessageChannel.Slack,
        "discord": MessageChannel.Discord,
        "synologychat": MessageChannel.SynologyChat,
        "vocechat": MessageChannel.VoceChat,
        "webpush": MessageChannel.WebPush,
        "qqbot": MessageChannel.QQ,
    }

    # ---------------------------------------------------------------- 生命周期

    def init_plugin(self, config: dict = None):
        """
        读取配置、构建站点映射、加载持久化状态并在启用时做一次静默基线。

        保存配置时的限速动作控制（避免无意义的限速 API 调用）：
        - 仅当「首次启用 / 上传限速大小变化 / 分享率下限阈值变化 / 下载器或站点范围变化」之一发生时，
          恢复旧下载器的限速并按新档位调用接口调整限速；
        - 否则只刷新站点分享率、档位状态与统计（面板数据），不调用任何限速接口。
        """
        was_enabled = self._enabled
        old_downloaders = self._downloaders or []
        self._stop_restore_retry(wait=True)

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify_channel = self._normalize_channels(config.get("notify_channel"))
        self._downloaders = self._normalize_config_list(config.get("downloaders"))
        self._sites = self._normalize_config_list(config.get("sites"))
        self._ratio_lower = self._to_ratio(config.get("ratio_lower_limit"), 1.0)
        self._site_confs, self._site_conf_text = self._normalize_site_confs(config.get("site_confs"))
        self._upload_limit = max(self._to_int(config.get("upload_limit"), 2000), 0)

        # 站点映射（域名/名称）只构建一次
        self._site_domains = self._load_site_domains()
        self._site_names = {name.lower(): name for name in self._site_domains.values() if name}

        # 规范化持久化：修正历史遗留的非法配置，避免非法值回显到表单
        try:
            if self._current_config() != config:
                self.update_config(self._current_config())
        except Exception:
            pass

        # 加载跨会话持久化的待恢复记录、站点档位状态与上次生效的配置签名
        self._restore_hashes = self._load_set_map(self._RESTORE_DATA_KEY)
        try:
            raw_states = self.get_data(self._SITE_STATES_KEY) or {}
            self._site_states = {
                str(name): str(state)
                for name, state in raw_states.items()
                if name and state in (self._STATE_LOW, self._STATE_NORMAL, self._STATE_UNKNOWN)
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

        # 每次重新初始化时清空会话级记录；待恢复记录与站点档位状态保留
        self._limited_hashes = {}
        self._site_stats = {}
        self._site_ratios = {}
        self._site_ratio_times = {}

        # 停用插件时：恢复旧配置下所有已限速的种子为不限速（不再走基线）
        if was_enabled and not self._enabled:
            self._restore_limits(downloaders=old_downloaders)
            logger.info(f"{self.LOG_TAG}插件已停用，已恢复本插件限速过的种子为不限速")
            self._save_set_map(self._RESTORE_DATA_KEY, self._restore_hashes)
            self._start_restore_retry()
            return

        if not self._enabled:
            self._save_set_map(self._RESTORE_DATA_KEY, self._restore_hashes)
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

        # 从旧选择中移除的下载器：先恢复其限速（返回恢复权限）
        if old_sig and downloaders_changed:
            removed = [name for name in old_downloaders if name not in self._downloaders]
            if removed:
                self._restore_limits(downloaders=removed)

        # 首次启用或相关配置变化：以静默基线按新档位执行限速/取消限速动作；
        # 上传限速大小变化时强制按新值刷新已限速种子
        if first_run or apply_changed:
            self._process_site_ratios(notify=False, apply=True, force=bool(upload_changed))
            logger.info(
                f"{self.LOG_TAG}已完成站点状态基线构建（{'首次' if first_run else '配置变化'}"
                f"{'，上传限速大小变化，已按新值刷新' if upload_changed else ''}）"
            )
        else:
            # 配置未实质变化：只刷新状态与统计（不调用限速接口）
            self._process_site_ratios(notify=False, apply=False)
            logger.info(f"{self.LOG_TAG}配置未发生实质变化，仅刷新站点分享率/档位/统计，不调整限速")

        # 记录本次生效的配置签名
        try:
            self.save_data(self._SIG_KEY, new_sig)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}持久化配置签名失败：{err}")

        # 持久化待恢复记录（恢复失败项保留）
        self._save_set_map(self._RESTORE_DATA_KEY, self._restore_hashes)

    def stop_service(self):
        """停止插件；停用或卸载插件时自动将已限速种子恢复为不限速。"""
        try:
            self._restore_limits()
        except Exception as err:
            logger.error(f"{self.LOG_TAG}恢复上传不限速失败：{err}")
        self._save_set_map(self._RESTORE_DATA_KEY, self._restore_hashes)
        self._start_restore_retry()

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
        站点档位为「正常」（分享率 > 下限）时立即限制该种子的上传速度，否则不做任何操作。
        """
        if not self._enabled:
            return
        if not event:
            logger.error(f"{self.LOG_TAG}下载事件数据为空")
            return
        event_data = event.event_data or {}
        download_hash = str(event_data.get("hash") or "").strip()
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
        if state != self._STATE_NORMAL:
            ratio = (stats or {}).get("ratio")
            ratio_text = f"{ratio:g}" if ratio is not None else "无数据"
            logger.info(
                f"{self.LOG_TAG}站点 {site_name} 档位状态：{self._STATE_LABELS.get(state or self._STATE_UNKNOWN)}"
                f"（分享率 {ratio_text}），种子 [{download_hash}] 不做限速处理"
            )
            return

        # 站点档位为正常（分享率高于下限）：直接限制该种子上传速度
        if self._upload_limit <= 0:
            logger.info(f"{self.LOG_TAG}上传速度为 0（不做限速处理），种子 [{download_hash}] 跳过")
            return
        self._limit_torrent_by_hash(download_hash, site_name)

    @eventmanager.register(EventType.SiteRefreshed)
    def on_site_refreshed(self, event: Event = None):
        """
        站点分享率数据刷新事件：仅在全站刷新完成（site_id == *）时处理，
        刷新内部站点状态并按新档位执行批量限速/取消限速，档位变化时通知。
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
        self._process_site_ratios(notify=True)

    # ---------------------------------------------------------------- 核心流程

    def _process_site_ratios(self, notify: bool = True, apply: bool = True, force: bool = False):
        """
        刷新站点账号分享率快照，重算每个管理站点的档位状态（仅两档）：
        - 分享率 <= 下限 -> 🔻 低于下限：取消该站种子的上传限速；
        - 分享率 > 下限  -> ✅ 正常：将该站未限速种子批量补限速（force=True 时按新值刷新已限速种子）；
        - 档位发生变化（且非首次基线）时按变化方向发送通知；
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
            lower = self._site_thresholds(name)
            ratio = self._site_ratios.get(name)
            if ratio is None:
                ratio = self._site_ratios.get(name.lower())
            updated = self._site_ratio_times.get(name) or self._site_ratio_times.get(name.lower())
            stats = self._site_stats.setdefault(name, {})
            stats["lower"] = lower
            stats["ratio"] = ratio
            stats["updated"] = updated
            if ratio is None:
                stats["state"] = self._STATE_UNKNOWN
                new_states[name] = self._site_states.get(name, self._STATE_UNKNOWN)
                logger.info(f"{self.LOG_TAG}站点 {name} 无分享率数据（等待站点数据统计），保持原档位")
                continue

            # 仅两档：<= 下限为低于下限，其余全部为正常
            if ratio <= lower:
                new_state = self._STATE_LOW
            else:
                new_state = self._STATE_NORMAL
            stats["state"] = new_state

            prev_state = self._site_states.get(name)
            # 仅当档位发生变化且不是首次基线（无历史状态）时通知
            if notify and prev_state and prev_state != new_state and prev_state != self._STATE_UNKNOWN:
                if new_state == self._STATE_LOW:
                    self._send_message(self._build_low_text(name, ratio, lower))
                elif new_state == self._STATE_NORMAL:
                    self._send_message(self._build_normal_text(name, ratio, lower))
            new_states[name] = new_state
            logger.info(
                f"{self.LOG_TAG}站点 {name} 分享率 {ratio:g}（下限 {lower:g}）"
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
            self._save_set_map(self._RESTORE_DATA_KEY, self._restore_hashes)
            return
        for service_name, service_info in services.items():
            downloader = service_info.instance
            downloader_type = getattr(service_info, "type", "")
            by_site, _ = self._torrents_by_site(service_name, downloader)
            # 执行动作：仅当 apply=True 时才调用限速/取消限速接口
            if apply:
                for name in managed_names:
                    hit_site = next((s for s in by_site if s.lower() == name.lower()), None)
                    site_torrents = by_site.get(hit_site, []) if hit_site else []
                    state = self._site_states.get(name)
                    if state == self._STATE_LOW:
                        canceled = self._cancel_site_limits(service_name, downloader, name, site_torrents)
                        summary.append(f"{service_name}：站点 {name} 取消限速 {canceled} 个种子")
                    elif state == self._STATE_NORMAL:
                        limit = self._effective_upload_limit(downloader, downloader_type, self._upload_limit)
                        if limit <= 0:
                            summary.append(f"{service_name}：站点 {name} 上传速度为 0，不执行限速")
                        else:
                            applied = self._apply_site_limits(
                                service_name, downloader, name, site_torrents, limit, force=force
                            )
                            summary.append(f"{service_name}：站点 {name} 限速 {applied} 个种子")
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
            # 兜底重试「限速过但恢复失败」的种子
            if apply:
                self._retry_stuck_restores(service_name, downloader)

        if summary:
            logger.info(f"{self.LOG_TAG}" + "；".join(summary))
        self._save_set_map(self._RESTORE_DATA_KEY, self._restore_hashes)

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
                self._restore_hashes.setdefault(service_name, set()).add(download_hash)
                # 同步站点统计中的限速计数
                stats = self._site_stats.setdefault(site_name, {})
                stats["limited"] = stats.get("limited", 0) + 1
                stats["total"] = stats.get("total", 0) + 1
                logger.info(f"{self.LOG_TAG}[{service_name}] 种子 [{self._torrent_name(target) or download_hash}] 所属站点 {site_name} 档位正常（分享率高于下限），已限速 {self._format_limit(effective_limit)}")
                self._save_set_map(self._RESTORE_DATA_KEY, self._restore_hashes)
                return True
            except Exception as err:
                logger.error(f"{self.LOG_TAG}[{service_name}] 定位种子 [{download_hash}] 失败：{err}")
        logger.warning(f"{self.LOG_TAG}种子 [{download_hash}] 未在已选下载器中发现（可能尚未添加完成），跳过")
        return False

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
                self._restore_hashes.get(service_name, set()).discard(torrent_hash)
                canceled += 1
                logger.info(f"{self.LOG_TAG}[{service_name}] 站点 {site_name} 分享率低于下限，已取消种子 [{torrent_hash}] 的上传限速")
            else:
                # 恢复失败：保留待恢复记录，后续兜底重试
                self._restore_hashes.setdefault(service_name, set()).add(torrent_hash)
        return canceled

    def _apply_site_limits(self, service_name: str, downloader: Any, site_name: str, torrents: List[Any], limit: float, force: bool = False) -> int:
        """
        对指定站点「正常」档位的种子设置上传限速：
        - force=False（常规）：仅对当前未限速（up_limit=0）的种子设置限速，已限速的种子跳过；
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
                    continue
            else:
                # 常规：已限速（含外部设置的限速）不重复干预
                if current_kb > 0:
                    continue
            try:
                ok = downloader.change_torrent(hash_string=torrent_hash, upload_limit=target)
            except Exception as err:
                ok = False
                logger.error(f"{self.LOG_TAG}[{service_name}] 站点 {site_name} 种子 [{torrent_hash}] 设置上传限速失败：{err}")
            if ok:
                limited.add(torrent_hash)
                self._restore_hashes.setdefault(service_name, set()).add(torrent_hash)
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

    def _site_thresholds(self, site_name: str) -> float:
        """返回站点分享率下限；未配置单独阈值的站点使用全局值。"""
        conf = self._site_confs.get(str(site_name or "").strip().lower())
        if conf:
            return conf
        return self._ratio_lower

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

    # ---------------------------------------------------------------- 通知

    def _build_low_text(self, site_name: str, ratio: float, lower: float) -> str:
        return (f"**{site_name}** 分享率过低（当前 **{ratio:g}** ≤ 下限 **{lower:g}**），"
                f"已取消该站所有种子的上传速度上限。")

    def _build_normal_text(self, site_name: str, ratio: float, lower: float) -> str:
        return (f"**{site_name}** 分享率已恢复到下限以上（当前 **{ratio:g}** > 下限 **{lower:g}**），"
                f"已对该站未限速的种子新增上传限速。")

    def _send_message(self, text: str) -> bool:
        """按配置的通知渠道发送消息。"""
        channels = self._normalize_channels(self._notify_channel)
        if not channels:
            return False
        sent = False
        for channel in channels:
            notify_channel = self._NOTIFY_TYPE_MAP.get(channel)
            if not notify_channel:
                continue
            try:
                self.post_message(
                    channel=notify_channel,
                    title="【站点分享率上传限速】",
                    text=text,
                    link=settings.MP_DOMAIN(f"#/plugins?tab=installed&id={self.__class__.__name__}"),
                )
                sent = True
            except Exception as err:
                logger.error(f"{self.LOG_TAG}发送通知失败（{channel}）：{err}")
        return sent

    # ---------------------------------------------------------------- 恢复与调度

    def _restore_limits(self, downloaders: Optional[List[str]] = None):
        """将本插件限速过的种子恢复为不限速（停用/卸载时调用）。"""
        if downloaders is None:
            names = list(dict.fromkeys((self._downloaders or []) + list(self._restore_hashes.keys())))
        else:
            names = downloaders
        services = self._get_services(names)
        if not services:
            return
        for service_name, service_info in services.items():
            downloader = service_info.instance
            hashes = self._restore_hashes.get(service_name) or set()
            failed_hashes = set()
            for torrent_hash in hashes:
                try:
                    if not downloader.change_torrent(hash_string=torrent_hash, upload_limit=0):
                        failed_hashes.add(torrent_hash)
                        logger.error(f"{self.LOG_TAG}[{service_name}] 恢复种子 [{torrent_hash}] 上传限速失败：下载器返回失败")
                        continue
                    logger.info(f"{self.LOG_TAG}[{service_name}] 种子 [{torrent_hash}] 已恢复不限速")
                except Exception as err:
                    failed_hashes.add(torrent_hash)
                    logger.error(f"{self.LOG_TAG}[{service_name}] 恢复种子 [{torrent_hash}] 上传限速失败：{err}")
            if failed_hashes:
                self._restore_hashes[service_name] = failed_hashes
            else:
                self._restore_hashes.pop(service_name, None)
            self._limited_hashes[service_name] = set()

    def _start_restore_retry(self):
        """存在待恢复记录时启动兜底恢复重试任务（停用/卸载状态下的生命周期保障）。"""
        try:
            if getattr(self, "_retry_scheduler", None) and self._retry_scheduler.running:
                return
            if not any(self._restore_hashes.values()):
                return
            self._retry_attempts = 0
            self._retry_scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._retry_scheduler.add_job(
                func=self._restore_retry_job,
                trigger="interval",
                seconds=60,
                max_instances=1,
                name="站点分享率上传限速-兜底恢复",
            )
            self._retry_scheduler.start()
            logger.info(
                f"{self.LOG_TAG}存在 {sum(len(v) for v in self._restore_hashes.values())} 个待恢复种子，"
                "已启动兜底恢复重试任务"
            )
        except Exception as err:
            logger.error(f"{self.LOG_TAG}启动兜底恢复重试任务失败：{err}")

    def _stop_restore_retry(self, wait: bool = False):
        """停止兜底恢复重试任务。"""
        try:
            if getattr(self, "_retry_scheduler", None):
                if self._retry_scheduler.running:
                    self._retry_scheduler.shutdown(wait=wait)
                self._retry_scheduler = None
            self._retry_attempts = 0
        except Exception as err:
            logger.error(f"{self.LOG_TAG}停止兜底恢复重试任务失败：{err}")

    def _restore_retry_job(self):
        """兜底恢复重试任务：每轮尝试恢复待恢复记录，全部成功后自动停止。"""
        try:
            self._restore_limits()
        except Exception as err:
            logger.error(f"{self.LOG_TAG}兜底恢复上传不限速失败：{err}")
        self._save_set_map(self._RESTORE_DATA_KEY, self._restore_hashes)
        if not any(self._restore_hashes.values()):
            self._stop_restore_retry()
            return
        self._retry_attempts += 1
        if self._retry_attempts % self._MAX_RESTORE_RETRY == 0:
            logger.warning(
                f"{self.LOG_TAG}待恢复种子仍有限速未恢复（已重试 {self._retry_attempts} 次），"
                "已继续定时重试，下载器重连后将自动恢复，请检查下载器连接"
            )

    def _retry_stuck_restores(self, service_name: str, downloader: Any):
        """启用状态下兜底重试「限速过但恢复失败」的种子恢复不限速。"""
        pending = set(self._restore_hashes.get(service_name) or set()) - set(
            self._limited_hashes.get(service_name) or set()
        )
        if not pending:
            return
        succeeded = set()
        for torrent_hash in pending:
            try:
                if not downloader.change_torrent(hash_string=torrent_hash, upload_limit=0):
                    logger.error(f"{self.LOG_TAG}[{service_name}] 兜底恢复种子 [{torrent_hash}] 上传限速失败：下载器返回失败")
                    continue
                succeeded.add(torrent_hash)
                logger.info(f"{self.LOG_TAG}[{service_name}] 种子 [{torrent_hash}] 已兜底恢复不限速")
            except Exception as err:
                logger.error(f"{self.LOG_TAG}[{service_name}] 兜底恢复种子 [{torrent_hash}] 上传限速失败：{err}")
        if succeeded:
            self._restore_hashes.get(service_name, set()).difference_update(succeeded)

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
            # 站点单独阈值确定性排序：{站点小写: 下限}
            "site_confs": sorted(f"{key}={value}" for key, value in self._site_confs.items()),
            "downloaders": sorted(self._downloaders),
            "sites": sorted(self._sites),
        }

    def _current_config(self) -> Dict[str, Any]:
        """返回当前配置，供表单回填。"""
        return {
            "enabled": self._enabled,
            "notify_channel": self._notify_channel,
            "downloaders": self._downloaders,
            "sites": self._sites,
            "ratio_lower_limit": self._ratio_lower,
            "site_confs": self._site_conf_text,
            "upload_limit": self._upload_limit,
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件设置表单：
        第一行：启用插件 / 发送通知（多选渠道）；
        第二行：下载器（多选，仅 qBittorrent）/ 站点（多选，按站点筛选）；
        第三行：全局分享率下限 / 上传速度（KB/s）；
        第四行：按站点单独分享率下限（站点=下限）；
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

        # 通知渠道下拉：MoviePilot 已启用渠道的类型去重
        notify_items = []
        try:
            seen_types = set()
            for conf in (ServiceConfigHelper.get_notification_configs() or []):
                if not getattr(conf, "enabled", False):
                    continue
                conf_type = getattr(conf, "type", "") or ""
                conf_name = getattr(conf, "name", "") or conf_type
                if conf_type and conf_type not in seen_types:
                    seen_types.add(conf_type)
                    notify_items.append({"title": conf_name, "value": conf_type})
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取通知渠道配置失败：{err}")

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
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "notify_channel",
                                            "label": "发送通知",
                                            "items": notify_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "可多选 MoviePilot 系统设置中已配置并启用的通知渠道；留空表示不发送通知。仅在站点分享率档位发生变化时通知（分享率过低 / 恢复限速）。",
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
                                "props": {"cols": 12, "md": 6},
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
                                            "hint": "正数（>0），最多 1 位小数。档位仅两档：站点分享率小于等于该值时取消上传限速，大于该值时全部限速。",
                                            "persistent-hint": True,
                                            "onKeydown": "function (e) { if (e.key === '-') { e.preventDefault(); } }",
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
                                            "label": "按站点单独分享率下限",
                                            "placeholder": "一行一个，例如：\n馒头=0.8\nHDChina=1.0",
                                            "rows": 3,
                                            "auto-grow": True,
                                            "clearable": True,
                                            "hint": "格式：站点名称=分享率下限（正数>0，最多 1 位小数）。对应站点使用单独下限；未配置或无法识别站点时回退使用全局分享率下限。",
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
                                            "text": "数据来源：站点账号分享率由「站点数据统计」插件抓取，并通过分享率刷新事件（SiteRefreshed）同步到本插件内部状态。请提前安装并启用该插件。下载种子时本插件直接读取内部维护的站点档位状态判定是否限速（档位正常时限速，否则不操作）。",
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
                                            "text": "档位仅两档：分享率 <= 下限 -> 🔻 低于下限，取消该站所有种子的上传速度上限；分享率 > 下限 -> ✅ 正常，该站全部种子限速。档位发生变化时才发送通知。保存配置时仅在上传速度、阈值（导致档位变化）或站点/下载器范围变化时才调用接口调整限速。停用或卸载插件时自动恢复本插件限速过的种子上传速度。",
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
        详情面板：站点状态表——展示每个站点的账号分享率与档位状态
        （即下载种子时判定限速的数据来源），不再展示种子明细。
        """
        if not self._enabled:
            return [{'component': 'div', 'text': '插件未启用', 'props': {'class': 'text-center'}}]

        site_names = list(self._site_stats.keys())
        if not site_names:
            return [{'component': 'div', 'text': '暂无站点分享率数据（等待站点数据统计刷新）', 'props': {'class': 'text-center'}}]

        # ---- 站点状态表 ----
        site_rows = []
        for name in site_names:
            stats = self._site_stats.get(name) or {}
            ratio = stats.get("ratio")
            lower = stats.get("lower")
            state = stats.get("state") or self._STATE_UNKNOWN
            ratio_text = f"{ratio:g}" if ratio is not None else "—"
            threshold_text = f"{lower:g}" if lower is not None else "—"
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

        return [
            {
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
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '分享率下限'},
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
        ]

    # ---------------------------------------------------------------- 工具方法

    def _normalize_site_confs(self, value: Any) -> Tuple[Dict[str, float], str]:
        """
        解析并规范化站点单独分享率下限文本。

        表单使用每行「站点名称=分享率下限」格式；兼容冒号/中文冒号分隔。
        兼容旧版「站点=下限,上限」格式：存在两个数值时取第一个作为下限并提示忽略上限。
        站点名称按小写匹配，重复站点以最后一项为准，非法项会被忽略。
        """
        confs: Dict[str, float] = {}
        labels: Dict[str, str] = {}
        invalid_count = 0
        ignored_upper = 0

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
            if len(parts) == 2:
                # 兼容旧版「站点=下限,上限」，仅取下限
                ignored_upper += 1
                parts = parts[:1]
            elif len(parts) > 2:
                invalid_count += 1
                continue
            lower = self._to_ratio(parts[0], 0.0)
            if lower <= 0:
                invalid_count += 1
                continue
            key = name.lower()
            confs[key] = lower
            labels[key] = name

        if ignored_upper:
            logger.info(f"{self.LOG_TAG}站点单独分享率阈值中 {ignored_upper} 行包含旧版上限值，已忽略上限仅采用下限")
        if invalid_count:
            logger.warning(f"{self.LOG_TAG}站点单独分享率阈值中有 {invalid_count} 项格式无效，已忽略")
        normalized_text = "\n".join(
            f"{labels[key]}={lower}" for key, lower in sorted(confs.items())
        )
        return confs, normalized_text

    @staticmethod
    def _normalize_channels(value: Any) -> List[str]:
        """将通知渠道配置规范化为去重后的字符串列表，兼容旧版单个字符串配置。"""
        if value is None:
            return []
        if isinstance(value, str):
            raw = [value]
        else:
            try:
                raw = list(value)
            except TypeError:
                raw = [value]
        channels = []
        for item in raw:
            item = str(item or "").strip()
            if item and item not in channels:
                channels.append(item)
        return channels

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