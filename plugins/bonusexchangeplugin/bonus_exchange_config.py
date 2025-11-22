from dataclasses import dataclass, field
from typing import List, Optional, Dict


@dataclass
class ExchangeRule:
    """单个兑换规则"""
    option: str  # 兑换选项编号
    upload_amount: str  # 上传量（GB）
    bonus_cost: str  # 魔力消耗
    upload_threshold: str  # 上传量阈值（GB）

    def __str__(self):
        return f"{self.option} {self.upload_amount} {self.bonus_cost}"


@dataclass
class SiteExchangeConfig:
    """站点兑换配置"""
    site_id: int
    site_name: str
    exchange_rules: List[ExchangeRule] = field(default_factory=list)
    exchange_class: str = "exchange_001"  # 默认使用001类兑换


@dataclass
class BonusExchangeConfig:
    """
    魔力兑换插件配置类
    """
    enabled: Optional[bool] = True  # 启用插件，默认开启
    sites: List[int] = field(default_factory=list)  # 站点列表
    site_infos: dict = None  # 站点信息字典
    onlyonce: Optional[bool] = False  # 立即运行一次
    notify: Optional[bool] = True  # 发送通知，默认开启
    cron: Optional[str] = "0 */6 * * *"  # 执行周期，默认每6小时执行一次

    # 监控阈值配置
    ratio_threshold: Optional[float] = 1.0  # 分享率阈值
    enable_ratio_check: Optional[bool] = True  # 启用分享率检查，默认开启

    bonus_threshold: Optional[float] = 1000.0  # 魔力阈值
    enable_bonus_check: Optional[bool] = True  # 启用魔力检查，默认开启

    # 站点兑换规则配置（文本格式：每行 站点名称 上传量阈值 兑换规则）
    # 例如：学校 500G 2 5G 2300;3 10G 4200
    site_exchange_rules: str = ""

    # 解析后的站点兑换配置
    parsed_exchange_configs: Dict[str, SiteExchangeConfig] = None

    def __post_init__(self):
        # 类型转换
        self.ratio_threshold = self._convert_float(self.ratio_threshold, 1.0)
        self.bonus_threshold = self._convert_float(self.bonus_threshold, 1000.0)

        # 初始化字典属性
        if self.site_infos is None:
            self.site_infos = {}
        if self.parsed_exchange_configs is None:
            self.parsed_exchange_configs = {}

        # 解析兑换规则
        self.parsed_exchange_configs = self._parse_exchange_rules()

    @staticmethod
    def _convert_float(value, default):
        """转换为浮点数"""
        try:
            return float(value) if value is not None else default
        except (ValueError, TypeError):
            return default

    def _parse_exchange_rules(self) -> Dict[str, SiteExchangeConfig]:
        """解析站点兑换规则"""
        if not self.site_exchange_rules:
            return {}

        configs = {}
        lines = self.site_exchange_rules.strip().split('\n')

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 格式：站点名称 上传量阈值 规则1;规则2;规则3
            parts = line.split(' ', 2)
            if len(parts) < 3:
                continue

            try:
                site_name = parts[0]
                upload_threshold = parts[1]
                rules_str = parts[2]

                # 解析多个规则
                exchange_rules = []
                rule_parts = rules_str.split(';')

                for rule_str in rule_parts:
                    rule_str = rule_str.strip()
                    if not rule_str:
                        continue

                    rule_parts = rule_str.split()
                    if len(rule_parts) == 3:
                        exchange_rules.append(ExchangeRule(
                            option=rule_parts[0],
                            upload_amount=rule_parts[1],
                            bonus_cost=rule_parts[2],
                            upload_threshold=upload_threshold
                        ))

                if exchange_rules:
                    # 使用站点名称作为key
                    configs[site_name] = SiteExchangeConfig(
                        site_id=0,  # 稍后会更新为真实ID
                        site_name=site_name,
                        exchange_rules=exchange_rules
                    )

            except (ValueError, IndexError):
                continue

        return configs

    def get_exchange_rules_for_site(self, site_name: str) -> List[ExchangeRule]:
        """获取指定站点的兑换规则"""
        if self.parsed_exchange_configs and site_name in self.parsed_exchange_configs:
            return self.parsed_exchange_configs[site_name].exchange_rules
        return []

    def update_site_ids(self, site_infos: dict):
        """更新站点ID"""
        if not self.parsed_exchange_configs:
            return

        for site_name, config in self.parsed_exchange_configs.items():
            for site_id, site_info in site_infos.items():
                if site_info.name == site_name:
                    config.site_id = site_id
                    break