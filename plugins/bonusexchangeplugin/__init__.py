import threading
import time
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
import pytz
from app.helper.sites import SitesHelper
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import YAMLError
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.core.plugin import PluginManager
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.plugins import _PluginBase
from app.scheduler import Scheduler
from app.schemas import NotificationType
from app.schemas.types import EventType, SystemConfigKey
from .bonus_exchange_config import BonusExchangeConfig
from .exchange_001 import Exchange001
lock = threading.Lock()
# 记录最后一次兑换时间，用于控制兑换间隔
last_exchange_time = {}
# 记录每个站点的当前魔力值（用于动态扣除）
site_current_bonus = {}
class BonusExchangePlugin(_PluginBase):
    # 插件名称
    plugin_name = "魔力兑换助手"
    # 插件描述
    plugin_desc = "自动监控分享率，在低于阈值时执行魔力兑换操作"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/InfinityPacer/MoviePilot-Plugins/main/icons/trafficassistant.png"
    # 插件版本
    plugin_version = "1.6"
    # 插件作者
    plugin_author = "Lyzd1"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "bonus_exchange_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 2
    # region 私有属性
    pluginmanager = None
    siteshelper = None
    siteoper = None
    systemconfig = None
    # 插件配置
    _config = BonusExchangeConfig()
    # 定时器
    _scheduler = None
    # 退出事件
    _event = threading.Event()
    # endregion
    def init_plugin(self, config: dict = None):
        self.pluginmanager = PluginManager()
        self.siteshelper = SitesHelper()
        self.siteoper = SiteOper()
        self.systemconfig = SystemConfigOper()
        if not config:
            return
        result, reason = self.__validate_and_fix_config(config=config)
        if not result and not self._config:
            self.__update_config_if_error(config=config, error=reason)
            return
        # 更新站点ID
        if self._config.site_infos and self._config.parsed_exchange_configs:
            self._config.update_site_ids(self._config.site_infos)
        # 打印选中的站点信息
        self.__print_site_info()
        if self._config.onlyonce:
            self._config.onlyonce = False
            self.update_config(config=config)
            logger.info("立即运行一次魔力兑换助手服务")
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(self.exchange_monitor, 'date',
                                    run_date=datetime.now(
                                        tz=pytz.timezone(settings.TZ)
                                    ) + timedelta(seconds=3),
                                    name="魔力兑换助手")
            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()
        self.__update_config()
    def get_state(self) -> bool:
        return self._config and self._config.enabled
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        pass
    def get_api(self) -> List[Dict[str, Any]]:
        pass
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
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
                                            'hint': '开启后插件将处于激活状态',
                                            'persistent-hint': True
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                            'hint': '是否在特定事件发生时发送通知',
                                            'persistent-hint': True
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                            'hint': '插件将立即运行一次',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 8
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'sites',
                                            'label': '站点列表',
                                            'items': self.__get_site_options(),
                                            'hint': '选择参与监控的站点',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4,
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式',
                                            'hint': '使用cron表达式指定执行周期，如 0 */6 * * *',
                                            'persistent-hint': True
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_ratio_check',
                                            'label': '启用分享率检查',
                                            'hint': '开启后监控站点分享率',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'ratio_threshold',
                                            'label': '分享率阈值',
                                            'type': 'number',
                                            "min": "0",
                                            'step': "0.1",
                                            'hint': '设置分享率阈值，低于此值将触发操作',
                                            'persistent-hint': True
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_bonus_check',
                                            'label': '启用魔力检查',
                                            'hint': '开启后监控站点魔力值',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'bonus_threshold',
                                            'label': '魔力阈值',
                                            'type': 'number',
                                            "min": "0",
                                            'step': "1",
                                            'hint': '设置魔力阈值，低于此值将触发操作',
                                            'persistent-hint': True
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'site_exchange_rules',
                                            'label': '站点兑换规则',
                                            'rows': '5',
                                            'placeholder': '每行配置一个站点，格式：站点名称 上传量阈值 兑换规则\n例如：学校 500G 2 5G 2300;3 10G 4200\n表示：当学校站点上传量低于500G时，且魔力大于2300时，可以调用option 2去兑换5G上传量',
                                            'hint': '每行格式：站点名称 上传量阈值 兑换规则1;兑换规则2',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
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
                                            'text': '插件默认每6小时执行一次监控，当分享率或上传量低于阈值时自动执行魔力兑换'
                                        }
                                    }
                                ]
                            },
                        ]
                    }
                ]
            }
        ], {
            "enabled": True,
            "onlyonce": False,
            "notify": True,
            "enable_ratio_check": True,
            "ratio_threshold": 1.0,
            "enable_bonus_check": True,
            "bonus_threshold": 1000.0,
            "cron": "0 */6 * * *",
            "site_exchange_rules": ""
        }
    def get_page(self) -> List[dict]:
        pass
    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if not self._config:
            return []
        if self._config.enabled and self._config.cron:
            return [{
                "id": "BonusExchangePlugin",
                "name": "魔力兑换助手服务",
                "trigger": CronTrigger.from_crontab(self._config.cron),
                "func": self.exchange_monitor,
                "kwargs": {}
            }]
    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
    @eventmanager.register(EventType.SiteRefreshed)
    def exchange_monitor(self, event: Event = None):
        """
        魔力兑换监控服务
        """
        if event:
            event_data = event.event_data
            # 所有站点数据刷新完成即 site_id 为 *，才触发后续服务
            if not event_data or event_data.get("site_id") != "*":
                return
            else:
                logger.info("站点数据刷新完成，立即运行一次魔力兑换助手服务")
        with lock:
            config = self._config
            success, reason = self.__validate_config(config=config, force=True)
            if not success:
                err_msg = f"配置异常，原因：{reason}"
                logger.error(err_msg)
                self.__send_message(title="魔力兑换助手", message=err_msg)
                return
            # 获取站点统计数据
            result = self.__get_site_statistics()
            if result.get("success"):
                site_statistics = result.get("data")
                logger.debug(f"数据获取成功，获取到 {len(site_statistics)} 个站点的数据")
                # 详细打印每个站点的数据
                self.__print_detailed_statistics(site_statistics)
                # 执行监控逻辑
                self.__monitor_sites(config=config, site_statistics=site_statistics)
            else:
                error_msg = result.get("err_msg", "魔力兑换助手发生异常，请检查日志")
                logger.error(error_msg)
                self.__send_message(title="魔力兑换助手", message=error_msg)
    def __print_detailed_statistics(self, site_statistics: dict):
        """精简打印站点统计数据 - 仅保留分享率、魔力值、上传量"""
        logger.info("=== 站点数据摘要 ===")
        for site_name, site_data in site_statistics.items():
            if site_data.get('success'):
                ratio = site_data.get('ratio', 'N/A')
                bonus = site_data.get('bonus', 'N/A')
                upload = site_data.get('upload', 'N/A')
                stat_time = site_data.get('statistic_time', 'N/A')
                # 将上传量从字节转换为GB
                upload_gb = upload / (1024 * 1024 * 1024)
                logger.info(f"站点: {site_name} (数据日期: {stat_time})")
                logger.info(f"  分享率: {ratio}, 魔力值: {bonus}, 上传量: {upload_gb:.2f}GB")
            else:
                logger.info(f"站点: {site_name} - 数据获取失败: {site_data.get('err_msg', '未知错误')}")
            logger.info("---")
    def __monitor_sites(self, config: BonusExchangeConfig, site_statistics: dict):
        """监控站点数据并执行相应操作"""
        aggregated_messages = []
        exchange_results = []
        # 初始化站点魔力值
        global site_current_bonus
        for site_id, site_info in config.site_infos.items():
            site_name = site_info.name
            site_stat = site_statistics.get(site_name)
            if site_stat and site_stat.get("success"):
                bonus_str = site_stat.get("bonus")
                if bonus_str:
                    try:
                        site_current_bonus[site_name] = float(bonus_str)
                        logger.debug(f"站点 {site_name}: 初始化魔力值为 {site_current_bonus[site_name]}")
                    except ValueError:
                        site_current_bonus[site_name] = 0
        for site_id, site_info in config.site_infos.items():
            site_name = site_info.name
            site_stat = site_statistics.get(site_name)
            if not site_stat:
                message = f"站点 {site_name}: 无统计数据"
                logger.warning(message)
                aggregated_messages.append(message)
                continue
            if not site_stat.get("success"):
                message = f"站点 {site_name}: 数据获取失败 - {site_stat.get('err_msg', '未知错误')}"
                logger.warning(message)
                aggregated_messages.append(message)
                continue
            # 检查分享率
            if config.enable_ratio_check:
                ratio_result = self.__check_ratio(config=config, site_name=site_name, site_stat=site_stat)
                aggregated_messages.append(ratio_result)
            # 检查魔力值
            if config.enable_bonus_check:
                bonus_result = self.__check_bonus(config=config, site_name=site_name, site_stat=site_stat)
                aggregated_messages.append(bonus_result)
            # 检查是否需要执行兑换，支持连续兑换
            exchange_results.extend(self.__execute_continuous_exchange(config=config, site_info=site_info, site_stat=site_stat))
        # 发送聚合消息
        if aggregated_messages:
            full_message = "\n".join(aggregated_messages)
            self.__send_message(title="魔力兑换助手监控结果", message=full_message)
        # 发送兑换结果消息
        if exchange_results:
            exchange_message = "\n".join(exchange_results)
            self.__send_message(title="魔力兑换助手兑换结果", message=exchange_message)
    def __check_ratio(self, config: BonusExchangeConfig, site_name: str, site_stat: dict) -> str:
        """检查分享率"""
        ratio_str = site_stat.get("ratio")
        if ratio_str is None:
            return f"站点 {site_name}: 分享率数据缺失"
        try:
            ratio = float(ratio_str)
        except ValueError:
            return f"站点 {site_name}: 分享率格式错误 - {ratio_str}"
        stat_time = site_stat.get("statistic_time", "N/A")
        if ratio <= config.ratio_threshold:
            # 分享率低于阈值，需要检查魔力值
            logger.debug(f"站点 {site_name}: 当前分享率: {ratio} ≤ 阈值: {config.ratio_threshold}")
            return f"站点 {site_name}: 当前分享率: {ratio} ≤ 阈值: {config.ratio_threshold}"
        else:
            logger.debug(f"站点 {site_name}: 当前分享率: {ratio} > 阈值: {config.ratio_threshold}")
            return f"站点 {site_name}: 当前分享率: {ratio} > 阈值: {config.ratio_threshold}"
    def __get_site_statistics(self) -> dict:
        """获取站点统计数据"""
        def is_data_valid(data):
            """检查数据是否有效"""
            return data is not None and "ratio" in data and not data.get("err_msg")
        config = self._config
        site_infos = config.site_infos
        current_day = datetime.now(tz=pytz.timezone(settings.TZ)).date()
        previous_day = current_day - timedelta(days=1)
        result = {"success": True, "data": {}}
        # 尝试获取当天和前一天的数据
        current_data = {data.name: data for data in
                        (self.siteoper.get_userdata_by_date(date=str(current_day)) or [])}
        previous_day_data = {data.name: data for data in
                             (self.siteoper.get_userdata_by_date(date=str(previous_day)) or [])}
        if not current_data and not previous_day_data:
            err_msg = f"{current_day} 和 {previous_day}，均没有获取到有效的数据，请检查"
            logger.warning(err_msg)
            result["success"] = False
            result["err_msg"] = err_msg
            return result
        # 检查每个站点的数据是否有效
        all_sites_failed = True
        for site_id, site in site_infos.items():
            site_name = site.name
            site_current_data = current_data.get(site_name)
            site_current_data = site_current_data.to_dict() if site_current_data else {}
            site_previous_data = previous_day_data.get(site_name)
            site_previous_data = site_previous_data.to_dict() if site_previous_data else {}
            if is_data_valid(site_current_data):
                result["data"][site_name] = {**site_current_data, "success": True,
                                             "statistic_time": str(current_day)}
                all_sites_failed = False
            else:
                if is_data_valid(site_previous_data):
                    result["data"][site_name] = {**site_previous_data, "success": True,
                                                 "statistic_time": str(previous_day)}
                    logger.debug(f"站点 {site_name} 使用了 {previous_day} 的数据")
                    all_sites_failed = False
                else:
                    err_msg = site_previous_data.get("err_msg", "无有效数据")
                    result["data"][site_name] = {"err_msg": err_msg, "success": False,
                                                 "statistic_time": str(previous_day)}
                    logger.warning(f"{site_name} 前一天的数据也无效，错误信息：{err_msg}")
        # 如果所有站点的数据都无效，则标记全局失败
        if all_sites_failed:
            err_msg = f"{current_day} 和 {previous_day}，所有站点的数据获取均失败，无法继续监控服务"
            logger.warning(err_msg)
            result["success"] = False
            result["err_msg"] = err_msg
        return result
    def __send_message(self, title: str, message: str):
        """发送消息"""
        if self._config.notify:
            self.post_message(mtype=NotificationType.Plugin, title=f"【{title}】", text=message)
    def __validate_config(self, config: BonusExchangeConfig, force: bool = False) -> (bool, str):
        """验证配置是否有效"""
        if not config.enabled and not force:
            return True, "插件未启用，无需进行验证"
        # 检查站点列表是否为空
        if not config.sites:
            return False, "站点列表不能为空"
        # 检查分享率阈值是否有效
        if config.enable_ratio_check:
            if config.ratio_threshold <= 0:
                return False, "分享率阈值必须大于0"
        # 检查魔力阈值是否有效
        if config.enable_bonus_check:
            if config.bonus_threshold < 0:
                return False, "魔力阈值不能小于0"
        return True, "所有配置项都有效"
    def __validate_and_fix_config(self, config: dict = None) -> [bool, str]:
        """检查并修正配置值"""
        if not config:
            return False, ""
        try:
            # 使用字典推导来提取所有字段，并用config中的值覆盖默认值
            plugin_config = BonusExchangeConfig(
                **{field.name: config.get(field.name, getattr(BonusExchangeConfig, field.name, None))
                   for field in fields(BonusExchangeConfig)})
            result, reason = self.__validate_config(config=plugin_config)
            if result:
                # 过滤掉已删除的站点并保存
                if plugin_config.sites:
                    site_id_to_public_status = {site.get("id"): site.get("public") for site in
                                                self.siteshelper.get_indexers()}
                    plugin_config.sites = [
                        site_id for site_id in plugin_config.sites
                        if site_id in site_id_to_public_status and not site_id_to_public_status[site_id]
                    ]
                    site_infos = {}
                    for site_id in plugin_config.sites:
                        site_info = self.siteoper.get(site_id)
                        if site_info:
                            site_infos[site_id] = site_info
                    plugin_config.site_infos = site_infos
                    # 更新站点ID
                    if plugin_config.parsed_exchange_configs:
                        plugin_config.update_site_ids(site_infos)
                self._config = plugin_config
                return True, ""
            else:
                self._config = None
                return result, reason
        except YAMLError as e:
            self._config = None
            logger.error(e)
            return False, str(e)
        except Exception as e:
            self._config = None
            logger.error(e)
            return False, str(e)
    def __update_config_if_error(self, config: dict = None, error: str = None):
        """异常时停用插件并保存配置"""
        if config:
            if config.get("enabled", False) or config.get("onlyonce", False):
                config["enabled"] = False
                config["onlyonce"] = False
                self.__log_and_notify_error(
                    f"配置异常，已停用魔力兑换助手，原因：{error}" if error else "配置异常，已停用魔力兑换助手，请检查")
            self.update_config(config)
    def __update_config(self):
        """保存配置"""
        config_mapping = asdict(self._config)
        del config_mapping["site_infos"]
        self.update_config(config_mapping)
    def __log_and_notify_error(self, message):
        """记录错误日志并发送系统通知"""
        logger.error(message)
        self.systemmessage.put(message, title="魔力兑换助手")
    def __print_site_info(self):
        """打印选中的站点信息"""
        if not self._config or not self._config.site_infos:
            logger.info("没有配置站点信息")
            return
        logger.info("=== 选中的站点信息 ===")
        for site_id, site_info in self._config.site_infos.items():
            logger.debug(f"站点ID: {site_id}")
            logger.debug(f"  站点名称: {site_info.name}")
            logger.debug(f"  站点域名: {site_info.domain}")
            logger.debug(f"  Cookie长度: {len(site_info.cookie) if site_info.cookie else 0}")
            logger.debug(f"  User-Agent: {site_info.ua if site_info.ua else '默认'}")
            logger.debug(f"  使用代理: {site_info.proxy}")
            # 打印兑换规则
            exchange_rules = self._config.get_exchange_rules_for_site(site_info.name)
            if exchange_rules:
                for rule in exchange_rules:
                    # 如果上传量阈值为0G，则不显示上传量阈值
                    if rule.upload_threshold == "0G":
                        logger.info(f"站点名称: {site_info.name} - 选项: {rule.option}, 上传量: {rule.upload_amount}, 魔力消耗: {rule.bonus_cost}")
                    else:
                        logger.info(f"站点名称: {site_info.name} - 上传量阈值: {rule.upload_threshold}, 选项: {rule.option}, 上传量: {rule.upload_amount}, 魔力消耗: {rule.bonus_cost}")
            else:
                logger.info(f"站点名称: {site_info.name} - 无兑换规则,不可兑换")
            logger.info("---")
    def __check_bonus(self, config: BonusExchangeConfig, site_name: str, site_stat: dict) -> str:
        """检查魔力值"""
        bonus_str = site_stat.get("bonus")
        if bonus_str is None:
            return f"站点 {site_name}: 魔力值数据缺失"
        try:
            bonus = float(bonus_str)
        except ValueError:
            return f"站点 {site_name}: 魔力值格式错误 - {bonus_str}"
        stat_time = site_stat.get("statistic_time", "N/A")
        if bonus > config.bonus_threshold:
            # 魔力值大于阈值，可以执行兑换操作
            logger.debug(f"站点 {site_name}: 当前魔力值: {bonus} > 阈值: {config.bonus_threshold}")
            return f"站点 {site_name}: 当前魔力值: {bonus} > 阈值: {config.bonus_threshold}"
        else:
            logger.debug(f"站点 {site_name}: 当前魔力值: {bonus} ≤ 阈值: {config.bonus_threshold}")
            return f"站点 {site_name}: 当前魔力值: {bonus} ≤ 阈值: {config.bonus_threshold}"
    def __check_and_execute_exchange(self, config: BonusExchangeConfig, site_info, site_stat: dict) -> str:
        """检查并执行兑换操作"""
        site_name = site_info.name
        # 获取站点的兑换规则
        exchange_rules = config.get_exchange_rules_for_site(site_name)
        if not exchange_rules:
            return None
        # 获取分享率、上传量和魔力值
        ratio_str = site_stat.get("ratio")
        upload_str = site_stat.get("upload")
        bonus_str = site_stat.get("bonus")
        if ratio_str is None or upload_str is None or bonus_str is None:
            return f"站点 {site_name}: 分享率、上传量或魔力值数据缺失"
        try:
            current_ratio = float(ratio_str)
            current_upload_bytes = float(upload_str)
            current_bonus = float(bonus_str)
            # 将上传量从字节转换为GB
            current_upload_gb = current_upload_bytes / (1024 * 1024 * 1024)
        except ValueError:
            return f"站点 {site_name}: 分享率、上传量或魔力值格式错误"
        # 获取当前动态魔力值
        global site_current_bonus
        if site_name not in site_current_bonus:
            site_current_bonus[site_name] = current_bonus
            logger.info(f"站点 {site_name}: 初始化动态魔力值为 {site_current_bonus[site_name]}")
        # 检查是否满足兑换条件
        # 首先对兑换规则按魔力消耗从高到低排序
        sorted_rules = sorted(exchange_rules, key=lambda x: float(x.bonus_cost), reverse=True)
        for rule in sorted_rules:
            try:
                upload_threshold = float(rule.upload_threshold.replace('G', '').replace('g', ''))
                bonus_cost = float(rule.bonus_cost)
                # 检查兑换条件：
                # 情况一：分享率低于阈值且魔力值大于阈值
                # 情况二：上传量小于阈值且魔力值大于阈值
                should_exchange = False
                if config.enable_ratio_check and current_ratio <= config.ratio_threshold and site_current_bonus[site_name] > config.bonus_threshold:
                    should_exchange = True
                    logger.info(f"站点 {site_name}: 满足情况一（分享率低且魔力值高），准备兑换")
                elif current_upload_gb <= upload_threshold and site_current_bonus[site_name] > config.bonus_threshold:
                    should_exchange = True
                    logger.info(f"站点 {site_name}: 满足情况二（上传量低且魔力值高），准备兑换")
                # 如果满足兑换条件，检查具体兑换规则
                if should_exchange and site_current_bonus[site_name] >= bonus_cost:
                    # 检查兑换间隔
                    if not self.__can_execute_exchange(site_name):
                        return f"站点 {site_name}: 距离上次兑换不足30秒，跳过本次兑换"
                    # 执行兑换
                    success, message = self.__execute_exchange(site_info, rule)
                    # 更新最后兑换时间
                    global last_exchange_time
                    last_exchange_time[site_name] = time.time()
                    # 只有在兑换成功时才扣除魔力值
                    if success:
                        site_current_bonus[site_name] -= bonus_cost
                        logger.info(f"站点 {site_name}: 兑换成功，扣除 {bonus_cost} 魔力，剩余魔力值: {site_current_bonus[site_name]}")
                        # 兑换成功后，返回特殊标记表示需要继续兑换
                        return f"CONTINUE_EXCHANGE|站点 {site_name}: 兑换成功 - {message}"
                    else:
                        # 兑换失败时，停止继续尝试其他规则
                        logger.info(f"站点 {site_name}: 兑换失败，停止继续尝试其他兑换规则")
                        return f"站点 {site_name}: 兑换失败 - {message}"
            except ValueError:
                continue
        return None
    def __execute_continuous_exchange(self, config: BonusExchangeConfig, site_info, site_stat: dict) -> list:
        """执行连续兑换，每次最多兑换5次"""
        exchange_results = []
        max_exchanges = 5  # 每次最多兑换5次
        exchange_count = 0
        # 第一次检查是否满足兑换条件
        initial_should_exchange = False
        site_name = site_info.name
        # 获取初始数据
        ratio_str = site_stat.get("ratio")
        upload_str = site_stat.get("upload")
        bonus_str = site_stat.get("bonus")
        try:
            current_ratio = float(ratio_str) if ratio_str else 0
            current_upload_bytes = float(upload_str) if upload_str else 0
            current_bonus = float(bonus_str) if bonus_str else 0
            # 将上传量从字节转换为GB
            current_upload_gb = current_upload_bytes / (1024 * 1024 * 1024)
            # 获取上传量阈值（从第一个兑换规则获取）
            upload_threshold_str = config.get_exchange_rules_for_site(site_name)[0].upload_threshold
            upload_threshold_gb = float(upload_threshold_str.replace('G', '').replace('g', ''))
            # 如果上传量阈值不为0G，则显示上传量阈值
            if upload_threshold_gb != 0:
                logger.debug(f"站点 {site_name}: 上传量 {current_upload_gb:.2f} GB  上传量阈值 = {upload_threshold_gb} GB")
            # 检查是否满足初始兑换条件（必须同时满足魔力值大于阈值）
            if current_bonus > config.bonus_threshold:
                if config.enable_ratio_check and current_ratio <= config.ratio_threshold:
                    initial_should_exchange = True
                    logger.info(f"站点 {site_name}: 满足情况一（分享率低且魔力值高），开始连续兑换")
                elif upload_threshold_gb > 0 and current_upload_gb <= upload_threshold_gb:
                    initial_should_exchange = True
                    logger.info(f"站点 {site_name}: 满足情况二（上传量低且魔力值高），开始连续兑换")
                else:
                    logger.debug(f"站点 {site_name}: 魔力值足够但其他条件不满足，跳过连续兑换")
            else:
                logger.debug(f"站点 {site_name}: 魔力值不足（{current_bonus} ≤ {config.bonus_threshold}），跳过连续兑换")
        except (ValueError, TypeError, IndexError):
            logger.warning(f"站点 {site_name}: 初始数据解析失败，无法开始连续兑换")
            return exchange_results
        if not initial_should_exchange:
            logger.info(f"站点 {site_name}: 不满足初始兑换条件，跳过连续兑换")
            return exchange_results
        while exchange_count < max_exchanges:
            # 在连续兑换中，只检查魔力值是否足够
            result = self.__check_and_execute_exchange_continuous(config=config, site_info=site_info)
            if not result:
                # 没有兑换结果，说明魔力值不足
                break
            # 检查是否需要继续兑换
            if result.startswith("CONTINUE_EXCHANGE|"):
                # 兑换成功，需要继续兑换
                exchange_results.append(result.replace("CONTINUE_EXCHANGE|", ""))
                exchange_count += 1
                if exchange_count >= max_exchanges:
                    logger.info(f"站点 {site_name}: 已达到本次最大兑换次数({max_exchanges})，停止兑换")
                    break
                logger.info(f"站点 {site_name}: 兑换成功({exchange_count}/{max_exchanges})，等待30秒后继续判断...")
                # 等待30秒
                time.sleep(30)
            else:
                # 兑换失败
                exchange_results.append(result)
                break
        if exchange_count > 0:
            logger.info(f"站点 {site_name}: 本次共执行 {exchange_count} 次兑换")
        return exchange_results
    def __check_and_execute_exchange_continuous(self, config: BonusExchangeConfig, site_info) -> str:
        """连续兑换专用：只检查魔力值是否足够"""
        site_name = site_info.name
        # 获取站点的兑换规则
        exchange_rules = config.get_exchange_rules_for_site(site_name)
        if not exchange_rules:
            return None
        # 首先对兑换规则按魔力消耗从高到低排序
        sorted_rules = sorted(exchange_rules, key=lambda x: float(x.bonus_cost), reverse=True)
        for rule in sorted_rules:
            try:
                bonus_cost = float(rule.bonus_cost)
                # 在连续兑换中，只检查魔力值是否足够
                global site_current_bonus
                if site_name not in site_current_bonus:
                    logger.warning(f"站点 {site_name}: 在连续兑换中未找到动态魔力值")
                    return None
                if site_current_bonus[site_name] >= bonus_cost:
                    # 检查兑换间隔
                    if not self.__can_execute_exchange(site_name):
                        return f"站点 {site_name}: 距离上次兑换不足30秒，跳过本次兑换"
                    # 执行兑换
                    success, message = self.__execute_exchange(site_info, rule)
                    # 更新最后兑换时间
                    global last_exchange_time
                    last_exchange_time[site_name] = time.time()
                    # 只有在兑换成功时才扣除魔力值
                    if success:
                        site_current_bonus[site_name] -= bonus_cost
                        logger.info(f"站点 {site_name}: 兑换成功，扣除 {bonus_cost} 魔力，剩余魔力值: {site_current_bonus[site_name]}")
                        # 兑换成功后，返回特殊标记表示需要继续兑换
                        return f"CONTINUE_EXCHANGE|站点 {site_name}: 兑换成功 - {message}"
                    else:
                        # 兑换失败时，停止继续尝试其他规则
                        logger.info(f"站点 {site_name}: 兑换失败，停止继续尝试其他兑换规则")
                        return f"站点 {site_name}: 兑换失败 - {message}"
            except ValueError:
                continue
        # 没有找到可以兑换的规则（魔力值不足）
        logger.info(f"站点 {site_name}: 魔力值不足，无法继续兑换")
        return None
    def __can_execute_exchange(self, site_name: str) -> bool:
        """检查是否可以执行兑换（30秒间隔控制）"""
        global last_exchange_time
        current_time = time.time()
        if site_name not in last_exchange_time:
            return True
        time_diff = current_time - last_exchange_time[site_name]
        return time_diff >= 30  # 30秒间隔
    def __execute_exchange(self, site_info, rule) -> (bool, str):
        """执行兑换操作"""
        try:
            # --- 修改开始 ---
            # 尝试获取站点完整的 URL (从 Indexer 配置中获取)
            # site_info.domain 只是主域名，可能缺失子域名(如 pt.)或协议头
            site_url = f"https://{site_info.domain}"
            
            try:
                # 获取所有索引器配置
                indexers = self.siteshelper.get_indexers()
                for indexer in indexers:
                    # 匹配站点ID
                    if indexer.get("id") == site_info.id:
                        indexer_url = indexer.get("domain")
                        if indexer_url and indexer_url.startswith("http"):
                            site_url = indexer_url
                            logger.info(f"站点 {site_info.name}: 使用配置的完整URL -> {site_url}")
                        break
            except Exception as e:
                logger.warning(f"获取站点 {site_info.name} 完整URL失败，回退到默认拼接: {e}")
            # 去除 URL 末尾的斜杠，避免拼接时出现 //
            site_url = site_url.rstrip('/')
            # 创建兑换器
            exchanger = Exchange001(
                site_name=site_info.name,
                site_url=site_url,
                cookie=site_info.cookie,
                ua=site_info.ua
            )
            # --- 修改结束 ---
            # 执行兑换
            success, message = exchanger.execute_exchange(
                option=rule.option,
                upload_amount=rule.upload_amount,
                bonus_cost=rule.bonus_cost
            )
            return success, message
        except Exception as e:
            logger.error(f"执行兑换时发生错误: {str(e)}")
            return False, f"兑换过程发生错误: {str(e)}"
    def __get_site_options(self):
        """获取当前可选的站点"""
        site_options = [{"title": site.get("name"), "value": site.get("id")}
                        for site in self.siteshelper.get_indexers()]
        return site_options
