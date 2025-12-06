import requests
import json
from typing import Tuple
from app.log import logger


class ExchangeMteam:
    """
    馒头(M-Team)站点魔力兑换规则
    """

    def __init__(self, site_name: str, api_key: str, current_bonus: float, base_url: str = 'https://api.m-team.io', goods_id: int = 1):
        self.site_name = site_name
        self.api_key = api_key
        self.current_bonus = current_bonus
        self.base_url = base_url.rstrip('/')
        self.goods_id = goods_id
        self.exchange_url = f"{self.base_url}/api/mall/exchange"

    def execute_exchange(self, **kwargs) -> Tuple[bool, str]:
        """
        执行魔力兑换操作
        :return: (是否成功, 消息)
        """
        if not self.api_key:
            return False, "API Key为空，无法执行兑换"

        # 计算兑换数量 (魔力值/500=1G上传量)
        quantity = int(self.current_bonus // 500)
        if quantity <= 0:
            return False, "魔力值不足，无法兑换"

        # 设置请求头
        headers = {
            'x-api-key': self.api_key,
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        # 准备请求数据
        payload = {
            'goodsId': self.goods_id,
            'num': quantity
        }

        try:
            logger.info(f"执行站点 {self.site_name} 的魔力兑换")
            logger.debug(f"兑换URL: {self.exchange_url}")
            logger.debug(f"请求参数: goodsId={self.goods_id}, num={quantity}")

            # 发送POST请求
            response = requests.post(self.exchange_url, headers=headers, data=payload, timeout=30)

            # 检查HTTP状态码
            if response.status_code != 200:
                message = f"兑换失败：HTTP状态码 {response.status_code}"
                logger.warning(message)
                logger.debug(f"响应内容: {response.text}")
                return False, message

            # 解析JSON响应
            try:
                response_data = response.json()
            except json.JSONDecodeError:
                message = "兑换失败：无法解析服务器响应"
                logger.error(message)
                logger.debug(f"原始响应: {response.text}")
                return False, message

            logger.debug(f"服务器响应: {json.dumps(response_data, indent=2, ensure_ascii=False)}")

            # 检查业务逻辑是否成功
            # 根据用户要求，检查message字段是否为"SUCCESS"
            if response_data.get('message') == 'SUCCESS':
                message = f"兑换成功！消耗 {quantity * 500} 魔力获得 {quantity}G 上传量"
                logger.info(message)
                return True, message
            else:
                message = f"兑换失败：{response_data.get('message', '未知错误')}"
                logger.warning(message)
                return False, message

        except requests.exceptions.Timeout:
            error_msg = "兑换请求超时（30秒），请检查网络连接或站点是否正常"
            logger.error(error_msg)
            return False, error_msg
        except requests.exceptions.RequestException as e:
            error_msg = f"兑换请求网络错误: {str(e)}"
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            error_msg = f"兑换过程中发生未知错误: {str(e)}"
            logger.error(error_msg)
            return False, error_msg