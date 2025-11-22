import requests
from typing import Dict, Optional, Tuple
from app.log import logger


class Exchange001:
    """
    001类兑换规则 - 适用于学校等站点
    兑换规则格式：option upload_amount bonus_cost
    例如：2 5 2300 表示使用2300魔力兑换5GB上传量
    """

    def __init__(self, site_name: str, site_url: str, cookie: str):
        self.site_name = site_name
        self.site_url = site_url
        self.cookie = cookie
        self.exchange_url = f"{site_url}/mybonus.php?action=exchange"

    def execute_exchange(self, option: str, upload_amount: str, bonus_cost: str) -> Tuple[bool, str]:
        """
        执行魔力兑换操作

        Args:
            option: 兑换选项编号
            upload_amount: 上传量（GB）
            bonus_cost: 魔力消耗

        Returns:
            (success, message): 兑换是否成功和结果信息
        """
        if not self.cookie:
            return False, "Cookie为空，无法执行兑换"

        # 准备请求数据
        payload = {
            'option': option,
            'submit': 'Exchange'
        }

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Cookie': self.cookie
        }

        try:
            logger.info(f"执行站点 {self.site_name} 的魔力兑换")
            logger.info(f"兑换选项: {option}, 上传量: {upload_amount}GB, 魔力消耗: {bonus_cost}")

            response = requests.post(self.exchange_url, headers=headers, data=payload, timeout=10)
            response.raise_for_status()

            # 检查兑换结果
            if "兑换成功" in response.text or "exchange successful" in response.text.lower():
                message = f"兑换成功！消耗 {bonus_cost} 魔力获得 {upload_amount}GB 上传量"
                logger.info(message)
                return True, message
            else:
                message = "兑换失败：响应中未找到成功信息"
                logger.warning(message)
                return False, message

        except requests.exceptions.RequestException as e:
            error_msg = f"兑换请求失败: {str(e)}"
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            error_msg = f"兑换过程中发生未知错误: {str(e)}"
            logger.error(error_msg)
            return False, error_msg