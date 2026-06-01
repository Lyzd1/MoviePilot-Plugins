import requests
from typing import Tuple, Optional
import json
# --- 修改点1: 引用正确的系统日志模块 ---
from app.log import logger

class Exchange001:
    """
    001类兑换规则 - 适用于学校等站点
    """

    def __init__(self, site_name: str, site_url: str, cookie: str, ua: str = None, debug: bool = False):
        self.site_name = site_name
        self.site_url = site_url
        self.cookie = cookie
        self.ua = ua if ua else 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        self.exchange_url = f"{site_url}/mybonus.php?action=exchange"
        self.debug = debug

    def _log_debug(self, message):
        """安全的 debug 日志记录，避免 isEnabledFor 错误"""
        try:
            logger.debug(message)
        except AttributeError:
            # 如果 logger.debug 失败，尝试使用 info
            try:
                logger.info(f"[DEBUG] {message}")
            except:
                pass  # 如果都失败了，静默处理

    def _log_info(self, message):
        """安全的 info 日志记录"""
        try:
            logger.info(message)
        except:
            pass

    def _log_warning(self, message):
        """安全的 warning 日志记录"""
        try:
            logger.warning(message)
        except:
            pass

    def _log_error(self, message):
        """安全的 error 日志记录"""
        try:
            logger.error(message)
        except:
            pass

    def _format_debug_response(self, response, request_info):
        """格式化Debug信息"""
        # 简化为字符串拼接，避免复杂的格式化
        lines = []
        lines.append("="*50)
        lines.append("DEBUG: 请求详细信息")
        lines.append(f"URL: {request_info.get('url', '未知')}")
        lines.append(f"参数: {request_info.get('payload', {})}")
        lines.append(f"状态码: {response.status_code}")
        lines.append(f"耗时: {response.elapsed.total_seconds():.2f}秒")
        lines.append(f"响应大小: {len(response.text)} bytes")
        
        if len(response.text) <= 300:
            lines.append(f"响应内容: {response.text}")
        else:
            lines.append(f"响应前300字符: {response.text[:300]}")
        
        lines.append("="*50)
        return "\n".join(lines)

    def execute_exchange(self, option: str = None, upload_amount: str = None, bonus_cost: str = None, **kwargs) -> Tuple[bool, str]:
        """执行魔力兑换操作"""
        
        request_debug = kwargs.get('debug', self.debug)
        
        if not self.cookie:
            error_msg = "Cookie为空，无法执行兑换"
            if request_debug:
                error_msg = f"[DEBUG] {error_msg} | 站点: {self.site_name}"
            return False, error_msg

        payload = {
            'option': option,
            'submit': 'Exchange'
        }

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': self.ua,
            'Cookie': self.cookie
        }

        request_info = {
            "url": self.exchange_url,
            "payload": payload
        }

        try:
            # 记录开始信息
            if request_debug:
                self._log_info(f"[DEBUG] 开始兑换 - 站点: {self.site_name}")
                self._log_debug(f"URL: {self.exchange_url}")
                self._log_debug(f"参数: option={option}")
            else:
                self._log_info(f"执行站点 {self.site_name} 的魔力兑换")

            # 发送请求
            self._log_debug("正在发送兑换请求...")
            
            response = requests.post(self.exchange_url, headers=headers, data=payload, timeout=30)
            response.raise_for_status()

            # 记录响应信息
            if request_debug:
                self._log_debug(f"请求完成，耗时: {response.elapsed.total_seconds():.2f}秒")
                self._log_debug(f"状态码: {response.status_code}")

            # 处理结果
            if response.status_code == 200:
                if request_debug:
                    debug_info = self._format_debug_response(response, request_info)
                    self._log_info(debug_info)
                    
                    success_msg = (
                        f"[DEBUG] 兑换成功！\n"
                        f"消耗: {bonus_cost} 魔力\n"
                        f"获得: {upload_amount} 上传量\n"
                        f"站点: {self.site_name}\n"
                        f"响应大小: {len(response.text)} bytes"
                    )
                    self._log_info(success_msg)
                    return True, success_msg
                else:
                    message = f"兑换成功！消耗 {bonus_cost} 魔力获得 {upload_amount} 上传量"
                    self._log_info(message)
                    return True, message
            else:
                if request_debug:
                    debug_info = self._format_debug_response(response, request_info)
                    self._log_info(debug_info)
                    
                    error_msg = (
                        f"[DEBUG] 兑换失败\n"
                        f"状态码: {response.status_code}\n"
                        f"站点: {self.site_name}\n"
                        f"响应摘要: {response.text[:200]}"
                    )
                    self._log_warning(error_msg)
                    return False, error_msg
                else:
                    message = f"兑换失败：HTTP状态码 {response.status_code}"
                    self._log_warning(message)
                    return False, message

        except requests.exceptions.Timeout:
            error_msg = "兑换请求超时（30秒）"
            if request_debug:
                error_msg = (
                    f"[DEBUG] 请求超时\n"
                    f"站点: {self.site_name}\n"
                    f"URL: {self.exchange_url}\n"
                    f"参数: {payload}"
                )
            self._log_error(error_msg)
            return False, error_msg
            
        except requests.exceptions.ConnectionError as e:
            error_msg = f"连接错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"[DEBUG] 连接错误\n"
                    f"站点: {self.site_name}\n"
                    f"URL: {self.exchange_url}\n"
                    f"错误: {str(e)}"
                )
            self._log_error(error_msg)
            return False, error_msg
            
        except requests.exceptions.RequestException as e:
            error_msg = f"网络请求错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"[DEBUG] 请求异常\n"
                    f"站点: {self.site_name}\n"
                    f"URL: {self.exchange_url}\n"
                    f"类型: {type(e).__name__}\n"
                    f"错误: {str(e)}"
                )
            self._log_error(error_msg)
            return False, error_msg
            
        except Exception as e:
            error_msg = f"未知错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"[DEBUG] 未知异常\n"
                    f"站点: {self.site_name}\n"
                    f"类型: {type(e).__name__}\n"
                    f"错误: {str(e)}"
                )
            self._log_error(error_msg)
            return False, error_msg
