import json
import re
import time
from typing import Any, List, Dict, Tuple

from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.site_oper import SiteOper
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, SystemConfigKey, MediaType


class SubscribeGroup(_PluginBase):
    # 插件名称
    plugin_name = "订阅规则自动填充"
    # 插件描述
    plugin_desc = "电视剧下载后自动添加官组等信息到订阅；添加订阅后根据二级分类名称自定义订阅规则。"
    # 插件图标
    plugin_icon = "teamwork.png"
    # 插件版本
    plugin_version = "3.3.3"
    # 插件作者
    plugin_author = "Lyzd1,thsrite"
    # 作者主页
    author_url = "https://github.com/Lyzd1"
    # 插件配置项ID前缀
    plugin_config_prefix = "subscribegroup_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled: bool = False
    _category: bool = False
    _clear = False
    _clear_handle = False
    _debug: bool = False
    _update_details = []
    _update_confs = None
    _web_source_confs = None
    _subtitle_confs = None
    _subscribe_confs = {}
    _download_web_source_rules = {}
    _download_subtitle_rules = []
    _subscribeoper = None
    _downloadhistoryoper = None
    _siteoper = None

    def init_plugin(self, config: dict = None):
        self._downloadhistoryoper = DownloadHistoryOper()
        self._subscribeoper = SubscribeOper()
        self._siteoper = SiteOper()

        if config:
            self._enabled = config.get("enabled")
            self._category = config.get("category")
            self._clear = config.get("clear")
            self._clear_handle = config.get("clear_handle")
            self._debug = config.get("debug")
            self._update_details = config.get("update_details") or []
            self._update_confs = config.get("update_confs")
            self._web_source_confs = config.get("web_source_confs")
            self._subtitle_confs = config.get("subtitle_confs")

            if self._debug:
                logger.info("=" * 60)
                logger.info("【订阅规则自动填充插件】调试模式已开启")
                logger.info(f"初始化配置: enabled={self._enabled}, category={self._category}, debug={self._debug}")
                logger.info(f"更新详情配置: {self._update_details}")
                logger.info("=" * 60)

            # 解析 web_source_confs
            if self._web_source_confs:
                self._download_web_source_rules = {}
                for confs in str(self._web_source_confs).split("\n"):
                    if ":" in confs:
                        k = confs.split(":")[0].strip()
                        v = ":".join(confs.split(":")[1:]).strip()
                        if k and v:
                            self._download_web_source_rules[k] = v
                logger.info(f"获取到Web源自定义配置 {len(self._download_web_source_rules.keys())} 个")
                if self._debug:
                    logger.info(f"Web源规则详情: {self._download_web_source_rules}")
            else:
                self._download_web_source_rules = {}

            # 解析副标题匹配规则
            if self._subtitle_confs:
                self._download_subtitle_rules = []
                for rule in str(self._subtitle_confs).split("\n"):
                    rule = rule.strip()
                    if rule:
                        try:
                            re.compile(rule)
                            self._download_subtitle_rules.append(rule)
                            logger.info(f"添加副标题规则: {rule}")
                        except re.error as e:
                            logger.error(f"副标题规则无效: {rule}, 错误: {e}")
                logger.info(f"获取到副标题匹配规则 {len(self._download_subtitle_rules)} 个")
                if self._debug:
                    logger.info(f"副标题规则列表: {self._download_subtitle_rules}")
            else:
                self._download_subtitle_rules = []

            if self._update_confs:
                active_sites = self._siteoper.list_active()
                for confs in str(self._update_confs).split("\n"):
                    category = None
                    resolution = None
                    quality = None
                    effect = None
                    include = None
                    exclude = None
                    savepath = None
                    sites = []
                    filter_groups = []
                    for conf in str(confs).split("#"):
                        if ":" in conf:
                            k = conf.split(":")[0]
                            v = ":".join(conf.split(":")[1:])
                            if k == "category":
                                category = v
                            if k == "resolution":
                                resolution = v
                            if k == "quality":
                                quality = v
                            if k == "effect":
                                effect = v
                            if k == "include":
                                include = v
                            if k == "exclude":
                                exclude = v
                            if k == "savepath":
                                savepath = v
                            if k == "sites":
                                for site_name in str(v).split(","):
                                    for active_site in active_sites:
                                        if str(site_name) == str(active_site.name):
                                            sites.append(active_site.id)
                                            break
                            if k == "filter_groups":
                                filter_groups = [filter_group for filter_group in str(v).split(",")]

                    if category:
                        for c in str(category).split(","):
                            self._subscribe_confs[c] = {
                                'resolution': resolution,
                                'quality': quality,
                                'effect': effect,
                                'include': include,
                                'exclude': exclude,
                                'savepath': savepath,
                                'sites': sites,
                                'filter_groups': filter_groups
                            }
                logger.info(f"获取到二级分类自定义配置 {len(self._subscribe_confs.keys())} 个")
                if self._debug:
                    logger.info(f"二级分类配置详情: {self._subscribe_confs}")
            else:
                self._subscribe_confs = {}

            if self._clear_handle:
                self.del_data(key="history_handle")
                if self._debug:
                    logger.info("已处理历史记录已清理")
                self._clear_handle = False
                self.__update_config()
                logger.info("已处理历史清理完成")

            if self._clear:
                self.del_data(key="history")
                if self._debug:
                    logger.info("历史记录已清理")
                self._clear = False
                self.__update_config()
                logger.info("历史记录清理完成")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "category": self._category,
            "clear": self._clear,
            "clear_handle": self._clear_handle,
            "debug": self._debug,
            "update_details": self._update_details,
            "update_confs": self._update_confs,
            "web_source_confs": self._web_source_confs,
            "subtitle_confs": self._subtitle_confs,
        })

    @eventmanager.register(EventType.SubscribeAdded)
    def subscribe_notice(self, event: Event = None):
        """
        添加订阅根据二级分类填充订阅
        """
        if not event:
            logger.error("订阅事件数据为空")
            return

        if self._debug:
            logger.info("=" * 60)
            logger.info("【SubscribeAdded事件】接收到订阅添加事件")
            if hasattr(event, 'event_data'):
                logger.info(f"事件数据内容: {event.event_data}")
            logger.info("=" * 60)

        if not self._category:
            if self._debug:
                logger.info("二级分类自定义填充未开启")
            return

        if len(self._subscribe_confs.keys()) == 0:
            logger.error("插件未开启二级分类自定义填充")
            return

        if event:
            event_data = event.event_data
            if not event_data or not event_data.get("subscribe_id") or not event_data.get("mediainfo"):
                logger.error(f"订阅事件数据不完整 {event_data}")
                return

            sid = event_data.get("subscribe_id")
            category = event_data.get("mediainfo").get("category")
            
            if not category:
                media_info = self.chain.recognize_media(mtype=MediaType(event_data.get("mediainfo").get("type")),
                                                        tmdbid=event_data.get("mediainfo").get("tmdb_id"))
                if media_info and media_info.category:
                    category = media_info.category
                    logger.info(f"订阅ID:{sid} 二级分类:{category} 已通过媒体信息识别")
                else:
                    logger.error(f"订阅ID:{sid} 未获取到二级分类")
                    return

            if category not in self._subscribe_confs.keys():
                logger.error(f"订阅ID:{sid} 二级分类:{category} 未配置自定义规则")
                return

            subscribe = self._subscribeoper.get(sid)
            category_conf = self._subscribe_confs.get(category)

            logger.info(f"订阅记录:{subscribe.name} 二级分类:{category} 自定义配置:{category_conf}")

            update_dict = {}
            if category_conf.get('include'):
                update_dict['include'] = category_conf.get('include')
            if category_conf.get('exclude'):
                update_dict['exclude'] = category_conf.get('exclude')
            if category_conf.get('sites'):
                update_dict['sites'] = category_conf.get('sites')
            if category_conf.get('filter_groups'):
                update_dict['filter_groups'] = category_conf.get('filter_groups')
            if category_conf.get('resolution'):
                update_dict['resolution'] = self.__parse_pix(category_conf.get('resolution'))
            if category_conf.get('quality'):
                update_dict['quality'] = self.__parse_type(category_conf.get('quality'), None)
            if category_conf.get('effect'):
                update_dict['effect'] = self.__parse_effect(category_conf.get('effect'))
            if category_conf.get('savepath'):
                if '{name}' in category_conf.get('savepath'):
                    savepath = category_conf.get('savepath').replace('{name}', f"{subscribe.name} ({subscribe.year})")
                    update_dict['save_path'] = savepath
                else:
                    update_dict['save_path'] = category_conf.get('savepath')

            self._subscribeoper.update(sid, update_dict)
            logger.info(f"订阅记录:{subscribe.name} 填充成功\n{update_dict}")

            history = self.get_data('history') or []
            history.append({
                'name': subscribe.name,
                'type': f'二级分类自定义配置 {category}',
                'content': json.dumps(update_dict),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
            })
            self.save_data(key="history", value=history)

    @eventmanager.register(EventType.DownloadAdded)
    def download_notice(self, event: Event = None):
        """
        添加下载填充订阅制作组等信息
        """
        if not event:
            logger.error("下载事件数据为空")
            return

        if self._debug:
            logger.info("=" * 60)
            logger.info("【DownloadAdded事件】接收到下载添加事件")
            if hasattr(event, 'event_data'):
                context = event.event_data.get("context")
                if context:
                    if hasattr(context, 'torrent_info') and context.torrent_info:
                        logger.info(f"Torrent Info Site: {context.torrent_info.site}")
                        logger.info(f"Torrent Info Site Name: {context.torrent_info.site_name}")
                        logger.info(f"Torrent Info Description: {context.torrent_info.description}")
                    if hasattr(context, 'meta_info') and context.meta_info:
                        logger.info(f"Meta Info Web Source: {context.meta_info.web_source}")
                        logger.info(f"Meta Info Resource Team: {context.meta_info.resource_team}")
                    if hasattr(context, 'media_info') and context.media_info:
                        logger.info(f"Media Info Category: {context.media_info.category}")
            logger.info("=" * 60)

        if not self._enabled:
            if self._debug:
                logger.info("种子下载自定义填充未开启")
            return

        # 关键修复：检查 "规则匹配" 而不是 "制作组"
        if "规则匹配" not in self._update_details:
            if self._debug:
                logger.info("规则匹配未在填充内容中勾选")
            # 注意：这里只返回规则匹配功能，其他功能继续执行
            # 不整体return，让其他功能（分辨率、质量等）继续执行

        if event:
            event_data = event.event_data
            if not event_data or not event_data.get("hash") or not event_data.get("context"):
                logger.error(f"下载事件数据不完整 {event_data}")
                return
            download_hash = event_data.get("hash")
            
            download_history = self._downloadhistoryoper.get_by_hash(download_hash)
            if not download_history:
                logger.warning(f"种子hash:{download_hash} 对应下载记录不存在")
                return

            history_handle: List[str] = self.get_data('history_handle') or []

            if f"{download_history.type}:{download_history.tmdbid}" in history_handle:
                logger.info(f"下载历史:{download_history.title} 已处理过，不再重复处理")
                return

            if download_history.type != '电视剧':
                if self._debug:
                    logger.info(f"下载历史:{download_history.title} 不是电视剧，跳过")
                return

            subscribes = self._subscribeoper.list_by_tmdbid(tmdbid=download_history.tmdbid,
                                                            season=int(download_history.seasons.replace('S', ''))
                                                            if download_history.seasons and
                                                               download_history.seasons.count('-') == 0 else None)
            if not subscribes or len(subscribes) == 0:
                logger.warning(f"下载历史:{download_history.title} tmdbid:{download_history.tmdbid} 对应订阅记录不存在")
                return

            logger.info(f"获取到tmdbid {download_history.tmdbid} 订阅记录:{len(subscribes)} 个")

            for subscribe in subscribes:
                if subscribe.type != '电视剧':
                    continue

                if self._debug:
                    logger.info(f"处理订阅: {subscribe.name}, 当前include={subscribe.include}")

                context = event_data.get("context")
                _torrent = context.torrent_info
                _meta = context.meta_info

                update_dict = {}
                
                # 分辨率
                if "分辨率" in self._update_details and not subscribe.resolution:
                    resource_pix = _meta.resource_pix if _meta else None
                    if resource_pix:
                        resource_pix = self.__parse_pix(resource_pix)
                        if resource_pix:
                            update_dict['resolution'] = resource_pix
                            if self._debug:
                                logger.info(f"分辨率填充: {resource_pix}")
                
                # 资源质量
                if "资源质量" in self._update_details and not subscribe.quality:
                    resource_type = _meta.resource_type if _meta else None
                    video_encode = _meta.video_encode if _meta else None
                    parsed_quality = self.__parse_type(resource_type, video_encode)
                    if parsed_quality:
                        update_dict['quality'] = parsed_quality
                        if self._debug:
                            logger.info(f"资源质量填充: {parsed_quality}")

                # 特效
                if "特效" in self._update_details and not subscribe.effect:
                    resource_effect = _meta.resource_effect if _meta else None
                    if resource_effect:
                        resource_effect = self.__parse_effect(resource_effect)
                        if resource_effect:
                            update_dict['effect'] = resource_effect
                            if self._debug:
                                logger.info(f"特效填充: {resource_effect}")

                # 规则匹配（副标题优先，Web源次之）
                if "规则匹配" in self._update_details and not subscribe.include:
                    description = _torrent.description if _torrent and hasattr(_torrent, 'description') else ""
                    web_source = _meta.web_source if _meta else None

                    logger.info(f"========== 规则匹配开始 ==========")
                    logger.info(f"订阅名称: {subscribe.name}")
                    logger.info(f"Web源: {web_source}")
                    logger.info(f"Description: {description}")
                    logger.info(f"副标题规则数量: {len(self._download_subtitle_rules)}")
                    logger.info(f"Web源规则数量: {len(self._download_web_source_rules)}")
                    
                    include_value = None
                    match_source = None

                    # 1. 优先副标题匹配规则
                    if self._download_subtitle_rules and description:
                        for rule in self._download_subtitle_rules:
                            try:
                                if re.search(rule, description, re.IGNORECASE):
                                    include_value = rule
                                    match_source = "副标题匹配"
                                    logger.info(f"✅ 副标题匹配成功! 规则={rule}")
                                    break
                                else:
                                    logger.info(f"❌ 副标题匹配失败: {rule}")
                            except re.error as e:
                                logger.error(f"副标题规则执行错误: {rule}, 错误: {e}")

                    # 2. 如果没有副标题匹配，再尝试Web源规则
                    if not include_value and web_source and web_source in self._download_web_source_rules:
                        include_value = self._download_web_source_rules.get(web_source)
                        match_source = "Web源规则"
                        logger.info(f"✅ Web源规则匹配成功! web_source={web_source}, 填充值={include_value}")

                    if include_value:
                        update_dict['include'] = include_value
                        logger.info(f"🎯 最终include值: {include_value} (来源: {match_source})")
                    else:
                        logger.info(f"❌ 未匹配到任何规则")
                    
                    logger.info(f"========== 规则匹配结束 ==========")

                # 站点
                if "站点" in self._update_details and (not subscribe.sites or (subscribe.sites and len(subscribe.sites) == 0)):
                    rss_sites = self.systemconfig.get(SystemConfigKey.RssSites) or []
                    if _torrent and _torrent.site and int(_torrent.site) in rss_sites:
                        update_dict['sites'] = [_torrent.site]
                        if self._debug:
                            logger.info(f"站点填充: {_torrent.site}")

                if update_dict:
                    self._subscribeoper.update(subscribe.id, update_dict)
                    logger.info(f"订阅记录:{subscribe.name} 填充成功\n {update_dict}")

                    history = self.get_data('history') or []
                    history.append({
                        'name': subscribe.name,
                        'type': '种子下载自定义配置',
                        'content': json.dumps(update_dict),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                    })
                    self.save_data(key="history", value=history)

                    history_handle.append(f"{download_history.type}:{download_history.tmdbid}")
                    self.save_data('history_handle', history_handle)
                    if self._debug:
                        logger.info(f"已处理记录添加: {download_history.type}:{download_history.tmdbid}")
                else:
                    logger.info(f"订阅记录:{subscribe.name} 无需填充")

    def __parse_pix(self, resource_pix):
        if re.match(r"1080[pi]|x1080", resource_pix, re.IGNORECASE):
            return "1080[pi]|x1080"
        if re.match(r"4K|2160p|x2160", resource_pix, re.IGNORECASE):
            return "4K|2160p|x2160"
        if re.match(r"720[pi]|x720", resource_pix, re.IGNORECASE):
            return "720[pi]|x720"
        return resource_pix

    def __parse_type(self, resource_type, video_encode):
        if resource_type:
            if re.match(r"Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD", resource_type, re.IGNORECASE):
                return "Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD"
            if re.match(r"Remux", resource_type, re.IGNORECASE):
                return "Remux"
            if re.match(r"Blu-?Ray", resource_type, re.IGNORECASE):
                return "Blu-?Ray"
            if re.match(r"UHD|UltraHD", resource_type, re.IGNORECASE):
                return "UHD|UltraHD"

        if video_encode:
            if re.match(r"[Hx].?265|HEVC", video_encode, re.IGNORECASE):
                return "[Hx].?265|HEVC"
            if re.match(r"[Hx].?264|AVC", video_encode, re.IGNORECASE):
                return "[Hx].?264|AVC"

        if resource_type:
            if re.match(r"WEB-?DL|WEB-?RIP", resource_type, re.IGNORECASE):
                return "WEB-?DL|WEB-?RIP"
            if re.match(r"HDTV", resource_type, re.IGNORECASE):
                return "HDTV"
        
        if resource_type:
            if re.match(r"[Hx].?265|HEVC", resource_type, re.IGNORECASE):
                return "[Hx].?265|HEVC"
            if re.match(r"[Hx].?264|AVC", resource_type, re.IGNORECASE):
                return "[Hx].?264|AVC"

        return None

    def __parse_effect(self, resource_effect):
        if re.match(r"Dolby[\\s.]+Vision|DOVI|[\\s.]+DV[\\s.]+", resource_effect, re.IGNORECASE):
            return "Dolby[\\s.]+Vision|DOVI|[\\s.]+DV[\\s.]+"
        if re.match(r"Dolby[\\s.]*\\+?Atmos|Atmos", resource_effect, re.IGNORECASE):
            return "Dolby[\\s.]*\\+?Atmos|Atmos"
        if re.match(r"[\\s.]+HDR[\\s.]+|HDR10|HDR10\\+", resource_effect, re.IGNORECASE):
            return "[\\s.]+HDR[\\s.]+|HDR10|HDR10\\+"
        if re.match(r"[\\s.]+SDR[\\s.]+", resource_effect, re.IGNORECASE):
            return "[\\s.]+SDR[\\s.]+"
        return resource_effect

    def get_state(self) -> bool:
        return self._enabled or self._category

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '种子下载自定义填充'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'category', 'label': '二级分类自定义填充'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'clear', 'label': '清理历史记录'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'clear_handle', 'label': '清理已处理记录'}}
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'update_details',
                                            'label': '种子下载填充内容',
                                            'items': [
                                                {"title": "资源质量", "value": "资源质量"},
                                                {"title": "分辨率", "value": "分辨率"},
                                                {"title": "特效", "value": "特效"},
                                                {"title": "规则匹配", "value": "规则匹配"},  # 关键修复：value改为"规则匹配"
                                                {"title": "站点", "value": "站点"}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'debug',
                                            'label': '调试模式',
                                            'hint': '开启后会输出详细的调试日志，方便排查问题',
                                            'persistent-hint': True
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'subtitle_confs',
                                            'label': '副标题匹配规则（优先级高）',
                                            'rows': 5,
                                            'placeholder': '无.*水印\n国粤双语\nH.*265\n4K.*重制\n\n规则说明：\n每行一个正则表达式，匹配种子的description字段\n匹配成功时，将该正则表达式填入include字段'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'web_source_confs',
                                            'label': 'Web源规则（优先级低）',
                                            'rows': 5,
                                            'placeholder': 'Netflix:NF\nKKTV:KKTV\nDisney:D+\nAmazon:APV\nHBO:MAX\nApple:ATVP'
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'update_confs',
                                            'label': '二级分类自定义填充',
                                            'rows': 3,
                                            'placeholder': 'category:日番#include:.*(CR.*简繁|简繁英).RLWeb|ADWeb.#sites:观众,红叶PT'
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '规则优先级：副标题匹配 > Web源规则\n'
                                                    '1. 副标题匹配：匹配种子的description字段，支持正则表达式\n'
                                                    '2. Web源规则：匹配种子的web_source字段，仅当副标题未匹配时使用'
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'error',
                                            'variant': 'tonal',
                                            'text': '种子下载自定义填充：需要下载种子才会填充订阅属性，且不会覆盖原有属性！'
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'error',
                                            'variant': 'tonal',
                                            'text': '二级分类自定义填充：添加订阅才会填充订阅属性，会强制覆盖！用于根据二级分类自定义订阅规则。'
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
            "category": False,
            "clear": False,
            "clear_handle": False,
            "debug": False,
            "update_details": [],
            "update_confs": "",
            "web_source_confs": "",
            "subtitle_confs": "",
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data('history')
        if not historys:
            return [{'component': 'div', 'text': '暂无数据', 'props': {'class': 'text-center'}}]

        if not isinstance(historys, list):
            historys = [historys]

        historys = sorted(historys, key=lambda x: x.get("time") or 0, reverse=True)

        contens = [
            {
                'component': 'tr',
                'props': {'class': 'text-sm'},
                'content': [
                    {'component': 'td', 'props': {'class': 'whitespace-nowrap break-keep text-high-emphasis'}, 'text': history.get("time")},
                    {'component': 'td', 'text': history.get("name")},
                    {'component': 'td', 'text': history.get("type")},
                    {'component': 'td', 'text': history.get("content").encode('utf-8').decode('unicode_escape') if history.get("content") else ''}
                ]
            } for history in historys
        ]

        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {'hover': True},
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '执行时间'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '订阅名称'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '更新类型'},
                                            {'component': 'th', 'props': {'class': 'text-start ps-4'}, 'text': '更新内容'},
                                        ]
                                    },
                                    {'component': 'tbody', 'content': contens}
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        pass
