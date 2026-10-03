"""
空间不足自动删种插件（MoviePilot V2）

定时检查存储剩余空间，空间不足时自动删除 qBittorrent 种子及其文件，直到剩余空间回到停止阈值以上。

核心规则：
1. 触发/停止阈值：剩余空间 < 「触发阈值」（GB，默认 50）时开始清理；
   清理到剩余空间 >= 「停止阈值」（GB，默认 60）时立即停止（每删完一组内容重新检查一次空间，绝不超删）；
2. 删除对象：qBittorrent 种子及对应文件（delete_file=True），
   除「排除路径」命中的种子外全部可删（含手工添加的种子，不限是否 MoviePilot 添加、不限是否已整理）；
3. 删除顺序：按 added_on（添加时间）升序，最旧的内容先删；
4. 排除路径：配置项（多行文本，一行一个关键词），种子的 content_path 或 save_path
   命中任一关键词 -> 永不删除；
5. 保护条件：
   - 「未完成的种子不删」：progress < 1、amount_left > 0 或处于下载类状态时不删（默认开启）；
   - 「做种时长不足 N 小时的不删」：可配置整数小时（默认 0 = 不限制），用 completion_on
     （为 0 时用 added_on）到当前时间计算做种时长，用于防止 H&R 种子被误删；
6. 辅种整组删除：同一 content_path 的多个 hash（同内容的多站点辅种）共享同一份文件，
   删掉文件却只删一个种子会留下报错的残留种子，所以按「整组」处理：
   - 整组都可删时，默认把整组 hash 一次删完（关闭该开关则只删组内最旧的一个）；
   - 组内只要有一个成员受保护（命中排除路径 / 未完成 / 做种时长不足），整组本轮都不删除
     （计入详情页的「整组跳过」），既不会留下残留种子，也不会误删受保护的种子；
7. 预演模式（dry_run）：开启时只写日志 / 更新详情页 / 发通知，不调用任何删除接口，供上线前测试；
8. 「立即运行一次」（onlyonce）：保存配置后立刻执行一轮并自动复位；
9. 通知（notify）：仅在「实际发生清理或发生错误」时发送通知（含删除数量、去重后释放空间、剩余空间）；
   空间充足时不发通知，只记日志；
10. 执行周期：cron 表达式配置项（默认 */30 * * * *），由 MoviePilot 系统调度；
11. 下载器多选（仅处理 qbittorrent 类型），未配置时自动选中唯一一个已启用的 qBittorrent；
12. 详情页：展示监控路径、当前剩余/总量、触发与停止阈值、预演模式状态、上次运行时间与上次结果。

安全护栏（防止误删种子）：
- 每删完一组重新检查剩余空间，达到停止阈值立即停止（绝不超删）；
- 删除后先等文件系统「空间结算」再读剩余空间：qBittorrent 的 delete_torrents(delete_file=True)
  是服务端异步删文件，接口返回时空间尚未归还，直接读「删除瞬间」的剩余空间会把正常删除
  误判成「没有释放空间」（本插件 v1.0.0 的真实事故即由此而来）；
- 停止阈值必须大于触发阈值，配置非法时自动修正为「触发阈值 +10」；
- 未开启「同时删除文件」时不删除任何内容（删种子不删文件无法释放空间，删除毫无意义）；
- 若连续 3 组删除、且「空间结算」后本轮剩余空间的累计增量仍明显不足
  （小于 max(200MB, 该组去重后大小 * 20%)），且本轮累计已尝试释放量达到 2GB
  （媒体库是硬链接、监控路径不在下载文件所在分区等），立即中止本轮，
  并置位跨会话「无释放空间」闭锁：在用户修正配置（改动「同时删除文件」或
  「监控路径」）或空间恢复到触发阈值以上之前，不再自动删除任何内容，
  避免定时任务一轮一轮地把种子删光。闭锁状态在详情页展示；
- 闭锁自愈：置位时记录的剩余空间若已明显小于当前剩余空间（相差 ≥ max(1GB, 当前的 5%)），
  说明此前被判定的「没有释放」其实只是延迟兑现，自动解除暂停并记日志。

磁盘剩余空间以容器内路径（默认 /downloads，对应宿主 /mnt/storage/media/downloads）为准；
路径不存在或无权限时记录 error 日志 + 通知，本轮跳过、不删除任何东西。
"""

import datetime
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo


class SpaceCleaner(_PluginBase):
    """
    空间不足自动删种插件。

    定时检查存储剩余空间，低于触发阈值时按「最旧优先」删除 qBittorrent 种子及文件，
    每删完一组内容重新检查空间，达到停止阈值立即停止；支持排除路径、未完成保护、
    做种时长保护、辅种整组删除与预演模式。
    """

    # 插件名称
    plugin_name = "空间不足自动删种"
    # 插件描述
    plugin_desc = "定时检查存储剩余空间，空间不足时按「最旧优先」自动删除 qBittorrent 种子及文件，直到剩余空间回到停止阈值以上；支持排除路径、未完成种子保护、做种时长保护（防 H&R 误删）、同内容多站点辅种整组删除与预演模式（只记录不删除）。"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "Lyzd1"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "spacecleaner_"
    # 加载顺序
    plugin_order = 32
    # 可使用的用户级别
    auth_level = 1

    LOG_TAG = "[空间不足自动删种] "

    # ---- 配置项默认值 ----
    _enabled = False
    # 是否发送通知（仅在发生清理或发生错误时通知）
    _notify = True
    # 预演模式：只写日志/详情页/通知，不调用删除接口
    _dry_run = False
    # 保存配置后立即运行一次并自动复位
    _onlyonce = False
    # 执行周期（cron 表达式）
    _cron = "*/30 * * * *"
    # 已选择的下载器名称（仅 qBittorrent）
    _downloaders: List[str] = []
    # 监控路径（容器内路径）
    _monitor_path = "/downloads"
    # 触发阈值（GB）：剩余空间小于该值开始清理
    _threshold_gb = 50.0
    # 停止阈值（GB）：剩余空间大于等于该值停止清理
    _target_gb = 60.0
    # 做种时长不足该小时数的不删（0 = 不限制）
    _min_seeding_hours = 0
    # 未完成的种子不删
    _protect_downloading = True
    # 同 content_path 的辅种整组删除
    _process_duplicates = True
    # 删除种子时同时删除文件
    _delete_files = True
    # 排除路径关键词（命中的种子永不删除）
    _exclude_paths: List[str] = []
    _exclude_text = ""

    # 下载类状态（这些状态的种子视为「未完成」，不删除）
    _DOWNLOADING_STATES = {
        "downloading", "stalledDL", "metaDL", "forcedDL", "queuedDL",
        "checkingDL", "allocating", "moving", "forcedMetaDL", "checkingResumeData",
    }

    # 无进展护栏：连续这么多组删除后磁盘剩余空间都没有增加，就中止本轮（避免把种子删光）
    _NO_PROGRESS_LIMIT = 3
    # 单组「无进展」判定的容忍下限（字节）：本轮累计增量低于「该下限」与
    # 「本组去重后大小 * _NO_PROGRESS_RATIO」的较大者时，才算这一组无进展
    # （容忍并发写入带来的其它增量与小文件噪声，避免误判）
    _NO_PROGRESS_FLOOR = 200 * 1024 ** 2
    _NO_PROGRESS_RATIO = 0.2
    # 本轮累计已尝试释放量不足该值时不允许中止：小文件噪声不触发护栏，留待下一轮观察
    _MIN_ABORT_RELEASE = 2 * 1024 ** 3
    # 删除后等待文件系统「空间结算」的超时（秒）与轮询间隔（秒）
    _SETTLE_TIMEOUT = 12.0
    _SETTLE_INTERVAL = 2.0
    # 结算等待的「已明显开始归还」判定上限（字节）：预期归还量的 50% 与该值取小
    _SETTLE_EARLY_CAP = 2 * 1024 ** 3

    # ---- 运行时状态 ----
    # 「立即运行一次」的一次性调度器
    _once_scheduler = None
    # 上次运行结果（详情页展示 + 跨会话持久化）
    _last_result: Dict[str, Any] = {}
    # 无释放空间闭锁（跨会话持久化）：检测到「删了种子但磁盘空间没释放」后暂停自动删除，
    # 避免定时任务一轮一轮地把种子删光；修正配置（删除文件/监控路径变化）或空间恢复后自动解除
    _latch: Dict[str, Any] = {}

    # 持久化数据键
    _LAST_RESULT_KEY = "last_result"
    _LATCH_KEY = "no_release_latch"

    # ---------------------------------------------------------------- 生命周期

    def init_plugin(self, config: dict = None):
        """读取配置、加载上次运行结果，并按需启动「立即运行一次」。"""
        self._stop_once_scheduler()

        config = config or {}
        self._enabled = self._to_bool(config.get("enabled"), False)
        self._notify = self._to_bool(config.get("notify"), True)
        self._dry_run = self._to_bool(config.get("dry_run"), False)
        self._onlyonce = self._to_bool(config.get("onlyonce"), False)
        self._cron = str(config.get("cron") or "").strip() or "*/30 * * * *"
        self._downloaders = self._normalize_config_list(config.get("downloaders"))
        # 未配置下载器时：自动选中唯一一个已启用的 qBittorrent 下载器（多个时留空，由用户手动选择）
        if not self._downloaders:
            self._downloaders = self._default_downloaders()
        self._monitor_path = str(config.get("monitor_path") or "").strip() or "/downloads"
        self._threshold_gb = self._to_float(config.get("threshold_gb"), 50.0, minimum=0.0)
        self._target_gb = self._to_float(config.get("target_gb"), 60.0, minimum=0.0)
        # 停止阈值必须大于触发阈值，否则会出现「刚删完又低于触发线」甚至删空磁盘的风险
        if self._target_gb <= self._threshold_gb:
            self._target_gb = round(self._threshold_gb + 10, 1)
            logger.warning(
                f"{self.LOG_TAG}停止阈值应大于触发阈值，已自动修正为 {self._target_gb:g} GB"
                f"（触发阈值 {self._threshold_gb:g} GB）"
            )
        self._min_seeding_hours = max(self._to_int(config.get("min_seeding_hours"), 0), 0)
        self._protect_downloading = self._to_bool(config.get("protect_downloading"), True)
        self._process_duplicates = self._to_bool(config.get("process_duplicates"), True)
        self._delete_files = self._to_bool(config.get("delete_files"), True)
        self._exclude_paths, self._exclude_text = self._normalize_excludes(config.get("exclude_paths"))

        # 读取上次运行结果（详情页展示用）
        try:
            raw = self.get_data(self._LAST_RESULT_KEY) or {}
            self._last_result = raw if isinstance(raw, dict) else {}
        except Exception as err:
            logger.error(f"{self.LOG_TAG}读取上次运行结果失败：{err}")
            self._last_result = {}

        # 读取跨会话持久化的「无释放空间」闭锁
        try:
            raw_latch = self.get_data(self._LATCH_KEY) or {}
            self._latch = raw_latch if isinstance(raw_latch, dict) else {}
        except Exception as err:
            logger.error(f"{self.LOG_TAG}读取闭锁状态失败：{err}")
            self._latch = {}

        # 规范化持久化：修正历史遗留的非法配置，避免非法值回显到表单
        try:
            if self._current_config() != config:
                self.update_config(self._current_config())
        except Exception:
            pass

        if not self._enabled:
            return

        # 立即运行一次：保存配置后 3 秒执行一轮，随后自动复位
        if self._onlyonce:
            self._onlyonce = False
            # 「立即运行一次」同时也是「解除无释放空间暂停」的手动入口：
            # 根因是硬链接等无法通过改配置修正的场景下，用户可以勾选本项强制重试一轮
            if self._latch.get("active"):
                logger.warning(
                    f"{self.LOG_TAG}已勾选「立即运行一次」：解除「删除未释放空间」暂停状态，重新尝试清理一轮"
                )
                self._clear_latch()
            try:
                self.update_config(self._current_config())
            except Exception:
                pass
            logger.info(f"{self.LOG_TAG}已勾选「立即运行一次」，将在 3 秒后执行一轮检查")
            self._start_once()

    def _default_downloaders(self) -> List[str]:
        """
        未配置下载器时的默认选中项：只自动选中「唯一一个已启用的 qBittorrent 下载器」。

        存在多个 qBittorrent 时返回空列表并记 warning，由用户在插件配置里手动选择，
        避免插件在用户未确认的情况下对多个下载器执行删除。
        """
        try:
            names = [
                str(getattr(conf, "name", "") or "").strip()
                for conf in (ServiceConfigHelper.get_downloader_configs() or [])
                if getattr(conf, "enabled", False)
                and str(getattr(conf, "type", "") or "") == "qbittorrent"
                and str(getattr(conf, "name", "") or "").strip()
            ]
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取下载器配置失败（无法自动选中默认下载器）：{err}")
            return []
        if len(names) == 1:
            logger.info(f"{self.LOG_TAG}未配置下载器，已自动选中唯一的 qBittorrent 下载器：{names[0]}")
            return names
        if len(names) > 1:
            logger.warning(
                f"{self.LOG_TAG}检测到 {len(names)} 个 qBittorrent 下载器，未自动选中，"
                "请在插件配置中手动选择要清理的下载器"
            )
        return []

    # ---------------------------------------------------------------- 无释放空间闭锁

    def _latch_signature(self) -> Dict[str, Any]:
        """闭锁签名：只跟「是否删除文件」「监控路径」有关，这两项变化说明用户已修正配置。"""
        return {"delete_files": bool(self._delete_files), "monitor_path": self._monitor_path}

    def _latch_active(self) -> bool:
        """
        当前是否处于「删除不释放空间」闭锁状态（配置未变化时才生效）。

        自动解除的两条路径：
        1. 用户已修改「同时删除文件」或「监控路径」——视为已修正配置；
        2. 闭锁自愈：置位时记录的剩余空间比当前剩余空间小至少 max(1GB, 当前的 5%)，
           说明此前被判定的「没有释放」其实只是延迟兑现（qBittorrent 删文件是异步的），
           自动解除暂停，避免正常删除被长期误停。
        """
        if not self._latch.get("active"):
            return False
        # 用户已修改「同时删除文件」或「监控路径」：视为已修正配置，自动解除闭锁
        if self._latch.get("signature") != self._latch_signature():
            self._clear_latch()
            return False
        # 闭锁自愈：延迟释放已兑现（当前剩余空间明显高于置位时记录的剩余空间）-> 自动解除
        usage = self._disk_usage(silent=True)
        if usage:
            current_gb = usage[1] / 1024 ** 3
            recorded_gb = float(self._latch.get("free_gb") or 0.0)
            tolerance_gb = max(1.0, current_gb * 0.05)
            if recorded_gb and current_gb - recorded_gb >= tolerance_gb:
                logger.info(
                    f"{self.LOG_TAG}检测到此前删除的延迟释放已兑现：闭锁置位时剩余空间 "
                    f"{recorded_gb:.1f} GB，当前已升至 {current_gb:.1f} GB"
                    f"（相差 ≥ {tolerance_gb:.1f} GB），自动解除「删除未释放空间」暂停"
                )
                self._clear_latch()
                return False
        return True

    def _set_latch(self, reason: str):
        """置位闭锁并持久化：在用户修正配置或空间恢复之前，不再自动删除任何内容。"""
        usage = self._disk_usage(silent=True)
        self._latch = {
            "active": True,
            "reason": reason,
            "signature": self._latch_signature(),
            "time": datetime.datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S"),
            "free_gb": round(usage[1] / 1024 ** 3, 1) if usage else 0.0,
        }
        try:
            self.save_data(self._LATCH_KEY, self._latch)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}持久化闭锁状态失败：{err}")

    def _clear_latch(self):
        """解除闭锁（原本没有闭锁时不写库）。"""
        if not self._latch.get("active"):
            return
        self._latch = {}
        try:
            self.save_data(self._LATCH_KEY, {})
        except Exception as err:
            logger.error(f"{self.LOG_TAG}清除闭锁状态失败：{err}")

    def _start_once(self):
        """启动一次性的空间检查清理任务。"""
        try:
            self._stop_once_scheduler()
            self._once_scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._once_scheduler.add_job(
                func=self.run_cleanup,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                name="空间不足自动删种-立即运行一次",
            )
            self._once_scheduler.start()
        except Exception as err:
            logger.error(f"{self.LOG_TAG}启动「立即运行一次」任务失败：{err}")

    def _stop_once_scheduler(self):
        """停止「立即运行一次」的一次性任务。"""
        try:
            if getattr(self, "_once_scheduler", None):
                if self._once_scheduler.running:
                    self._once_scheduler.shutdown(wait=False)
                self._once_scheduler = None
        except Exception as err:
            logger.error(f"{self.LOG_TAG}停止「立即运行一次」任务失败：{err}")

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时清理服务：按配置的 cron 表达式周期检查空间并按需清理。"""
        if self._enabled and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron)
            except Exception as err:
                logger.error(f"{self.LOG_TAG}执行周期 cron 表达式无效：{err}")
                return []
            return [
                {
                    "id": "SpaceCleanerCleanup",
                    "name": "空间不足自动删种",
                    "trigger": trigger,
                    "func": self.run_cleanup,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        """停止插件（升级/热重载/卸载时由 MoviePilot 调用）。"""
        self._stop_once_scheduler()

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

    # ---------------------------------------------------------------- 主流程

    def run_cleanup(self):
        """
        执行一轮空间检查与清理：

        1. 取监控路径的剩余/总空间（失败则记错误日志 + 通知，本轮跳过且不删除任何东西）；
        2. 剩余空间 >= 触发阈值 -> 直接结束（只记日志，不发通知）；
        3. 取所选下载器的全部种子，按 content_path 分组并过滤（排除路径 / 未完成 / 做种时长）；
        4. 按 added_on 升序逐组删除，每删一组重新检查剩余空间，达到停止阈值立即停止。
        """
        if not self._enabled:
            logger.info(f"{self.LOG_TAG}插件未启用，跳过本轮检查")
            return

        logger.info(
            f"{self.LOG_TAG}开始检查存储空间：监控路径 {self._monitor_path}，"
            f"触发阈值 {self._threshold_gb:g} GB，停止阈值 {self._target_gb:g} GB"
            f"{'（预演模式：只记录不删除）' if self._dry_run else ''}"
        )

        # 上次运行留下的错误信息（用于「同一错误不重复发通知」，避免每轮 cron 都推送）
        previous_error = str((self._last_result or {}).get("error") or "")

        usage = self._disk_usage()
        if not usage:
            # _disk_usage 内部已记录错误日志并发送通知
            return
        total_bytes, free_bytes = usage
        free_gb = free_bytes / 1024 ** 3
        logger.info(
            f"{self.LOG_TAG}当前剩余空间 {free_gb:.1f} GB / 总量 {total_bytes / 1024 ** 3:.1f} GB"
        )

        # 空间充足：不清理、不通知，只更新详情页状态；同时解除「无释放空间」闭锁
        if free_gb >= self._threshold_gb:
            logger.info(
                f"{self.LOG_TAG}剩余空间 {free_gb:.1f} GB ≥ 触发阈值 {self._threshold_gb:g} GB，空间充足，无需清理"
            )
            self._clear_latch()
            self._save_result(self._build_result(
                status="ok", message="空间充足，无需清理", total=total_bytes, free=free_bytes,
            ))
            return

        logger.warning(
            f"{self.LOG_TAG}剩余空间 {free_gb:.1f} GB < 触发阈值 {self._threshold_gb:g} GB，开始清理 ..."
        )

        # 无释放空间闭锁：上次已确认「删了种子但空间没释放」，在用户修正配置前不再自动删除
        # （先判闭锁：配置变化时它会自动解除，避免用户改回旧配置时闭锁被重新激活）
        if self._latch_active():
            latch = self._latch or {}
            message = (f"已暂停自动删除：上次运行检测到「删除种子后磁盘空间没有释放」"
                       f"（{latch.get('reason') or '未知原因'}，{latch.get('time') or '—'}）。"
                       "解除方式：①修正「同时删除文件」开关或监控路径后保存配置；"
                       "②勾选「立即运行一次」强制重试一轮；"
                       "③等剩余空间恢复到触发阈值以上。")
            logger.error(f"{self.LOG_TAG}{message}")
            self._save_result(self._build_result(
                status="error", message=message, error=message,
                total=total_bytes, free=free_bytes, free_before=free_bytes,
            ))
            return

        # 未开启「同时删除文件」：只删种子不删文件，磁盘空间不会释放，删除毫无意义还会误删种子
        if not self._delete_files and not self._dry_run:
            message = ("未开启「同时删除文件」：删除种子不会释放磁盘空间，本轮不删除任何内容。"
                       "如需自动释放空间，请开启该开关")
            logger.error(f"{self.LOG_TAG}{message}")
            self._save_result(self._build_result(
                status="error", message=message, error=message,
                total=total_bytes, free=free_bytes, free_before=free_bytes,
            ))
            # 同一个问题只通知一次（避免每轮 cron 重复推送）
            if message != previous_error:
                self._notify_message(
                    title="空间不足自动删种：配置有误",
                    text=f"监控路径：{self._monitor_path}\n剩余空间：{free_gb:.1f} GB"
                         f"（低于触发阈值 {self._threshold_gb:g} GB）\n{message}",
                )
            return

        # 收集候选内容组（每个下载器单独分组、单独删除）
        candidates, skipped_count, skipped_groups = self._collect_candidates()
        if candidates is None:
            return  # 已记录错误与状态
        if skipped_groups:
            logger.warning(
                f"{self.LOG_TAG}本轮有 {skipped_groups} 组内容因「同内容种子中存在受保护的种子」整组跳过"
                "（未完成 / 做种时长不足 / 命中排除路径）"
            )
        if not candidates:
            logger.warning(
                f"{self.LOG_TAG}没有可删除的内容（受排除路径/未完成/做种时长保护，"
                f"共跳过 {skipped_count} 个种子、{skipped_groups} 组内容）"
            )
            self._save_result(self._build_result(
                status="ok",
                message=f"空间不足但没有可删除的内容（跳过 {skipped_count} 个种子、{skipped_groups} 组内容）",
                total=total_bytes, free=free_bytes, free_before=free_bytes,
                skipped_groups=skipped_groups,
            ))
            self._notify_message(
                title="空间不足自动删种：没有可删除的内容",
                text=(
                    f"监控路径：{self._monitor_path}\n"
                    f"剩余空间：{free_gb:.1f} GB（低于触发阈值 {self._threshold_gb:g} GB）\n"
                    f"受保护/被排除而跳过的种子：{skipped_count} 个\n"
                    f"因同内容种子受保护而整组跳过的内容：{skipped_groups} 组\n"
                    "请检查排除路径、未完成种子保护与做种时长保护的配置。"
                ),
            )
            return

        # 按 added_on 升序（最旧的内容先删）
        candidates.sort(key=lambda item: item["added_on"])

        # 删除前把本轮计划写日志
        logger.info(
            f"{self.LOG_TAG}{'[预演]' if self._dry_run else ''}本轮清理计划（最旧优先）：共 {len(candidates)} 组内容"
        )
        for index, item in enumerate(candidates, start=1):
            logger.info(
                f"{self.LOG_TAG}{'[预演]' if self._dry_run else ''}计划 {index}. {item['name']}"
                f"（{item['service']}，{item['count']} 个种子，去重后 {self._fmt_size(item['dedup_size'])}，"
                f"种子合计 {self._fmt_size(item['raw_size'])}）"
            )

        self._execute(candidates, total_bytes, free_bytes, skipped_groups)

    def _execute(self, candidates: List[Dict[str, Any]], total_bytes: int, start_free: int,
                 skipped_groups: int = 0):
        """
        逐组删除内容，每删一组重新检查剩余空间，达到停止阈值立即停止（绝不超删）。

        预演模式下不调用删除接口，用「初始剩余 + 去重后释放量」模拟剩余空间，
        以便预览清理到哪一组会达到停止阈值（预演不等待空间结算）。

        真实删除时，每次读完剩余空间先等一次「空间结算」（qBittorrent 删文件是异步的，
        接口返回时空间尚未归还），再用于「停止阈值」与「无进展」判定，避免用删除瞬间的
        瞬时读数误判为「没有释放空间」。

        额外护栏：若连续多组删除、且「空间结算」后本轮剩余空间的累计增量仍明显不足
        （例如未开启「同时删除文件」、媒体库是硬链接、监控路径不在下载文件所在分区），
        说明删种子并不能释放空间，此时立即中止本轮（记错误 + 通知），避免把种子一路删光。
        """
        deleted_seeds = 0
        deleted_groups = 0
        freed_dedup = 0
        freed_raw = 0
        records: List[Dict[str, Any]] = []
        reached = False
        aborted = False
        current_free = start_free
        no_progress = 0
        failed = 0

        for item in candidates:
            hashes = item["hashes"]
            if not hashes:
                continue

            if self._dry_run:
                ok = True
            else:
                try:
                    ok = item["client"].delete_torrents(delete_file=self._delete_files, ids=hashes)
                except Exception as err:
                    ok = False
                    logger.error(f"{self.LOG_TAG}[{item['service']}] 删除内容 {item['name']} 失败：{err}")
                if not ok:
                    failed += 1
                    logger.error(
                        f"{self.LOG_TAG}[{item['service']}] 删除内容 {item['name']}"
                        f"（{len(hashes)} 个种子）失败，继续处理下一组"
                    )
                    continue

            deleted_groups += 1
            deleted_seeds += len(hashes)
            freed_dedup += item["dedup_size"]
            freed_raw += item["raw_size"]
            records.append({
                "name": item["name"],
                "site": item["site"],
                "count": len(hashes),
                "size": item["dedup_size"],
                "added_on": item["added_on"],
                # 内容路径与实际删除的 hash（便于在日志/详情页核对，辅种整组删除时会有多个）
                "key": item["key"],
                "hashes": hashes,
            })
            logger.info(
                f"{self.LOG_TAG}{'[预演]' if self._dry_run else ''}已删除内容：{item['name']}"
                f"（{item['service']}，{len(hashes)} 个种子，去重后 {self._fmt_size(item['dedup_size'])}，"
                f"种子合计 {self._fmt_size(item['raw_size'])}）"
            )

            # 每删一组重新检查剩余空间：达到停止阈值立即停止
            if self._dry_run:
                # 预演模式：用去重后的释放量模拟，便于预估清理范围（不等待空间结算）
                current_free = start_free + freed_dedup
            else:
                # 删除后先等文件系统「空间结算」再判定（qBittorrent 异步删文件，接口返回时空间尚未归还）
                current_free = self._read_free_settled(
                    current_free, item["dedup_size"], self._SETTLE_TIMEOUT, self._SETTLE_INTERVAL
                )
                if self._disk_usage(silent=True) is None:
                    # 读不到空间时不继续删，避免超删
                    logger.error(f"{self.LOG_TAG}删除后读取剩余空间失败，本轮提前结束（避免超删）")
                    break
                # 无进展护栏（累计口径）：结算等待后「本轮剩余空间的累计增量」仍低于
                # max(200MB, 本组去重后大小 * 20%) 才算这一组无进展；累计口径 + 容忍阈值
                # 可以容忍并发写入与小文件噪声，不会被单次瞬时读数误判
                increment = current_free - start_free
                expected = max(self._NO_PROGRESS_FLOOR, int(item["dedup_size"] * self._NO_PROGRESS_RATIO))
                no_progress = no_progress + 1 if increment < expected else 0
                if no_progress >= self._NO_PROGRESS_LIMIT and freed_dedup >= self._MIN_ABORT_RELEASE:
                    aborted = True
                    reason = (
                        f"已连续 {no_progress} 组删除、等待空间结算后磁盘剩余空间仍未明显增加"
                        f"（本轮累计仅增加 {self._fmt_size(max(increment, 0))}，"
                        f"已尝试释放 {self._fmt_size(freed_dedup)}）。常见原因："
                        "①未开启「同时删除文件」；②媒体库使用硬链接（删除下载侧文件不释放空间）；"
                        "③监控路径不在下载文件所在分区"
                    )
                    logger.error(
                        f"{self.LOG_TAG}{reason}。为避免把种子删除干净，本轮自动中止，"
                        "并暂停后续自动删除（修正配置或空间恢复后自动解除）。"
                    )
                    # 置位跨会话闭锁：避免下一轮定时任务继续一批一批地删种子
                    self._set_latch(reason)
                    break
                if no_progress >= self._NO_PROGRESS_LIMIT:
                    # 无进展组数够了，但本轮累计尝试释放量还不到 _MIN_ABORT_RELEASE：
                    # 小文件噪声不触发护栏，继续观察后续组再决定是否中止
                    logger.warning(
                        f"{self.LOG_TAG}已连续 {no_progress} 组删除、等待空间结算后剩余空间仍未明显增加，"
                        f"但本轮累计已尝试释放仅 {self._fmt_size(freed_dedup)}"
                        f"（不足 {self._fmt_size(self._MIN_ABORT_RELEASE)}），暂不中止，继续观察"
                    )
            if current_free / 1024 ** 3 >= self._target_gb:
                reached = True
                logger.info(
                    f"{self.LOG_TAG}剩余空间已回到 {current_free / 1024 ** 3:.1f} GB"
                    f"（≥ 停止阈值 {self._target_gb:g} GB），停止清理"
                )
                break

        # 实际释放量 = 磁盘剩余空间前后差值：统计前先等一次「空间结算」（最后一组可能仍在
        # 异步归还中，不等待会少算释放量，也让下面的兜底护栏拿到结算后的真实读数）；
        # 基准取本轮开始前的剩余空间、预期量取本轮去重后释放量：正常情况下本轮空间已归还，
        # 第一次读取就满足条件、不会额外等待；
        # 可能因其它进程写入而不精确，仅作参考
        freed_actual = 0
        if not self._dry_run and deleted_groups:
            current_free = self._read_free_settled(
                start_free, freed_dedup, self._SETTLE_TIMEOUT, self._SETTLE_INTERVAL
            )
            freed_actual = max(current_free - start_free, 0)

        # 兜底：删了内容但磁盘一点都没释放、且未达到停止阈值 -> 与无进展护栏同因（硬链接等），
        # 同样中止并置位闭锁（候选不足 3 组时上面的连续计数护栏不会触发，靠这里兜住）；
        # 累计尝试释放量不足 _MIN_ABORT_RELEASE 时不中止，小文件噪声不触发护栏
        if (not self._dry_run and not aborted and not reached and deleted_groups
                and freed_actual <= 0 and freed_dedup >= self._MIN_ABORT_RELEASE):
            aborted = True
            reason = (
                f"已删除 {deleted_groups} 组内容并等待空间结算后，监控路径的剩余空间仍未增加。常见原因："
                "①未开启「同时删除文件」；②媒体库使用硬链接（删除下载侧文件不释放空间）；"
                "③监控路径不在下载文件所在分区"
            )
            logger.error(
                f"{self.LOG_TAG}{reason}。为避免继续删除种子，本轮自动中止，"
                "并暂停后续自动删除（修正配置、勾选「立即运行一次」或空间恢复后自动解除）。"
            )
            self._set_latch(reason)

        if not reached and not aborted:
            logger.warning(
                f"{self.LOG_TAG}{'[预演]' if self._dry_run else ''}候选内容已处理完，"
                f"剩余空间仍未达到停止阈值 {self._target_gb:g} GB（当前 {current_free / 1024 ** 3:.1f} GB）"
            )

        # 日志中同时给出「去重后释放量」与「种子条数」，避免辅种导致虚高
        logger.info(
            f"{self.LOG_TAG}{'[预演]' if self._dry_run else ''}本轮清理完成：删除种子 {deleted_seeds} 个"
            f"（内容 {deleted_groups} 组），去重后释放 {self._fmt_size(freed_dedup)}"
            f"（种子合计 {self._fmt_size(freed_raw)}，磁盘实际释放 {self._fmt_size(freed_actual)}），"
            f"剩余空间 {current_free / 1024 ** 3:.1f} GB"
            f"{'，已达到停止阈值' if reached else '，未达到停止阈值'}"
            f"{f'，删除失败 {failed} 组' if failed else ''}"
            f"{f'，因同内容种子受保护整组跳过 {skipped_groups} 组' if skipped_groups else ''}"
            f"{'，已因删除未释放空间而中止' if aborted else ''}"
        )

        if aborted:
            message = ("清理中止：删除未释放磁盘空间（请检查是否开启「同时删除文件」、"
                       "媒体库是否为硬链接、监控路径是否在下载分区）；已暂停后续自动删除，"
                       "修正配置或空间恢复到触发阈值以上后自动解除")
        elif reached:
            message = "清理完成"
        else:
            message = "候选内容已删完，仍未达到停止阈值"
        result = self._build_result(
            status="error" if aborted else "ok",
            message=message,
            total=total_bytes,
            free=current_free,
            free_before=start_free,
            deleted_seeds=deleted_seeds,
            deleted_groups=deleted_groups,
            freed_dedup=freed_dedup,
            freed_raw=freed_raw,
            freed_actual=freed_actual,
            reached=reached,
            records=records,
            failed=failed,
            aborted=aborted,
            skipped_groups=skipped_groups,
            error=message if aborted else "",
        )
        self._save_result(result)

        # 仅在「实际发生清理或发生错误」时通知
        if deleted_groups or failed or aborted:
            title = "空间不足自动删种" + ("（预演）" if self._dry_run else "")
            lines = [
                f"监控路径：{self._monitor_path}",
                f"删除种子：{deleted_seeds} 个（内容 {deleted_groups} 组）",
                f"去重后释放：{self._fmt_size(freed_dedup)}",
                f"剩余空间：{current_free / 1024 ** 3:.1f} GB（触发 {self._threshold_gb:g} GB / 停止 {self._target_gb:g} GB）",
                f"是否达到停止阈值：{'是' if reached else '否'}",
            ]
            if self._dry_run:
                lines.append("预演模式：未实际删除任何种子与文件")
            if aborted:
                lines.append(f"⚠️ 已中止：{message}")
            if failed:
                lines.append(f"删除失败：{failed} 组（详见日志）")
            if not reached and not aborted and not self._dry_run:
                lines.append("提示：候选内容已删完仍未达到停止阈值，可检查排除路径或手动清理")
            self._notify_message(title=title, text="\n".join(lines))

    def _collect_candidates(self) -> Tuple[Optional[List[Dict[str, Any]]], int, int]:
        """
        取所选下载器的全部种子（含手工添加），按 content_path 分组并过滤：

        - 命中排除路径（content_path / save_path）的种子永不删除；
        - 未完成的种子（progress < 1 / amount_left > 0 / 下载类状态）不删（开关保护）；
        - 做种时长不足配置小时数的不删（防 H&R 误删）；
        - 内容组按整体处理：同一 content_path 的种子共享同一份文件，只要组内有任意一个受保护成员，
          整组本轮都不删（只删其中一部分会把共享文件删掉、让留下的种子报错，也可能误删受保护的种子）；
        - 整组可删时：辅种整组删除开关开启 -> 组内全部 hash 一次删完；
          关闭 -> 只删组内最旧的那一个 hash。

        返回 (内容组列表, 被跳过的种子数, 因同内容受保护而整组跳过的内容组数)；
        下载器不可用时返回 (None, 0, 0)。
        """
        services = self._get_services()
        if not services:
            logger.error(f"{self.LOG_TAG}没有可用的 qBittorrent 下载器，本轮跳过（不删除任何内容）")
            self._notify_message(
                title="空间不足自动删种：执行失败",
                text=f"没有可用的 qBittorrent 下载器，请检查下载器配置与连接状态。\n监控路径：{self._monitor_path}",
            )
            return None, 0, 0

        candidates: List[Dict[str, Any]] = []
        skipped_count = 0
        skipped_groups = 0
        for service_name, service_info in services.items():
            groups, skipped, group_skipped = self._scan_service(service_name, service_info)
            candidates.extend(groups)
            skipped_count += skipped
            skipped_groups += group_skipped
        return candidates, skipped_count, skipped_groups

    def _scan_service(self, service_name: str, service_info: ServiceInfo) -> Tuple[List[Dict[str, Any]], int, int]:
        """扫描单个下载器，返回 (可删内容组列表, 被跳过的种子数, 整组跳过的内容组数)。"""
        client = service_info.instance
        try:
            torrents, error = client.get_torrents()
        except Exception as err:
            logger.error(f"{self.LOG_TAG}[{service_name}] 获取种子列表失败：{err}")
            return [], 0, 0
        if error:
            logger.warning(f"{self.LOG_TAG}[{service_name}] 获取种子列表失败（下载器返回错误），跳过该下载器")
            return [], 0, 0
        torrents = torrents or []
        logger.info(f"{self.LOG_TAG}[{service_name}] 读取到 {len(torrents)} 个种子（含手工添加的种子）")

        # 按 content_path 分组：同一 content_path 的多个 hash = 同内容的多站点辅种
        grouped: Dict[str, List[Any]] = {}
        for torrent in torrents:
            key = self._content_key(torrent)
            if key:
                grouped.setdefault(key, []).append(torrent)

        groups: List[Dict[str, Any]] = []
        skipped_count = 0
        skipped_groups = 0
        for key, members in grouped.items():
            deleted: List[Any] = []
            protected: List[Tuple[Any, str]] = []
            for torrent in members:
                reason = self._skip_reason(torrent)
                if reason:
                    protected.append((torrent, reason))
                else:
                    deleted.append(torrent)
            skipped_count += len(protected)
            if protected:
                # 组内有受保护成员 -> 该内容组本轮整组不删（含全部成员都受保护的情况）
                skipped_groups += 1
            if not deleted:
                continue
            # 组内最旧的种子决定该内容组的删除顺序（最旧优先）
            deleted.sort(key=lambda item: self._added_on(item))
            head = deleted[0]
            if protected:
                # 同内容种子共享同一份文件：组内有受保护成员时整组跳过，
                # 避免「删了文件却留下种子」导致其它种子报错的残留（排除路径/未完成/做种时长保护优先）
                detail = "；".join(
                    f"{self._torrent_name(item) or self._torrent_hash(item)}（{reason}）"
                    for item, reason in protected[:3]
                )
                logger.info(
                    f"{self.LOG_TAG}[{service_name}] 内容 {self._torrent_name(head)} 的同内容种子中有 "
                    f"{len(protected)} 个受保护（{detail}），整组本轮跳过不删除"
                )
                continue
            if self._process_duplicates:
                # 辅种整组删除：同一 content_path 的全部 hash 一起删（一次接口调用）
                delete_members = deleted
            else:
                # 关闭整组删除时，只删该内容组里最旧的一个 hash
                delete_members = [head]
            hashes = [self._torrent_hash(item) for item in delete_members]
            hashes = [item for item in hashes if item]
            if not hashes:
                continue
            groups.append({
                "service": service_name,
                "client": client,
                "key": key,
                "name": self._torrent_name(head) or self._torrent_hash(head),
                "site": self._tracker_host(head),
                "hashes": hashes,
                "count": len(hashes),
                # 去重后大小：同一 content_path 只计一次（取组内最大 size）
                "dedup_size": max(self._size(item) for item in members),
                # 种子合计大小：仅统计实际删除的种子
                "raw_size": sum(self._size(item) for item in delete_members),
                "added_on": self._added_on(head),
            })
        return groups, skipped_count, skipped_groups

    # ---------------------------------------------------------------- 判定辅助

    def _skip_reason(self, torrent: Any) -> str:
        """返回种子不可删除的原因；返回空字符串表示可以删除。"""
        # 1. 排除路径：content_path 或 save_path 命中任一关键词 -> 永不删除
        if self._hit_exclude(torrent):
            return "命中排除路径"
        # 2. 未完成的种子不删
        if self._protect_downloading and self._is_unfinished(torrent):
            return "未完成"
        # 3. 做种时长不足的不删（防 H&R 误删）
        if self._min_seeding_hours > 0:
            hours = self._seeding_hours(torrent)
            if hours < self._min_seeding_hours:
                return f"做种仅 {hours:.1f} 小时（不足 {self._min_seeding_hours} 小时）"
        return ""

    def _hit_exclude(self, torrent: Any) -> bool:
        """种子是否命中排除路径关键词（content_path 或 save_path 包含关键词，忽略大小写）。"""
        if not self._exclude_paths:
            return False
        paths = [
            str(self._field(torrent, "content_path", "") or "").lower(),
            str(self._field(torrent, "save_path", "") or "").lower(),
        ]
        return any(keyword in path for keyword in self._exclude_paths for path in paths if path)

    def _is_unfinished(self, torrent: Any) -> bool:
        """种子是否未完成：progress < 1、amount_left > 0 或处于下载类状态。"""
        state = str(self._field(torrent, "state", "") or "").strip()
        if state in self._DOWNLOADING_STATES:
            return True
        try:
            progress = float(self._field(torrent, "progress", 1) or 0)
        except (TypeError, ValueError):
            progress = 1.0
        if progress < 1:
            return True
        try:
            amount_left = int(self._field(torrent, "amount_left", 0) or 0)
        except (TypeError, ValueError):
            amount_left = 0
        return amount_left > 0

    def _seeding_hours(self, torrent: Any) -> float:
        """
        计算做种时长（小时）：以 completion_on 为起点，为 0 时回退 added_on。
        两个时间都取不到时返回 0（视为做种时长不足，受保护）。
        """
        completed = self._to_int(self._field(torrent, "completion_on", 0), 0)
        added = self._added_on(torrent)
        base = completed if completed > 0 else added
        if base <= 0:
            return 0.0
        return max(time.time() - base, 0) / 3600

    @staticmethod
    def _content_key(torrent: Any) -> str:
        """内容分组键：优先 content_path，缺失时回退「save_path/name」。"""
        content_path = str(SpaceCleaner._field(torrent, "content_path", "") or "").strip()
        if content_path:
            return content_path
        save_path = str(SpaceCleaner._field(torrent, "save_path", "") or "").strip()
        name = str(SpaceCleaner._field(torrent, "name", "") or "").strip()
        return f"{save_path}/{name}" if (save_path or name) else ""

    @staticmethod
    def _added_on(torrent: Any) -> int:
        """种子的添加时间（时间戳），取不到时返回 0。"""
        try:
            return int(SpaceCleaner._field(torrent, "added_on", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _size(torrent: Any) -> int:
        """种子大小（字节），优先 size，缺失时用 total_size。"""
        for key in ("size", "total_size"):
            try:
                value = int(SpaceCleaner._field(torrent, key, 0) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return 0

    @staticmethod
    def _torrent_hash(torrent: Any) -> str:
        """种子哈希。"""
        return str(SpaceCleaner._field(torrent, "hash", "") or "").strip()

    @staticmethod
    def _torrent_name(torrent: Any) -> str:
        """种子名称。"""
        return str(SpaceCleaner._field(torrent, "name", "") or "").strip()

    @staticmethod
    def _tracker_host(torrent: Any) -> str:
        """从 tracker 地址中提取站点域名（用于详情页展示）。"""
        try:
            host = urlparse(str(SpaceCleaner._field(torrent, "tracker", "") or "")).hostname or ""
        except Exception:
            return ""
        return host[4:] if host.startswith("www.") else host

    @staticmethod
    def _field(torrent: Any, key: str, default: Any = None) -> Any:
        """
        读取种子字段，兼容 dict 与 qbittorrentapi 的 TorrentDictionary（属性访问）。
        """
        value = None
        try:
            value = torrent.get(key)
        except Exception:
            value = None
        if value is None:
            value = getattr(torrent, key, None)
        return default if value is None else value

    # ---------------------------------------------------------------- 磁盘空间

    def _disk_usage(self, silent: bool = False) -> Optional[Tuple[int, int]]:
        """
        读取监控路径所在分区的 (总空间, 剩余空间)，单位字节。

        路径不存在或无权限读取时：记录 error 日志 + 发送通知（silent=True 时只记日志），
        返回 None（调用方跳过本轮，不删除任何东西）。
        """
        path = self._monitor_path
        if not path:
            return self._disk_error("未配置监控路径", silent)
        if not os.path.exists(path):
            return self._disk_error(
                f"监控路径在容器内不存在：{path}（请填写容器内可见路径，例如 /downloads）", silent
            )
        try:
            usage = shutil.disk_usage(path)
        except Exception as err:
            return self._disk_error(f"读取磁盘空间失败：{path}（{err}）", silent)
        return int(usage.total), int(usage.free)

    def _read_free_settled(self, base_free: int, expected_bytes: int,
                           timeout: float = 12.0, interval: float = 2.0) -> int:
        """
        删除后等待文件系统「空间结算」，返回结算后的剩余空间（字节）。

        qBittorrent 的 delete_torrents(delete_file=True) 是服务端异步删文件：接口返回后
        文件系统空间尚未归还（大文件尤其明显），若立刻读取剩余空间就会被误判为
        「删除未释放空间」。因此删除后轮询监控路径：一旦剩余空间 >= base_free +
        min(expected_bytes * 0.5, 2GB)（说明已明显开始归还）就提前返回；否则每隔
        interval 秒读一次，最多等 timeout 秒，返回最后一次读到的剩余空间。

        base_free：删除前的剩余空间（字节）；
        expected_bytes：本次删除预期归还的字节数（去重后大小）。
        磁盘读取失败时返回 base_free（调用方按需自行判断是否继续）。
        """
        started = time.time()
        target = base_free + min(max(int(expected_bytes or 0), 0) // 2, self._SETTLE_EARLY_CAP)
        last_free = base_free
        while True:
            usage = self._disk_usage(silent=True)
            if usage:
                last_free = usage[1]
                if last_free >= target:
                    logger.info(
                        f"{self.LOG_TAG}等待空间结算：预期归还 {self._fmt_size(expected_bytes)}，"
                        f"删除后 {time.time() - started:.1f} 秒内已归还"
                        f"（剩余空间 {base_free / 1024 ** 3:.1f} GB → {last_free / 1024 ** 3:.1f} GB）"
                    )
                    return last_free
            if time.time() - started >= timeout:
                logger.warning(
                    f"{self.LOG_TAG}等待空间结算：预期归还 {self._fmt_size(expected_bytes)}，"
                    f"删除后等待 {timeout:g} 秒超时仍未归还"
                    f"（剩余空间 {base_free / 1024 ** 3:.1f} GB → {last_free / 1024 ** 3:.1f} GB）"
                )
                return last_free
            time.sleep(interval)

    def _disk_error(self, message: str, silent: bool = False) -> None:
        """统一的路径/权限错误处理：记 error 日志、更新状态，（非 silent 时）发通知。"""
        logger.error(f"{self.LOG_TAG}{message}，本轮跳过（不删除任何内容）")
        if silent:
            return None
        self._save_result(self._build_result(
            status="error", message=message, total=0, free=0, error=message,
        ))
        self._notify_message(
            title="空间不足自动删种：执行失败",
            text=f"{message}\n本轮未删除任何内容，请检查监控路径配置。",
        )
        return None

    # ---------------------------------------------------------------- 状态与通知

    def _build_result(self, status: str, message: str, total: int, free: int,
                      free_before: Optional[int] = None,
                      deleted_seeds: int = 0, deleted_groups: int = 0,
                      freed_dedup: int = 0, freed_raw: int = 0, freed_actual: int = 0,
                      reached: bool = False, records: Optional[List[Dict[str, Any]]] = None,
                      failed: int = 0, error: str = "", aborted: bool = False,
                      skipped_groups: int = 0) -> Dict[str, Any]:
        """组装本轮运行结果（详情页展示 + 持久化）。"""
        return {
            "time": datetime.datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "message": message,
            "error": error,
            "aborted": bool(aborted),
            "skipped_groups": int(skipped_groups),
            "dry_run": self._dry_run,
            "path": self._monitor_path,
            "threshold_gb": self._threshold_gb,
            "target_gb": self._target_gb,
            "total": int(total or 0),
            # free = 本轮结束时的剩余空间；free_before = 本轮开始前的剩余空间
            "free": int(free or 0),
            "free_before": int(free if free_before is None else free_before),
            "deleted_seeds": int(deleted_seeds),
            "deleted_groups": int(deleted_groups),
            "freed_dedup": int(freed_dedup),
            "freed_raw": int(freed_raw),
            "freed_actual": int(freed_actual),
            "reached": bool(reached),
            "failed": int(failed),
            # 详情页只保留最近 50 条，避免数据过大
            "records": (records or [])[:50],
        }

    def _save_result(self, result: Dict[str, Any]):
        """保存本轮运行结果（内存 + 持久化）。"""
        self._last_result = result
        try:
            self.save_data(self._LAST_RESULT_KEY, result)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}持久化运行结果失败：{err}")

    def _notify_message(self, title: str, text: str):
        """发送通知（未开启通知时只记日志）。"""
        if not self._notify:
            logger.info(f"{self.LOG_TAG}通知未开启，跳过发送：{title}")
            return
        try:
            mtype = getattr(NotificationType, "Plugin", None) or getattr(NotificationType, "SiteMessage", None)
            self.post_message(mtype=mtype, title=f"【{title}】", text=text)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}发送通知失败：{err}")

    # ---------------------------------------------------------------- 下载器

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
        active_services: Dict[str, ServiceInfo] = {}
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

    # ---------------------------------------------------------------- 配置与页面

    def _current_config(self) -> Dict[str, Any]:
        """返回当前配置，供表单回填。"""
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "dry_run": self._dry_run,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "downloaders": self._downloaders,
            "monitor_path": self._monitor_path,
            "threshold_gb": self._threshold_gb,
            "target_gb": self._target_gb,
            "min_seeding_hours": self._min_seeding_hours,
            "protect_downloading": self._protect_downloading,
            "process_duplicates": self._process_duplicates,
            "delete_files": self._delete_files,
            "exclude_paths": self._exclude_text,
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件设置表单。

        第一行：启用插件 / 预演模式 / 发送通知；
        第二行：立即运行一次 / 执行周期（cron）/ 监控路径；
        第三行：触发阈值 / 停止阈值 / 做种时长保护；
        第四行：未完成种子保护 / 辅种整组删除 / 删除文件；
        第五行：下载器（多选，仅 qBittorrent）；
        第六行：排除路径（多行）；
        第七行：说明与危险提示。
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
                                            "model": "dry_run",
                                            "label": "预演模式（只记录不删除）",
                                            "hint": "开启后只写日志、更新详情页、发通知，不会调用任何删除接口。上线前建议先开预演模式观察一到两轮，确认要删的内容没问题后再关闭。",
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
                                            "model": "notify",
                                            "label": "发送通知",
                                            "hint": "仅在「实际发生清理或发生错误」时发送通知；空间充足时不发通知，只记日志。",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                            "hint": "保存配置后立即执行一轮空间检查与清理，执行后开关自动复位。注意：勾选它还会解除「删除未释放空间」的自动暂停（详情页显示为已暂停时，用它可强制重试一轮，届时最多再删 3 组内容）。",
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
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期（cron）",
                                            "placeholder": "*/30 * * * *",
                                            "hint": "cron 表达式，默认每 30 分钟检查一次存储剩余空间。",
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
                                            "model": "monitor_path",
                                            "label": "监控路径",
                                            "placeholder": "/downloads",
                                            "hint": "填容器内可见的路径：/downloads 对应宿主 /mnt/storage/media/downloads（与下载目录同一分区）。插件用该路径计算磁盘剩余空间。",
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
                                            "model": "threshold_gb",
                                            "label": "触发阈值（GB）",
                                            "placeholder": "例如 50",
                                            "type": "number",
                                            "min": 0,
                                            "step": 1,
                                            "hide-spin-buttons": True,
                                            "hint": "剩余空间小于该值时开始清理，默认 50 GB。",
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
                                            "model": "target_gb",
                                            "label": "停止阈值（GB）",
                                            "placeholder": "例如 60",
                                            "type": "number",
                                            "min": 0,
                                            "step": 1,
                                            "hide-spin-buttons": True,
                                            "hint": "剩余空间恢复到该值以上时停止清理，默认 60 GB。停止阈值应大于触发阈值（否则插件会自动修正为触发阈值 +10）。",
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
                                            "model": "min_seeding_hours",
                                            "label": "做种时长保护（小时）",
                                            "placeholder": "例如 72；0 表示不限制",
                                            "type": "number",
                                            "min": 0,
                                            "step": 1,
                                            "hide-spin-buttons": True,
                                            "hint": "做种时长（完成时间或添加时间起算）不足该小时数的种子不删除，用于防止 H&R 种子被误删；0 表示不限制。",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "protect_downloading",
                                            "label": "未完成的种子不删",
                                            "hint": "未下载完成（进度小于 100%、仍有待下载数据或处于下载/校验/移动等状态）的种子不删除，建议保持开启。",
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
                                            "model": "process_duplicates",
                                            "label": "辅种整组删除",
                                            "hint": "同一内容路径（content_path）的多个种子（多站点辅种）共享同一份文件，删除时整组一起删；只删一个会留下报错的残留种子。建议保持开启。",
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
                                            "model": "delete_files",
                                            "label": "同时删除文件",
                                            "hint": "开启时删除种子会一并删除已下载的文件，从而真正释放磁盘空间（本插件的目的就是释放空间，请保持开启）。关闭后删种子不删文件、磁盘空间不会释放，插件将不删除任何内容并在详情页提示配置有误。",
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
                                            "hint": "选择 MoviePilot 中已配置的 qBittorrent 下载器（只处理 qBittorrent 类型）；未选择时不会删除任何内容。",
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
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "exclude_paths",
                                            "label": "排除路径（永不删除）",
                                            "placeholder": "一行一个关键词，例如：\ndownloads/keep\n/downloads/torrent/keep",
                                            "rows": 3,
                                            "auto-grow": True,
                                            "clearable": True,
                                            "hint": "种子的内容路径（content_path）或保存路径（save_path）包含任一关键词时不删除，忽略大小写。例如填 downloads/keep。",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "工作方式：定时检查监控路径的剩余空间；剩余空间低于「触发阈值」开始清理，按添加时间从旧到新依次删除种子（含手工添加的种子，不限是否已整理），每删完一组内容重新检查一次空间，恢复到「停止阈值」以上立即停止。同一内容路径的多个种子（多站点辅种）默认整组删除。",
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
                                            "text": "注意：本插件会删除种子并删除文件（不可恢复）。上线前请先开启「预演模式」跑一到两轮，到 MoviePilot 日志与插件详情页确认计划删除的内容；同时建议配置「排除路径」保护重要目录，并按需设置「做种时长保护」防止 H&R 种子被误删。",
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
        详情面板：监控路径与空间概况、当前策略、上次运行结果与最近删除内容。

        版式按窄屏（手机）适配：每行 flex-wrap 自动换行，不使用 nowrap，卡片留左右内边距。
        """
        if not self._enabled:
            return [{'component': 'div', 'text': '插件未启用', 'props': {'class': 'text-center'}}]

        result = self._last_result or {}
        # 实时读一次磁盘空间（失败时用上次结果里的值兜底）
        usage = self._disk_usage(silent=True)

        def card(label: str, value: str, hint: str = "") -> dict:
            """窄屏友好的信息卡片：一行多个，放不下自动换行。"""
            content = [
                {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': label},
                {'component': 'div', 'props': {'class': 'text-body-1'}, 'text': value},
            ]
            if hint:
                content.append({'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': hint})
            return {
                'component': 'VCol',
                'props': {'cols': 12, 'sm': 6, 'md': 4},
                'content': [{'component': 'div', 'props': {'class': 'px-2 py-1'}, 'content': content}],
            }

        if usage:
            total_bytes, free_bytes = usage
            free_text = f"{free_bytes / 1024 ** 3:.1f} GB / 共 {total_bytes / 1024 ** 3:.1f} GB"
            usage_ok = True
        else:
            total_bytes = int(result.get("total") or 0)
            free_bytes = int(result.get("free") or 0)
            free_text = (
                "读取失败（详见日志）" if not total_bytes
                else f"{free_bytes / 1024 ** 3:.1f} GB / 共 {total_bytes / 1024 ** 3:.1f} GB（上次运行结果）"
            )
            usage_ok = False

        free_gb = free_bytes / 1024 ** 3
        if not usage_ok:
            space_state = "⚪ 无法读取监控路径"
        elif free_gb < self._threshold_gb:
            space_state = "🔴 空间不足（已触发清理条件）"
        elif free_gb < self._target_gb:
            space_state = "🟡 低于停止阈值"
        else:
            space_state = "🟢 空间充足"

        # 「无释放空间」闭锁状态（只取一次，避免重复触发自动解除逻辑）
        latch_active = self._latch_active()
        latch = self._latch if latch_active else {}

        overview = {
            'component': 'VRow',
            'content': [
                card("监控路径", self._monitor_path, "对应宿主 /mnt/storage/media/downloads"),
                card("当前空间状态", space_state),
                card("剩余空间", free_text),
                card("触发阈值 / 停止阈值", f"{self._threshold_gb:g} GB / {self._target_gb:g} GB", "剩余低于触发阈值开始删，恢复到停止阈值以上停止"),
                card("预演模式", "已开启（只记录不删除）" if self._dry_run else "已关闭（会真实删除种子与文件）"),
                card("执行周期", self._cron or "—"),
                card("自动删除状态",
                     "⛔ 已暂停（上次删除未释放空间）" if latch_active else "✅ 正常",
                     f"{latch.get('reason') or ''}（{latch.get('time') or ''}）"
                     if latch_active else "修正配置或空间恢复后自动解除"),
            ],
        }

        policy = {
            'component': 'VRow',
            'content': [
                card("下载器", "、".join(self._downloaders) if self._downloaders else "未选择（不会删除任何内容）"),
                card("未完成种子保护", "开启（不删未完成的种子）" if self._protect_downloading else "关闭（未完成的种子也会被删）"),
                card("辅种整组删除", "开启（同内容多个种子一起删）" if self._process_duplicates else "关闭（每个内容只删最旧的一个种子）"),
                card("同时删除文件", "开启" if self._delete_files else "关闭（只删种子，不释放磁盘空间）"),
                card("做种时长保护", f"{self._min_seeding_hours} 小时" if self._min_seeding_hours > 0 else "未限制"),
                card("排除路径", " ｜ ".join(self._exclude_paths) if self._exclude_paths else "未配置"),
            ],
        }

        sections = [overview, policy]

        if not result:
            sections.append({
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
                                    'text': '暂无运行记录（等待定时任务执行，或保存配置时勾选「立即运行一次」）。',
                                },
                            }
                        ],
                    }
                ],
            })
            return sections

        status_labels = {"ok": "✅ 正常", "error": "❌ 出错"}
        status = status_labels.get(str(result.get("status")), str(result.get("status") or "—"))
        detail_bits = []
        if result.get("message"):
            detail_bits.append(str(result.get("message")))
        if result.get("error"):
            detail_bits.append(f"错误：{result.get('error')}")
        sections.append({
            'component': 'VRow',
            'content': [
                card("上次运行时间", str(result.get("time") or "—"), "预演：是" if result.get("dry_run") else "预演：否"),
                card("上次运行状态", status, " ｜ ".join(detail_bits)),
                card("上次删除种子数", f"{int(result.get('deleted_seeds') or 0)} 个（内容 {int(result.get('deleted_groups') or 0)} 组）",
                     f"删除失败 {int(result.get('failed') or 0)} 组" if result.get("failed") else ""),
                card("上次去重后释放", self._fmt_size(int(result.get("freed_dedup") or 0)),
                     f"种子合计 {self._fmt_size(int(result.get('freed_raw') or 0))}，磁盘实际释放 {self._fmt_size(int(result.get('freed_actual') or 0))}"),
                card("上次是否达到停止阈值", "是" if result.get("reached") else "否"),
                card("上次剩余空间（运行前 → 运行后）",
                     f"{int(result.get('free_before') or 0) / 1024 ** 3:.1f} GB → "
                     f"{int(result.get('free') or 0) / 1024 ** 3:.1f} GB"
                     if result.get("free_before") else "—",
                     "因同内容种子受保护而整组跳过 "
                     f"{int(result.get('skipped_groups') or 0)} 组" if result.get("skipped_groups") else ""),
            ],
        })

        records = result.get("records") or []
        if records:
            rows = [
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'sm': 6, 'md': 4},
                            'content': [
                                {
                                    'component': 'div',
                                    'props': {
                                        'class': 'px-2 py-1 text-body-2',
                                        # 窄屏（手机）长名称自动换行，避免横向溢出
                                        'style': 'word-break: break-all; white-space: normal;',
                                    },
                                    'text': f"{item.get('name') or '—'}（{item.get('site') or '未知站点'}，{int(item.get('count') or 0)} 个种子，{self._fmt_size(int(item.get('size') or 0))}）",
                                }
                            ],
                        } for item in records
                    ],
                }
            ]
            sections.append({
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
                                    'text': f"上次运行删除的内容（最多显示 50 组，共 {int(result.get('deleted_groups') or 0)} 组）：",
                                },
                            },
                            *rows,
                        ],
                    }
                ],
            })
        return sections

    # ---------------------------------------------------------------- 工具方法

    @staticmethod
    def _normalize_excludes(value: Any) -> Tuple[List[str], str]:
        """
        解析排除路径文本：一行一个关键词，忽略空行与 # 注释行，
        去重后统一小写（匹配时忽略大小写），并返回规范化文本用于表单回填。
        """
        keywords: List[str] = []
        for raw_line in str(value or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            keyword = line.lower()
            if keyword not in keywords:
                keywords.append(keyword)
        return keywords, "\n".join(keywords)

    @staticmethod
    def _normalize_config_list(value: Any) -> List[str]:
        """将配置项规范化为去重后的字符串列表，兼容单个字符串配置。"""
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
    def _to_bool(value: Any, default: bool = False) -> bool:
        """安全转换为布尔值。"""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
        return default

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        """安全转换为整数：仅接受整数或整数字符串，转换失败时返回默认值。"""
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
    def _to_float(value: Any, default: float = 0.0, minimum: float = 0.0) -> float:
        """安全转换为不小于 minimum 的浮点数；非法值回退默认值。"""
        if value is None or isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number != number:  # NaN
            return default
        return max(round(number, 1), minimum)

    @staticmethod
    def _fmt_size(num: Any) -> str:
        """格式化文件大小（自动进位到 B/KB/MB/GB/TB）。"""
        try:
            value = float(num or 0)
        except (TypeError, ValueError):
            value = 0.0
        negative = value < 0
        value = abs(value)
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        index = 0
        while value >= 1024 and index < len(units) - 1:
            value /= 1024
            index += 1
        text = f"{int(value)} B" if index == 0 else f"{value:.2f} {units[index]}"
        return f"-{text}" if negative else text
