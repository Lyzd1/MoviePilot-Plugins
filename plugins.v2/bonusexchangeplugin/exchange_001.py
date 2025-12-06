import requests
from typing import Tuple
# --- 修改点1: 引用正确的系统日志模块 ---
from app.log import logger 

class Exchange001:
    """
    001类兑换规则 - 适用于学校等站点
    """

    def __init__(self, site_name: str, site_url: str, cookie: str, ua: str = None):
        self.site_name = site_name
        self.site_url = site_url
        self.cookie = cookie
        # 如果没有传入UA，使用一个默认的较新的UA
        self.ua = ua if ua else 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        self.exchange_url = f"{site_url}/mybonus.php?action=exchange"

    def execute_exchange(self, option: str = None, upload_amount: str = None, bonus_cost: str = None, **kwargs) -> Tuple[bool, str]:
        """
        执行魔力兑换操作
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
            # --- 修改点2: 使用动态传入的 User-Agent 或默认值，避免硬编码旧版本被拦截 ---
            'User-Agent': self.ua,
            'Cookie': self.cookie
        }

        try:
            # 这些日志现在应该能正常显示在 MoviePilot 控制台了
            logger.info(f"执行站点 {self.site_name} 的魔力兑换")
            logger.info(f"兑换URL: {self.exchange_url}")
            logger.info(f"请求参数: option={option}")

            logger.info("正在发送兑换请求...")
            
            # 设置超时时间为 30 秒，避免无限等待
            response = requests.post(self.exchange_url, headers=headers, data=payload, timeout=30)
            
            # 检查响应状态码
            response.raise_for_status() 

            logger.info(f"请求完成，状态码: {response.status_code}")

            # 检查兑换结果 - 只要返回200状态码就认为是成功
            if response.status_code == 200:
                message = f"兑换成功！消耗 {bonus_cost} 魔力获得 {upload_amount} 上传量"
                logger.info(message)
                return True, message
            else:
                # 如果状态码不是200，则兑换失败
                message = f"兑换失败：HTTP状态码 {response.status_code}"
                logger.warning(message)
                logger.info(f"页面响应摘要: {response.text[:200]}...")
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