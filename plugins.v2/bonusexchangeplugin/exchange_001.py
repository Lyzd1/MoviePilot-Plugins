import requests
from typing import Tuple
from app.log import logger


class Exchange001:
    """
    001类兑换规则 - 适用于学校等NexusPHP站点
    """

    def __init__(self, site_name: str, site_url: str, cookie: str, ua: str = None):
        self.site_name = site_name
        self.site_url = site_url
        self.cookie = cookie
        # 如果没有传入UA，使用一个默认的较新的UA
        self.ua = ua if ua else 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        self.exchange_url = f"{site_url}/mybonus.php?action=exchange"
        # 常见的兑换失败提示关键词，命中即判定失败（站点个性化提示语可在此补充）
        self.failure_markers = ("魔力值不足", "魔力不足", "积分不足", "not enough", "insufficient")

    @staticmethod
    def __parse_amount(value, default: float = 0.0) -> float:
        """解析数值，失败时返回默认值"""
        try:
            return float(str(value).replace('G', '').replace('g', ''))
        except (TypeError, ValueError):
            return default

    def execute_exchange(self, option: str = None, upload_amount: str = None,
                         bonus_cost: str = None, **kwargs) -> Tuple[bool, str, float, float]:
        """
        执行魔力兑换操作
        :return: (是否成功, 消息, 实际消耗魔力, 实际获得上传量GB)
        """
        if not self.cookie:
            return False, "Cookie为空，无法执行兑换", 0, 0
        if not self.site_url:
            return False, "站点域名为空，无法执行兑换", 0, 0

        # 001类站点按所配置的选项价格兑换，实际消耗与获得即规则配置值
        actual_bonus_cost = self.__parse_amount(bonus_cost)
        actual_upload_gb = self.__parse_amount(upload_amount)

        # 准备请求数据
        payload = {
            'option': option,
            'submit': 'Exchange'
        }

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': self.ua,
            'Cookie': self.cookie
        }

        try:
            logger.info(f"执行站点 {self.site_name} 的魔力兑换")
            logger.debug(f"兑换URL: {self.exchange_url}")
            logger.debug(f"请求参数: option={option}")

            logger.debug("正在发送兑换请求...")

            # 设置超时时间为 30 秒，避免无限等待
            response = requests.post(self.exchange_url, headers=headers, data=payload, timeout=30)

            # 检查响应状态码
            response.raise_for_status()

            logger.debug(f"请求完成，状态码: {response.status_code}")

            if response.status_code != 200:
                message = f"兑换失败：HTTP状态码 {response.status_code}"
                logger.warning(message)
                return False, message, 0, 0

            # NexusPHP 站点兑换失败时同样返回 HTTP 200 的错误页面，必须结合响应内容判断
            response_text = response.text or ""
            # Cookie 失效时通常被重定向到登录页
            if "login.php" in (response.url or ""):
                message = "兑换失败：响应为登录页，Cookie可能已失效"
                logger.warning(message)
                return False, message, 0, 0
            marker = next((m for m in self.failure_markers if m in response_text), None)
            if marker:
                message = f"兑换失败：站点返回失败提示（{marker}）"
                logger.warning(message)
                logger.debug(f"页面响应摘要: {response_text[:200]}...")
                return False, message, 0, 0

            message = f"兑换成功！消耗 {bonus_cost} 魔力获得 {upload_amount} 上传量"
            logger.info(message)
            return True, message, actual_bonus_cost, actual_upload_gb

        except requests.exceptions.Timeout:
            error_msg = "兑换请求超时（30秒），请检查网络连接或站点是否正常"
            logger.error(error_msg)
            return False, error_msg, 0, 0
        except requests.exceptions.RequestException as e:
            error_msg = f"兑换请求网络错误: {str(e)}"
            logger.error(error_msg)
            return False, error_msg, 0, 0
        except Exception as e:
            error_msg = f"兑换过程中发生未知错误: {str(e)}"
            logger.error(error_msg)
            return False, error_msg, 0, 0
