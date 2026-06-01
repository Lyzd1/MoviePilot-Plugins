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
        # 如果没有传入UA，使用一个默认的较新的UA
        self.ua = ua if ua else 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        self.exchange_url = f"{site_url}/mybonus.php?action=exchange"
        self.debug = debug  # Debug模式开关

    def _format_debug_response(self, response, request_info: dict) -> str:
        """
        格式化Debug信息，返回详细的请求和响应数据
        """
        debug_info = {
            "请求信息": {
                "URL": request_info.get("url", "未知"),
                "方法": "POST",
                "请求头": {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": self.ua[:50] + "..." if len(self.ua) > 50 else self.ua,
                    "Cookie": self.cookie[:30] + "..." if len(self.cookie) > 30 else self.cookie
                },
                "请求参数": request_info.get("payload", {}),
                "超时设置": "30秒"
            },
            "响应信息": {
                "状态码": response.status_code,
                "响应头": dict(response.headers),
                "响应内容长度": len(response.text),
                "响应编码": response.encoding if hasattr(response, 'encoding') else "未知",
                "耗时": f"{response.elapsed.total_seconds():.2f}秒",
                "重定向历史": [str(r) for r in response.history] if response.history else "无重定向"
            }
        }
        
        # 如果响应内容不太长，包含前500字符
        if len(response.text) <= 500:
            debug_info["响应信息"]["完整响应内容"] = response.text
        else:
            debug_info["响应信息"]["响应内容前500字符"] = response.text[:500] + "..."
            debug_info["响应信息"]["响应内容后200字符"] = "..." + response.text[-200:]
        
        return json.dumps(debug_info, indent=2, ensure_ascii=False)

    def execute_exchange(self, option: str = None, upload_amount: str = None, bonus_cost: str = None, **kwargs) -> Tuple[bool, str]:
        """
        执行魔力兑换操作
        
        Args:
            option: 兑换选项
            upload_amount: 上传量
            bonus_cost: 消耗的魔力值
            **kwargs: 其他参数，可包含 'debug' 参数来控制本次请求的Debug模式
        
        Returns:
            Tuple[bool, str]: (是否成功, 详细信息)
        """
        # 允许通过kwargs临时覆盖debug设置
        request_debug = kwargs.get('debug', self.debug)
        
        if not self.cookie:
            error_msg = "Cookie为空，无法执行兑换"
            if request_debug:
                error_msg = f"[DEBUG] {error_msg} | 站点: {self.site_name} | URL: {self.exchange_url}"
            return False, error_msg

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

        # 记录请求信息用于Debug
        request_info = {
            "url": self.exchange_url,
            "payload": payload
        }

        try:
            if request_debug:
                logger.info(f"[DEBUG模式] 开始执行站点 {self.site_name} 的魔力兑换")
                logger.debug(f"[DEBUG] 兑换URL: {self.exchange_url}")
                logger.debug(f"[DEBUG] 请求参数: option={option}")
                logger.debug(f"[DEBUG] 完整Payload: {payload}")
                logger.debug(f"[DEBUG] 请求头: User-Agent={self.ua[:80]}...")
                logger.debug(f"[DEBUG] Cookie前30字符: {self.cookie[:30]}...")
            else:
                logger.info(f"执行站点 {self.site_name} 的魔力兑换")
                logger.debug(f"兑换URL: {self.exchange_url}")
                logger.debug(f"请求参数: option={option}")

            logger.debug("正在发送兑换请求...")
            
            # 设置超时时间为 30 秒，避免无限等待
            response = requests.post(self.exchange_url, headers=headers, data=payload, timeout=30)
            
            # 检查响应状态码
            response.raise_for_status()

            if request_debug:
                logger.debug(f"[DEBUG] 请求完成，耗时: {response.elapsed.total_seconds():.2f}秒")
                logger.debug(f"[DEBUG] 响应状态码: {response.status_code}")
                logger.debug(f"[DEBUG] 响应头: {dict(response.headers)}")
                if response.history:
                    logger.debug(f"[DEBUG] 发生重定向: {[str(r) for r in response.history]}")

            # 检查兑换结果 - 只要返回200状态码就认为是成功
            if response.status_code == 200:
                if request_debug:
                    # 尝试从响应中提取更多信息
                    response_preview = response.text[:300].strip()
                    debug_message = (
                        f"[DEBUG] ✅ 兑换成功！\n"
                        f"├─ 消耗魔力: {bonus_cost}\n"
                        f"├─ 获得上传量: {upload_amount}\n"
                        f"├─ 站点: {self.site_name}\n"
                        f"├─ 响应大小: {len(response.text)} bytes\n"
                        f"└─ 响应预览: {response_preview}"
                    )
                    logger.info(debug_message)
                    # 添加详细的Debug信息
                    logger.debug(f"\n{self._format_debug_response(response, request_info)}")
                    return True, debug_message
                else:
                    message = f"兑换成功！消耗 {bonus_cost} 魔力获得 {upload_amount} 上传量"
                    logger.info(message)
                    return True, message
            else:
                if request_debug:
                    error_message = (
                        f"[DEBUG] ❌ 兑换失败\n"
                        f"├─ HTTP状态码: {response.status_code}\n"
                        f"├─ 站点: {self.site_name}\n"
                        f"├─ 响应大小: {len(response.text)} bytes\n"
                        f"└─ 响应摘要: {response.text[:200]}"
                    )
                    logger.warning(error_message)
                    # 添加详细的Debug信息
                    logger.debug(f"\n{self._format_debug_response(response, request_info)}")
                    return False, error_message
                else:
                    message = f"兑换失败：HTTP状态码 {response.status_code}"
                    logger.warning(message)
                    logger.info(f"页面响应摘要: {response.text[:200]}...")
                    return False, message

        except requests.exceptions.Timeout:
            error_msg = "兑换请求超时（30秒），请检查网络连接或站点是否正常"
            if request_debug:
                error_msg = (
                    f"[DEBUG] ⏱ {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"├─ 超时时间: 30秒\n"
                    f"└─ 请求参数: {payload}"
                )
            logger.error(error_msg)
            return False, error_msg
            
        except requests.exceptions.ConnectionError as e:
            error_msg = f"连接错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"[DEBUG] 🔌 {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"└─ 可能原因: DNS解析失败、网络不可达、目标服务器拒绝连接"
                )
            logger.error(error_msg)
            return False, error_msg
            
        except requests.exceptions.TooManyRedirects as e:
            error_msg = f"重定向次数过多: {str(e)}"
            if request_debug:
                error_msg = (
                    f"[DEBUG] 🔄 {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"└─ 可能原因: Cookie失效导致反复重定向到登录页"
                )
            logger.error(error_msg)
            return False, error_msg
            
        except requests.exceptions.RequestException as e:
            error_msg = f"兑换请求网络错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"[DEBUG] 🌐 {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"├─ 异常类型: {type(e).__name__}\n"
                    f"└─ 请求详情: {request_info}"
                )
            logger.error(error_msg)
            return False, error_msg
            
        except Exception as e:
            error_msg = f"兑换过程中发生未知错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"[DEBUG] 💥 {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"├─ 异常类型: {type(e).__name__}\n"
                    f"├─ 异常详情: {str(e)}\n"
                    f"└─ 请求参数: {payload}"
                )
            logger.error(error_msg)
            return False, error_msg
