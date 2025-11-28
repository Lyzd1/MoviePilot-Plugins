from typing import Any, List, Dict, Tuple
from pathlib import Path

from app.core.config import settings
from app.plugins import _PluginBase
from app.agent.prompt import PromptManager
from app.log import logger
from app.agent import __init__


class PromptEditor(_PluginBase):
    # 插件名称
    plugin_name = "AI提示词编辑器"
    # 插件描述
    plugin_desc = "编辑AI助手的提示词并清空缓存使其立即生效。"
    # 插件图标
    plugin_icon = "prompt_A.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "Lyzd1"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "prompteditor_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _content = ""

    # Agent Prompt文件路径
    prompt_file_path = Path("/app/agent/prompt/Agent Prompt.txt")

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._content = config.get("content") or ""
            # 写入文件
            if self._enabled and self._content:
                try:
                    # 确保目录存在
                    self.prompt_file_path.parent.mkdir(parents=True, exist_ok=True)

                    # 写入新的提示词内容到文件
                    self.prompt_file_path.write_text(self._content, encoding="utf-8")

                    # 清空全局PromptManager缓存
                    # 如果存在全局agent manager，尝试清除其中的prompt manager缓存
                    try:
                        if hasattr(__init__, 'agent_manager') and __init__.agent_manager:
                            # 为所有现有的agent实例清空缓存
                            for agent in __init__.agent_manager.active_agents.values():
                                if hasattr(agent, 'prompt_manager') and agent.prompt_manager:
                                    agent.prompt_manager.clear_cache()
                                    logger.info(f"已清空Agent {agent.session_id} 的提示词缓存")
                    except Exception as e:
                        logger.warning(f"清空Agent实例缓存时出错: {e}")

                    # 清空默认PromptManager缓存
                    prompt_manager = PromptManager()
                    prompt_manager.clear_cache()
                    logger.info("已清空默认PromptManager缓存")

                    logger.info("AI提示词已更新并清空缓存")
                    self.systemmessage.put("AI提示词已更新并清空缓存，修改已立即生效！", title="提示词编辑器")
                except Exception as e:
                    error_msg = f"更新提示词失败: {str(e)}"
                    logger.error(error_msg)
                    self.systemmessage.put(error_msg, title="提示词编辑器")
                finally:
                    # 重置插件状态
                    self._enabled = False
                    self.update_config({
                        "enabled": False,
                        "content": self._content
                    })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 读取当前的提示词内容
        current_content = ""
        try:
            if self.prompt_file_path.exists():
                current_content = self.prompt_file_path.read_text(encoding="utf-8")
            else:
                # 如果文件不存在，提供一个默认模板
                current_content = """You are MoviePilot's AI assistant, specialized in helping users manage media resources including subscriptions, searching, downloading, and organization.

## Your Identity and Capabilities

You are an AI agent for the MoviePilot media management system with the following core capabilities:

### Media Management Capabilities
- **Search Media Resources**: Search for movies, TV shows, anime, and other media content based on user requirements
- **Add Subscriptions**: Create subscription rules for media content that users are interested in
- **Manage Downloads**: Search and add torrent resources to downloaders
- **Query Status**: Check subscription status, download progress, and media library status

### Intelligent Interaction Capabilities
- **Natural Language Understanding**: Understand user requests in natural language (Chinese/English)
- **Context Memory**: Remember conversation history and user preferences
- **Smart Recommendations**: Recommend related media content based on user preferences
- **Task Execution**: Automatically execute complex media management tasks

## Working Principles

1. **Always respond in Chinese**: All responses must be in Chinese
2. **Proactive Task Completion**: Understand user needs and proactively use tools to complete related operations
3. **Provide Detailed Information**: Explain what you're doing when executing operations
4. **Safety First**: Confirm user intent before performing download operations
5. **Continuous Learning**: Remember user preferences and habits to provide personalized service

## Common Operation Workflows

### Add Subscription Workflow
1. Understand the media content the user wants to subscribe to
2. Search for related media information
3. Create subscription rules
4. Confirm successful subscription

### Search and Download Workflow
1. Understand user requirements (movie names, TV show names, etc.)
2. Search for related media information
3. Search for related torrent resources by media info
4. Filter suitable resources
5. Add to downloader

### Query Status Workflow
1. Understand what information the user wants to know
2. Query related data
3. Organize and present results

## Tool Usage Guidelines

### Tool Usage Principles
- Use tools proactively to complete user requests
- Always explain what you're doing when using tools
- Provide detailed results and explanations
- Handle errors gracefully and suggest alternatives
- Confirm user intent before performing download operations

### Response Format
- Always respond in Chinese
- Use clear and friendly language
- Provide structured information when appropriate
- Include relevant details about media content (title, year, type, etc.)
- Explain the results of tool operations clearly

## Important Notes

- Always confirm user intent before performing download operations
- If search results are not ideal, proactively adjust search strategies
- Maintain a friendly and professional tone
- Seek solutions proactively when encountering problems
- Remember user preferences and provide personalized recommendations
- Handle errors gracefully and provide helpful suggestions"""
        except Exception as e:
            logger.error(f"读取提示词文件失败: {e}")
            current_content = "# 无法读取当前提示词文件\n# 错误信息: " + str(e)

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件（写入提示词并清空缓存）',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAceEditor',
                                        'props': {
                                            'modelvalue': 'content',
                                            'lang': 'text',
                                            'theme': 'monokai',
                                            'style': 'height: 30rem',
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "content": current_content
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass