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

        # 检测当前系统是否开启了 DEBUG 级别日志
        is_debug = logger.isEnabledFor(10)  # 10 代表 logging.DEBUG

        try:
            logger.info(f"执行站点 {self.site_name} 的魔力兑换")
            logger.debug(f"兑换URL: {self.exchange_url}")
            logger.debug(f"请求参数: option={option}")
            logger.debug("正在发送兑换请求...")
            
            # 设置超时时间为 30 秒
            response = requests.post(self.exchange_url, headers=headers, data=payload, timeout=30)
            response.raise_for_status() 

            logger.debug(f"请求完成，状态码: {response.status_code}")

            # --- 核心修改：解析 NexusPHP 常见的错误提示 ---
            # 绝大多数 PT 站无论成功失败都返回 200，必须通过文本判断
            response_text = response.text
            
            error_keywords = ["错误", "失败", "不要信息", "魔力值不足", "不能", "Invalid", "Error", "failed"]
            has_error = any(keyword in response_text for keyword in error_keywords)

            if response.status_code == 200 and not has_error:
                message = f"兑换成功！消耗 {bonus_cost} 魔力获得 {upload_amount} 上传量"
                logger.info(message)
                return True, message
            else:
                # 提取页面中的核心提示（尝试捕获常见的 standard_error_table 文本）
                # 如果嫌正则麻烦，这里直接切片或输出相关语境
                summary = response_text[:500].replace('\n', ' ')
                
                base_msg = f"兑换可能失败（或触发站点提示）。"
                if has_error:
                    base_msg += "检测到页面包含错误关键词。"
                
                # 如果开启了 Debug 模式，把前500个字符作为 message 返回给前端/上层
                if is_debug:
                    message = f"{base_msg} 状态码: {response.status_code}。页面前500字响应: {summary}"
                else:
                    message = f"{base_msg} 状态码: {response.status_code}。请开启Debug日志查看完整响应。"
                
                logger.warning(message)
                return False, message

        except requests.exceptions.Timeout:
            error_msg = "兑换请求超时（30秒），请检查网络连接或站点是否正常"
            logger.error(error_msg)
            return False, error_msg
        except requests.exceptions.RequestException as e:
            # 网络请求错误时，如果是 Debug 模式，附带上完整的 Exception 信息
            error_msg = f"兑换请求网络错误: {str(e)}" if is_debug else "兑换请求网络错误，请检查网络"
            logger.error(f"兑换请求网络错误详情: {str(e)}")
            return False, error_msg
        except Exception as e:
            error_msg = f"兑换过程中发生未知错误: {str(e)}" if is_debug else "兑换过程中发生未知错误"
            logger.error(f"未知错误详情: {str(e)}")
            return False, error_msg
