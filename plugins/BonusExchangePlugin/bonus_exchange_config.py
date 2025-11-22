from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class BonusExchangeConfig:
    """
    魔力兑换插件配置类
    """
    enabled: Optional[bool] = False  # 启用插件
    sites: List[int] = field(default_factory=list)  # 站点列表
    site_infos: dict = None  # 站点信息字典
    onlyonce: Optional[bool] = False  # 立即运行一次
    notify: Optional[bool] = False  # 发送通知
    cron: Optional[str] = None  # 执行周期

    # 分享率阈值配置
    ratio_threshold: Optional[float] = 1.0  # 分享率阈值
    enable_ratio_check: Optional[bool] = False  # 启用分享率检查

    def __post_init__(self):
        # 类型转换
        self.ratio_threshold = self._convert_float(self.ratio_threshold, 1.0)

    @staticmethod
    def _convert_float(value, default):
        """转换为浮点数"""
        try:
            return float(value) if value is not None else default
        except (ValueError, TypeError):
            return default