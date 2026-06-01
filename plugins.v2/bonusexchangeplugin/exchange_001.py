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

    def _format_debug_response(self, response, request_info: dict, is_success: bool) -> str:
        """
        格式化Debug信息，返回详细的请求和响应数据
        """
        status_emoji = "✅" if is_success else "❌"
        status_text = "成功" if is_success else "失败"
        
        debug_info = f"{status_emoji} 兑换{status_text} - 详细调试信息\n"
        debug_info += "=" * 50 + "\n\n"
        
        # 请求信息
        debug_info += "📤 请求信息:\n"
        debug_info += f"  ├─ URL: {request_info.get('url', '未知')}\n"
        debug_info += f"  ├─ 方法: POST\n"
        debug_info += f"  ├─ Content-Type: application/x-www-form-urlencoded\n"
        debug_info += f"  ├─ User-Agent: {self.ua[:80]}...\n" if len(self.ua) > 80 else f"  ├─ User-Agent: {self.ua}\n"
        debug_info += f"  ├─ Cookie: {self.cookie[:30]}...\n" if len(self.cookie) > 30 else f"  ├─ Cookie: {self.cookie}\n"
        debug_info += f"  ├─ 请求参数: {json.dumps(request_info.get('payload', {}), ensure_ascii=False)}\n"
        debug_info += f"  └─ 超时设置: 30秒\n\n"
        
        # 响应信息
        debug_info += "📥 响应信息:\n"
        debug_info += f"  ├─ 状态码: {response.status_code}\n"
        debug_info += f"  ├─ 状态描述: {response.reason}\n"
        debug_info += f"  ├─ 响应耗时: {response.elapsed.total_seconds():.3f}秒\n"
        debug_info += f"  ├─ 响应大小: {len(response.text)} 字节\n"
        debug_info += f"  ├─ 响应编码: {response.encoding}\n"
        
        # 重定向信息
        if response.history:
            debug_info += f"  ├─ 重定向次数: {len(response.history)}\n"
            for i, redirect in enumerate(response.history, 1):
                debug_info += f"  │  └─ 第{i}次重定向: {redirect.status_code} -> {redirect.url}\n"
        else:
            debug_info += f"  ├─ 重定向: 无\n"
        
        # 重要响应头
        important_headers = ['Content-Type', 'Content-Length', 'Set-Cookie', 'Location', 'Server', 'Date']
        debug_info += f"  ├─ 关键响应头:\n"
        for header in important_headers:
            if header in response.headers:
                value = response.headers[header]
                if header == 'Set-Cookie':
                    value = value[:50] + "..." if len(value) > 50 else value
                debug_info += f"  │  ├─ {header}: {value}\n"
        
        # 完整响应头（可选）
        if self.debug:
            debug_info += f"  ├─ 完整响应头:\n"
            for key, value in response.headers.items():
                debug_info += f"  │  ├─ {key}: {value}\n"
        
        # 响应内容
        debug_info += f"  └─ 响应内容:\n"
        if len(response.text) <= 1000:
            # 内容较短，完整显示
            debug_info += f"     ├─ 长度: {len(response.text)} 字符\n"
            debug_info += f"     └─ 完整内容:\n{response.text}\n"
        else:
            # 内容较长，显示前后部分
            debug_info += f"     ├─ 总长度: {len(response.text)} 字符\n"
            debug_info += f"     ├─ 前500字符:\n{response.text[:500]}\n"
            debug_info += f"     │  ... (省略 {len(response.text) - 1000} 字符) ...\n"
            debug_info += f"     └─ 后500字符:\n{response.text[-500:]}\n"
        
        debug_info += "=" * 50
        return debug_info

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
                error_msg = f"❌ [DEBUG] {error_msg}\n站点: {self.site_name}\nURL: {self.exchange_url}"
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
                logger.info(f"🔍 [DEBUG模式] 开始执行站点 {self.site_name} 的魔力兑换")
                logger.debug(f"完整请求URL: {self.exchange_url}")
                logger.debug(f"请求参数: option={option}")
                logger.debug(f"完整Payload: {json.dumps(payload, ensure_ascii=False)}")
            else:
                logger.info(f"执行站点 {self.site_name} 的魔力兑换")
                logger.debug(f"兑换URL: {self.exchange_url}")
                logger.debug(f"请求参数: option={option}")

            logger.debug("正在发送兑换请求...")
            
            # 设置超时时间为 30 秒，避免无限等待
            response = requests.post(self.exchange_url, headers=headers, data=payload, timeout=30)
            
            # 检查响应状态码
            response.raise_for_status()

            logger.debug(f"请求完成，状态码: {response.status_code}")

            # 检查兑换结果 - 只要返回200状态码就认为是成功
            if response.status_code == 200:
                # 成功时的消息构建
                if request_debug:
                    success_message = (
                        f"✅ 兑换成功！\n"
                        f"├─ 消耗魔力: {bonus_cost}\n"
                        f"├─ 获得上传量: {upload_amount}\n"
                        f"├─ 站点: {self.site_name}\n"
                        f"├─ 响应时间: {response.elapsed.total_seconds():.3f}秒\n"
                        f"└─ 响应大小: {len(response.text)} 字节"
                    )
                    logger.info(success_message)
                    # 输出详细的调试信息（这就是您要的response信息）
                    debug_detail = self._format_debug_response(response, request_info, is_success=True)
                    logger.info(debug_detail)
                    return True, success_message
                else:
                    # 非Debug模式也记录基本的响应信息
                    message = f"兑换成功！消耗 {bonus_cost} 魔力获得 {upload_amount} 上传量"
                    logger.info(message)
                    # 即使在非Debug模式，也记录一些基本的响应信息
                    logger.debug(f"响应状态: {response.status_code}, 大小: {len(response.text)}字节, 耗时: {response.elapsed.total_seconds():.2f}秒")
                    return True, message
            else:
                if request_debug:
                    error_message = (
                        f"❌ 兑换失败\n"
                        f"├─ HTTP状态码: {response.status_code}\n"
                        f"├─ 状态描述: {response.reason}\n"
                        f"├─ 站点: {self.site_name}\n"
                        f"├─ 响应大小: {len(response.text)} 字节\n"
                        f"└─ 响应摘要: {response.text[:200]}"
                    )
                    logger.warning(error_message)
                    # 输出详细的调试信息
                    debug_detail = self._format_debug_response(response, request_info, is_success=False)
                    logger.info(debug_detail)
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
                    f"⏱ [DEBUG] {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"├─ 超时时间: 30秒\n"
                    f"└─ 请求参数: {json.dumps(payload, ensure_ascii=False)}"
                )
            logger.error(error_msg)
            return False, error_msg
            
        except requests.exceptions.ConnectionError as e:
            error_msg = f"连接错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"🔌 [DEBUG] {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"├─ 异常类型: {type(e).__name__}\n"
                    f"└─ 可能原因: DNS解析失败、网络不可达、目标服务器拒绝连接"
                )
            logger.error(error_msg)
            return False, error_msg
            
        except requests.exceptions.TooManyRedirects as e:
            error_msg = f"重定向次数过多: {str(e)}"
            if request_debug:
                error_msg = (
                    f"🔄 [DEBUG] {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"├─ 异常类型: {type(e).__name__}\n"
                    f"└─ 可能原因: Cookie失效导致反复重定向到登录页"
                )
            logger.error(error_msg)
            return False, error_msg
            
        except requests.exceptions.RequestException as e:
            error_msg = f"兑换请求网络错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"🌐 [DEBUG] {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"├─ 异常类型: {type(e).__name__}\n"
                    f"└─ 请求详情: {json.dumps(request_info, ensure_ascii=False)}"
                )
            logger.error(error_msg)
            return False, error_msg
            
        except Exception as e:
            error_msg = f"兑换过程中发生未知错误: {str(e)}"
            if request_debug:
                error_msg = (
                    f"💥 [DEBUG] {error_msg}\n"
                    f"├─ 站点: {self.site_name}\n"
                    f"├─ URL: {self.exchange_url}\n"
                    f"├─ 异常类型: {type(e).__name__}\n"
                    f"├─ 异常详情: {str(e)}\n"
                    f"└─ 请求参数: {json.dumps(payload, ensure_ascii=False)}"
                )
            logger.error(error_msg)
            return False, error_msg
