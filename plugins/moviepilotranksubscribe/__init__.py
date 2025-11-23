import datetime
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Tuple, List, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.chain.recommend import RecommendChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType


class MoviePilotRankSubscribe(_PluginBase):
    # 插件名称
    plugin_name = "MoviePilot榜单订阅"
    # 插件描述
    plugin_desc = "定期获取MoviePilot内置榜单，根据评分和过滤条件自动订阅内容。"
    # 插件图标
    plugin_icon = "movie.jpg"
    # 插件版本
    plugin_version = "1.5"
    # 插件作者
    plugin_author = "Lyzd1"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "moviepilotrank_"
    # 加载顺序
    plugin_order = 6
    # 可使用的用户级别
    auth_level = 2

    # 退出事件
    _event = Event()
    # 私有属性
    _scheduler = None

    # 支持的榜单源
    _rank_sources = {
        'tmdb_trending': 'TMDB流行趋势',
        'tmdb_movies': 'TMDB热门电影',
        'tmdb_tvs': 'TMDB热门电视剧',
        'douban_hot': '豆瓣热门',
        'douban_movie_hot': '豆瓣热门电影',
        'douban_tv_hot': '豆瓣热门电视剧',
        'douban_movie_showing': '豆瓣正在热映',
        'douban_movies': '豆瓣最新电影',
        'douban_tvs': '豆瓣最新电视剧',
        'douban_movie_top250': '豆瓣电影TOP250',
        'douban_tv_weekly_chinese': '豆瓣国产剧集榜',
        'douban_tv_weekly_global': '豆瓣全球剧集榜',
        'douban_tv_animation': '豆瓣热门动漫',
        'bangumi_calendar': '番组计划'
    }

    _enabled = False
    _cron = ""
    _onlyonce = False
    _sources = []
    _min_vote = 0
    _media_types = []
    _max_items = 20
    _clear_history = False
    _clear_flag = False

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._sources = config.get("sources") or []
            self._min_vote = float(config.get("min_vote")) if config.get("min_vote") else 0
            self._media_types = config.get("media_types") or []
            self._max_items = int(config.get("max_items")) if config.get("max_items") else 20
            self._clear_history = config.get("clear_history")

        logger.info(f"MoviePilot榜单订阅插件初始化: enabled={self._enabled}, sources={self._sources}")

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("MoviePilot榜单订阅服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__sync_refresh_rankings, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )

                if self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce or self._clear_history:
                # 关闭一次性开关
                self._onlyonce = False
                # 记录缓存清理标志
                self._clear_flag = self._clear_history
                # 关闭清理缓存
                self._clear_history = False
                # 保存配置
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除MoviePilot榜单订阅历史记录"
            },
            {
                "path": "/get_sources",
                "endpoint": self.get_sources,
                "methods": ["GET"],
                "summary": "获取支持的榜单源列表"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self._cron:
            return [
                {
                    "id": "MoviePilotRankSubscribe",
                    "name": "MoviePilot榜单订阅服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__sync_refresh_rankings,
                    "kwargs": {}
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "MoviePilotRankSubscribe",
                    "name": "MoviePilot榜单订阅服务",
                    "trigger": CronTrigger.from_crontab("0 8 * * *"),
                    "func": self.__sync_refresh_rankings,
                    "kwargs": {}
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear_history',
                                            'label': '清理历史记录',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_items',
                                            'label': '最大数量',
                                            'placeholder': '每个榜单最多处理条目数（默认20）',
                                            'type': 'number'
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'sources',
                                            'label': '榜单源',
                                            'items': [
                                                {'title': title, 'value': key}
                                                for key, title in self._rank_sources.items()
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'media_types',
                                            'label': '媒体类型',
                                            'items': [
                                                {'title': '电影', 'value': 'movie'},
                                                {'title': '电视剧', 'value': 'tv'},
                                                {'title': '全部', 'value': 'all'}
                                            ]
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'min_vote',
                                            'label': '最低评分',
                                            'placeholder': '只订阅评分大于等于该值的内容（0-10）',
                                            'type': 'number',
                                            'min': 0,
                                            'max': 10,
                                            'step': 0.1
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
            "cron": "",
            "onlyonce": False,
            "sources": [],
            "media_types": [],
            "min_vote": 0,
            "max_items": 20,
            "clear_history": False
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询历史记录
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)

        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            vote = history.get("vote")
            source = history.get("source")
            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {
                                'innerClass': 'absolute top-0 right-0',
                            },
                            'events': {
                                'click': {
                                    'api': 'plugin/MoviePilotRankSubscribe/delete_history',
                                    'method': 'get',
                                    'params': {
                                        'key': f"moviepilotrank: {title}",
                                        'apikey': settings.API_TOKEN
                                    }
                                }
                            },
                        },
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': poster,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VCardTitle',
                                            'props': {
                                                'class': 'ps-1 pe-5 break-words whitespace-break-spaces'
                                            },
                                            'text': title
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{mtype}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'评分：{vote}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'来源：{source}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'时间：{time_str}'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    def get_sources(self, apikey: str):
        """
        获取支持的榜单源列表
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        return schemas.Response(
            success=True,
            data=[
                {"value": key, "title": title}
                for key, title in self._rank_sources.items()
            ]
        )

    def delete_history(self, key: str, apikey: str):
        """
        删除历史记录
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        # 历史记录
        historys = self.get_data('history')
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")

        # 删除指定记录
        historys = [h for h in historys if h.get("unique") != key]
        self.save_data('history', historys)
        return schemas.Response(success=True, message="删除成功")

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "sources": self._sources,
            "media_types": self._media_types,
            "min_vote": self._min_vote,
            "max_items": self._max_items,
            "clear_history": self._clear_history
        })

    def __sync_refresh_rankings(self):
        """
        同步包装函数，用于APScheduler调用异步函数
        """
        logger.info("开始执行榜单刷新任务")
        try:
            # 直接在新线程中运行，避免事件循环冲突
            thread = threading.Thread(target=self.__run_in_thread)
            thread.daemon = True
            thread.start()
            thread.join(timeout=300)  # 5分钟超时
            if thread.is_alive():
                logger.warning("榜单刷新任务超时")
        except Exception as e:
            logger.error(f"榜单刷新任务执行失败：{str(e)}")
            import traceback
            logger.error(f"详细错误信息：{traceback.format_exc()}")

    def __run_in_thread(self):
        """
        在新线程中运行异步任务
        """
        try:
            # 创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 运行异步任务
            loop.run_until_complete(self.__refresh_rankings())
        except Exception as e:
            logger.error(f"线程中执行失败：{str(e)}")
        finally:
            try:
                loop.close()
            except:
                pass

    async def __refresh_rankings(self):
        """
        刷新榜单并添加订阅
        """
        logger.info("开始刷新MoviePilot榜单...")

        logger.info(f"当前配置：sources={self._sources}, min_vote={self._min_vote}, media_types={self._media_types}, max_items={self._max_items}")

        if not self._sources:
            logger.info("未选择榜单源，使用默认榜单源：tmdb_trending, douban_movie_hot")
            self._sources = ['tmdb_trending', 'douban_movie_hot']

        # 读取历史记录
        if self._clear_flag:
            history = []
        else:
            history: List[dict] = self.get_data('history') or []

        # 初始化链
        recommend_chain = RecommendChain()
        subscribe_chain = SubscribeChain()
        download_chain = DownloadChain()

        # 处理每个榜单源
        for source in self._sources:
            if self._event.is_set():
                logger.info("榜单订阅服务停止")
                return

            logger.info(f"处理榜单源：{self._rank_sources.get(source, source)}")

            try:
                # 获取榜单数据
                results = self.__get_ranking_data(recommend_chain, source)
                if not results:
                    logger.warning(f"榜单源 {source} 未获取到数据")
                    continue

                logger.info(f"榜单源 {source} 获取到 {len(results)} 条数据")

                # 处理每条记录
                for i, item in enumerate(results):
                    if self._event.is_set():
                        logger.info("榜单订阅服务停止")
                        return

                    # 验证item数据
                    if not item:
                        logger.warning(f"第 {i+1} 条记录为空，跳过")
                        continue

                    logger.debug(f"处理第 {i+1} 条记录：{item}")

                    # 检查必要字段
                    title = item.get('title')
                    if not title:
                        logger.warning(f"第 {i+1} 条记录缺少标题，跳过：{item}")
                        continue

                    # 检查媒体类型过滤
                    if self._media_types and 'all' not in self._media_types:
                        media_type = item.get('type')
                        if media_type == '电影' and 'movie' not in self._media_types:
                            logger.debug(f"{title} 是电影，但当前过滤只包含电视剧")
                            continue
                        elif media_type == '电视剧' and 'tv' not in self._media_types:
                            logger.debug(f"{title} 是电视剧，但当前过滤只包含电影")
                            continue

                    # 检查评分过滤
                    try:
                        vote_average = float(item.get('vote_average', 0))
                    except (ValueError, TypeError):
                        vote_average = 0

                    if vote_average < self._min_vote:
                        logger.debug(f"{title} 评分 {vote_average} 低于最低要求 {self._min_vote}")
                        continue

                    # 检查是否已处理过
                    unique_flag = f"moviepilotrank: {item.get('title')}"
                    if unique_flag in [h.get("unique") for h in history]:
                        logger.debug(f"{item.get('title')} 已处理过")
                        continue

                    # 获取详细信息并添加订阅
                    success = self.__add_subscription(
                        item, source, subscribe_chain, download_chain, history, unique_flag
                    )

                    if success:
                        logger.info(f"成功添加订阅：{item.get('title')}")

            except Exception as e:
                logger.error(f"处理榜单源 {source} 失败：{str(e)}")
                continue

        # 保存历史记录
        self.save_data('history', history)
        # 缓存只清理一次
        self._clear_flag = False
        logger.info("所有榜单刷新完成")


    def __get_ranking_data(self, recommend_chain: RecommendChain, source: str) -> List[dict]:
        """
        获取指定榜单源的数据（同步版本，避免Redis异步问题）
        """
        logger.info(f"开始获取榜单源 {source} 的数据...")
        try:
            # 使用同步方法获取数据，避免异步Redis调用问题
            if source == 'tmdb_trending':
                logger.debug("调用 recommend_chain.tmdb_trending (同步)")
                results = recommend_chain.tmdb_trending(page=1)
                logger.debug(f"tmdb_trending 返回数据类型：{type(results)}, 数量：{len(results) if results else 0}")
            elif source == 'tmdb_movies':
                logger.debug("调用 recommend_chain.tmdb_movies (同步)")
                results = recommend_chain.tmdb_movies(page=1)
                logger.debug(f"tmdb_movies 返回数据类型：{type(results)}, 数量：{len(results) if results else 0}")
            elif source == 'tmdb_tvs':
                logger.debug("调用 recommend_chain.tmdb_tvs (同步)")
                results = recommend_chain.tmdb_tvs(page=1)
                logger.debug(f"tmdb_tvs 返回数据类型：{type(results)}, 数量：{len(results) if results else 0}")
            elif source == 'douban_hot':
                # 合并电影和电视剧
                logger.debug("调用 recommend_chain.douban_movie_hot 和 douban_tv_hot (同步)")
                movie_results = recommend_chain.douban_movie_hot(page=1, count=self._max_items)
                tv_results = recommend_chain.douban_tv_hot(page=1, count=self._max_items)
                results = movie_results + tv_results
                logger.debug(f"douban_hot 返回数据：电影{len(movie_results) if movie_results else 0}条，电视剧{len(tv_results) if tv_results else 0}条")
            elif source == 'douban_movie_hot':
                logger.debug("调用 recommend_chain.douban_movie_hot (同步)")
                results = recommend_chain.douban_movie_hot(page=1, count=self._max_items)
                logger.debug(f"douban_movie_hot 返回数据类型：{type(results)}, 数量：{len(results) if results else 0}")
            elif source == 'douban_tv_hot':
                logger.debug("调用 recommend_chain.douban_tv_hot (同步)")
                results = recommend_chain.douban_tv_hot(page=1, count=self._max_items)
                logger.debug(f"douban_tv_hot 返回数据类型：{type(results)}, 数量：{len(results) if results else 0}")
            elif source == 'douban_movie_showing':
                logger.debug("调用 recommend_chain.douban_movie_showing (同步)")
                results = recommend_chain.douban_movie_showing(page=1, count=self._max_items)
            elif source == 'douban_movies':
                logger.debug("调用 recommend_chain.douban_movies (同步)")
                results = recommend_chain.douban_movies(page=1, count=self._max_items)
            elif source == 'douban_tvs':
                logger.debug("调用 recommend_chain.douban_tvs (同步)")
                results = recommend_chain.douban_tvs(page=1, count=self._max_items)
            elif source == 'douban_movie_top250':
                logger.debug("调用 recommend_chain.douban_movie_top250 (同步)")
                results = recommend_chain.douban_movie_top250(page=1, count=self._max_items)
            elif source == 'douban_tv_weekly_chinese':
                logger.debug("调用 recommend_chain.douban_tv_weekly_chinese (同步)")
                results = recommend_chain.douban_tv_weekly_chinese(page=1, count=self._max_items)
            elif source == 'douban_tv_weekly_global':
                logger.debug("调用 recommend_chain.douban_tv_weekly_global (同步)")
                results = recommend_chain.douban_tv_weekly_global(page=1, count=self._max_items)
            elif source == 'douban_tv_animation':
                logger.debug("调用 recommend_chain.douban_tv_animation (同步)")
                results = recommend_chain.douban_tv_animation(page=1, count=self._max_items)
            elif source == 'bangumi_calendar':
                logger.debug("调用 recommend_chain.bangumi_calendar (同步)")
                results = recommend_chain.bangumi_calendar(page=1, count=self._max_items)
            else:
                logger.warning(f"不支持的榜单源：{source}")
                return []

            # 限制数量并转换为字典
            if results:
                results = results[:self._max_items]
                logger.debug(f"原始数据前3条：{results[:3]}")

                # 转换为字典格式
                converted_results = []
                for i, item in enumerate(results):
                    try:
                        if hasattr(item, 'to_dict'):
                            converted = item.to_dict()
                        elif isinstance(item, dict):
                            converted = item.copy()
                        elif hasattr(item, '__dict__'):
                            converted = item.__dict__.copy()
                        else:
                            # 尝试转换为字典
                            converted = dict(item) if hasattr(item, '__iter__') else {}

                        # 确保必要字段存在
                        if not converted.get('title'):
                            logger.debug(f"第 {i+1} 条数据转换后缺少标题：{converted}")
                            continue

                        converted_results.append(converted)
                    except Exception as e:
                        logger.warning(f"转换第 {i+1} 条数据失败：{str(e)}")
                        continue

                results = converted_results
                logger.info(f"成功转换 {len(results)} 条数据")

            return results

        except Exception as e:
            logger.error(f"获取榜单数据失败：{str(e)}")
            return []

    def __add_subscription(self, item: dict, source: str, subscribe_chain: SubscribeChain,
                          download_chain: DownloadChain, history: List[dict], unique_flag: str) -> bool:
        """
        添加订阅
        """
        try:
            # 获取基本信息
            title = item.get('title')
            year = str(item.get('year', ''))
            media_type_str = item.get('type', '')

            logger.debug(f"准备添加订阅：title={title}, year={year}, type={media_type_str}")
            logger.debug(f"item数据详情：{item}")

            # 确保media_type_str是字符串
            if media_type_str is None:
                media_type_str = ''
            else:
                media_type_str = str(media_type_str)

            # 转换媒体类型
            media_type = None
            if media_type_str == '电影':
                media_type = MediaType.MOVIE
                logger.debug("识别为电影类型")
            elif media_type_str == '电视剧':
                media_type = MediaType.TV
                logger.debug("识别为电视剧类型")
            else:
                logger.warning(f"未知的媒体类型：{media_type_str}（类型：{type(media_type_str)}），跳过")
                return False

            logger.debug(f"转换后的media_type：{media_type}")

            # 先识别媒体信息
            from app.core.metainfo import MetaInfo
            from app.core.context import MediaInfo
            from app.chain.media import MediaChain

            meta = MetaInfo(title)
            meta.year = year
            if media_type:
                meta.type = media_type

            # 尝试识别媒体信息
            mediainfo = None
            try:
                media_chain = MediaChain()
                douban_id = item.get('douban_id')
                tmdb_id = item.get('tmdb_id')
                imdb_id = item.get('imdb_id')

                logger.debug(f"识别参数：title={title}, year={year}, douban_id={douban_id}, tmdb_id={tmdb_id}, imdb_id={imdb_id}")

                # 尝试多种识别方式
                if tmdb_id:
                    logger.debug(f"尝试使用TMDB ID识别：{tmdb_id}")
                    mediainfo = media_chain.recognize_media(meta=meta, tmdbid=tmdb_id)
                elif douban_id:
                    logger.debug(f"尝试使用豆瓣ID识别：{douban_id}")
                    mediainfo = media_chain.recognize_media(meta=meta, doubanid=douban_id)
                elif imdb_id:
                    logger.debug(f"尝试使用IMDB ID识别：{imdb_id}")
                    mediainfo = media_chain.recognize_media(meta=meta, imdbid=imdb_id)
                else:
                    logger.debug(f"尝试使用标题和年份识别：{title} ({year})")
                    mediainfo = media_chain.recognize_media(meta=meta)

                if mediainfo:
                    logger.info(f"成功识别媒体信息：{mediainfo.title} (TMDB: {mediainfo.tmdb_id})")
                else:
                    logger.warning(f"未能识别媒体信息：{title} (doubanid: {douban_id}, tmdbid: {tmdb_id})")
                    # 不直接返回False，而是继续尝试添加订阅，让MoviePilot自己处理

            except Exception as e:
                logger.error(f"识别媒体信息时出错：{str(e)}")
                # 继续执行，使用基本信息

            # 检查媒体库中是否已存在
            try:
                if mediainfo:
                    exist_flag, _ = download_chain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                    if exist_flag:
                        logger.info(f"{title} 媒体库中已存在")
                        return False
                else:
                    logger.debug(f"未识别到媒体信息，跳过媒体库存在性检查")
            except Exception as e:
                logger.error(f"检查媒体库是否存在时出错：{str(e)}")
                # 继续执行，让订阅逻辑处理

            # 检查是否已经添加订阅
            try:
                if mediainfo:
                    if subscribe_chain.exists(mediainfo=mediainfo, meta=meta):
                        logger.info(f"{title} 订阅已存在")
                        return False
                else:
                    # 如果没有识别到媒体信息，只使用meta检查
                    if subscribe_chain.exists(mediainfo=None, meta=meta):
                        logger.info(f"{title} 订阅已存在（基于标题检查）")
                        return False
            except Exception as e:
                logger.error(f"检查订阅是否存在时出错：{str(e)}")
                # 继续执行，让订阅逻辑处理

            # 获取TMDB ID（优先使用识别到的）
            tmdbid_int = None
            if mediainfo and mediainfo.tmdb_id:
                tmdbid_int = mediainfo.tmdb_id
            else:
                tmdb_id = item.get('tmdb_id')
                if tmdb_id:
                    try:
                        tmdbid_int = int(tmdb_id)
                    except (ValueError, TypeError):
                        logger.warning(f"无效的TMDB ID：{tmdb_id}")

            # 添加订阅
            logger.debug(f"准备调用subscribe_chain.add，参数：mtype={media_type}, title={title}, year={year}, tmdbid={tmdbid_int}")

            try:
                sid, message = subscribe_chain.add(
                    mtype=media_type,
                    title=title,
                    year=year,
                    tmdbid=tmdbid_int,
                    username="MoviePilot榜单订阅"
                )
                logger.debug(f"订阅添加结果：sid={sid}, message={message}")
            except Exception as e:
                logger.error(f"添加订阅时出错：{str(e)}")
                import traceback
                logger.error(f"详细错误：{traceback.format_exc()}")
                return False

            if sid:
                # 存储历史记录
                history.append({
                    "title": title,
                    "type": media_type_str,
                    "year": year,
                    "vote": item.get('vote_average'),
                    "poster": item.get('poster_path'),
                    "source": self._rank_sources.get(source, source),
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "unique": unique_flag
                })
                return True
            else:
                logger.warning(f"添加订阅失败：{message}")
                return False

        except Exception as e:
            logger.error(f"添加订阅失败：{str(e)}")
            return False
