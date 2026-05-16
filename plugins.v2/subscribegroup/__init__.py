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
    plugin_version = "3.3.0"  # 版本号更新，简化制作组填充逻辑
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
    _subscribe_confs = {}
    _download_web_source_rules = {}
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

            # 调试模式日志：输出插件初始化配置
            if self._debug:
                logger.debug("=" * 60)
                logger.debug("【订阅规则自动填充插件】调试模式已开启")
                logger.debug(f"初始化配置: {config}")
                logger.debug("=" * 60)

            # 解析 web_source_confs
            # 格式：Netflix:NF 或 Netflix:NF.*
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
                    logger.debug(f"Web源规则详情: {self._download_web_source_rules}")
            else:
                self._download_web_source_rules = {}

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
                    logger.debug(f"二级分类配置详情: {self._subscribe_confs}")
            else:
                self._subscribe_confs = {}

            # 清理已处理历史
            if self._clear_handle:
                self.del_data(key="history_handle")
                if self._debug:
                    logger.debug("已处理历史记录已清理")

                self._clear_handle = False
                self.__update_config()
                logger.info("已处理历史清理完成")

            # 清理历史记录
            if self._clear:
                self.del_data(key="history")
                if self._debug:
                    logger.debug("历史记录已清理")

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
        })

    @eventmanager.register(EventType.SubscribeAdded)
    def subscribe_notice(self, event: Event = None):
        """
        添加订阅根据二级分类填充订阅
        """
        if not event:
            logger.error("订阅事件数据为空")
            return

        # 调试模式：输出原始事件数据
        if self._debug:
            logger.debug("=" * 60)
            logger.debug("【SubscribeAdded事件】接收到订阅添加事件")
            logger.debug(f"事件原始数据: {event}")
            if hasattr(event, 'event_data'):
                logger.debug(f"事件数据内容: {event.event_data}")
            logger.debug("=" * 60)

        if not self._category:
            logger.debug("二级分类自定义填充未开启")
            return

        if len(self._subscribe_confs.keys()) == 0:
            logger.error("插件未开启二级分类自定义填充")
            return

        if event:
            event_data = event.event_data
            if not event_data or not event_data.get("subscribe_id") or not event_data.get("mediainfo"):
                logger.error(f"订阅事件数据不完整 {event_data}")
                if self._debug:
                    logger.debug(f"事件数据缺失字段: subscribe_id={event_data.get('subscribe_id')}, mediainfo={event_data.get('mediainfo')}")
                return

            sid = event_data.get("subscribe_id")
            category = event_data.get("mediainfo").get("category")
            
            if self._debug:
                logger.debug(f"订阅ID: {sid}, 原始分类: {category}")
            
            if not category:
                media_info = self.chain.recognize_media(mtype=MediaType(event_data.get("mediainfo").get("type")),
                                                        tmdbid=event_data.get("mediainfo").get("tmdb_id"))
                logger.error(f"订阅ID:{sid} 未获取到二级分类，尝试通过媒体信息识别 {media_info}")
                if self._debug:
                    logger.debug(f"媒体识别结果: {media_info}")
                    if media_info:
                        logger.debug(f"识别到的分类: {media_info.category}")
                
                if media_info and media_info.category:
                    category = media_info.category
                    logger.info(f"订阅ID:{sid} 二级分类:{category} 已通过媒体信息识别")
                else:
                    logger.error(f"订阅ID:{sid} 未获取到二级分类")
                    return

            if category not in self._subscribe_confs.keys():
                logger.error(f"订阅ID:{sid} 二级分类:{category} 未配置自定义规则")
                if self._debug:
                    logger.debug(f"可用分类配置: {list(self._subscribe_confs.keys())}")
                return

            # 查询订阅
            subscribe = self._subscribeoper.get(sid)
            
            if self._debug:
                logger.debug(f"订阅信息: name={subscribe.name}, current_config={subscribe.dict() if hasattr(subscribe, 'dict') else subscribe}")

            # 二级分类自定义配置
            category_conf = self._subscribe_confs.get(category)

            logger.error(
                f"订阅记录:{subscribe.name} 二级分类:{category} 自定义配置:{category_conf}")

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
                # 判断是否有变量{name}
                if '{name}' in category_conf.get('savepath'):
                    savepath = category_conf.get('savepath').replace('{name}', f"{subscribe.name} ({subscribe.year})")
                    update_dict['save_path'] = savepath
                else:
                    update_dict['save_path'] = category_conf.get('savepath')

            if self._debug:
                logger.debug(f"准备更新的字段: {update_dict}")

            # 更新订阅自定义配置
            self._subscribeoper.update(sid, update_dict)
            logger.info(f"订阅记录:{subscribe.name} 填充成功\n{update_dict}")

            # 读取历史记录
            history = self.get_data('history') or []

            history.append({
                'name': subscribe.name,
                'type': f'二级分类自定义配置 {category}',
                'content': json.dumps(update_dict),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
            })
            # 保存历史
            self.save_data(key="history", value=history)

    @eventmanager.register(EventType.DownloadAdded)
    def download_notice(self, event: Event = None):
        """
        添加下载填充订阅制作组等信息
        """
        if not event:
            logger.error("下载事件数据为空")
            return

        # 调试模式：输出原始事件数据
        if self._debug:
            logger.debug("=" * 60)
            logger.debug("【DownloadAdded事件】接收到下载添加事件")
            logger.debug(f"事件原始数据: {event}")
            if hasattr(event, 'event_data'):
                logger.debug(f"事件数据内容: {event.event_data}")
                # 详细输出context中的信息
                context = event.event_data.get("context")
                if context:
                    logger.debug(f"Context类型: {type(context)}")
                    if hasattr(context, 'torrent_info'):
                        logger.debug(f"Torrent Info: {context.torrent_info}")
                    if hasattr(context, 'meta_info'):
                        logger.debug(f"Meta Info: {context.meta_info}")
                        if context.meta_info:
                            logger.debug(f"  - resource_type: {context.meta_info.resource_type}")
                            logger.debug(f"  - resource_pix: {context.meta_info.resource_pix}")
                            logger.debug(f"  - resource_effect: {context.meta_info.resource_effect}")
                            logger.debug(f"  - web_source: {context.meta_info.web_source}")
                            logger.debug(f"  - org_string: {context.meta_info.org_string}")
                            logger.debug(f"  - video_encode: {getattr(context.meta_info, 'video_encode', 'N/A')}")
                    if hasattr(context, 'media_info'):
                        logger.debug(f"Media Info: {context.media_info}")
                        if context.media_info:
                            logger.debug(f"  - category: {context.media_info.category}")
                            logger.debug(f"  - type: {context.media_info.type}")
            logger.debug("=" * 60)

        if not self._enabled:
            if self._debug:
                logger.debug("种子下载自定义填充未开启")
            return

        if len(self._update_details) == 0:
            if self._debug:
                logger.debug("插件未开启更新填充内容")
            return

        if event:
            event_data = event.event_data
            if not event_data or not event_data.get("hash") or not event_data.get("context"):
                logger.error(f"下载事件数据不完整 {event_data}")
                return
            download_hash = event_data.get("hash")
            
            if self._debug:
                logger.debug(f"下载Hash: {download_hash}")
            
            # 根据hash查询下载记录
            download_history = self._downloadhistoryoper.get_by_hash(download_hash)
            if not download_history:
                logger.warning(f"种子hash:{download_hash} 对应下载记录不存在")
                return

            if self._debug:
                logger.debug(f"下载历史记录: title={download_history.title}, type={download_history.type}, tmdbid={download_history.tmdbid}, seasons={download_history.seasons}")

            history_handle: List[str] = self.get_data('history_handle') or []

            if f"{download_history.type}:{download_history.tmdbid}" in history_handle:
                logger.debug(f"下载历史:{download_history.title} 已处理过，不再重复处理")
                if self._debug:
                    logger.debug(f"已处理记录: {history_handle}")
                return

            if download_history.type != '电视剧':
                logger.debug(f"下载历史:{download_history.title} 不是电视剧，不进行官组填充")
                if self._debug:
                    logger.debug(f"类型为: {download_history.type}, 期望类型: 电视剧")
                return

            # 根据下载历史查询订阅记录
            subscribes = self._subscribeoper.list_by_tmdbid(tmdbid=download_history.tmdbid,
                                                            season=int(download_history.seasons.replace('S', ''))
                                                            if download_history.seasons and
                                                               download_history.seasons.count('-') == 0 else None)
            if not subscribes or len(subscribes) == 0:
                logger.warning(f"下载历史:{download_history.title} tmdbid:{download_history.tmdbid} 对应订阅记录不存在")
                if self._debug:
                    logger.debug(f"查询参数: tmdbid={download_history.tmdbid}, season={int(download_history.seasons.replace('S', '')) if download_history.seasons and download_history.seasons.count('-') == 0 else None}")
                return

            logger.info(
                f"获取到tmdbid {download_history.tmdbid} season {int(download_history.seasons.replace('S', '')) if download_history.seasons and download_history.seasons.count('-') == 0 else None} 订阅记录:{len(subscribes)} 个")

            if self._debug:
                logger.debug(f"找到 {len(subscribes)} 个订阅记录: {[s.name for s in subscribes]}")

            for subscribe in subscribes:
                if subscribe.type != '电视剧':
                    logger.debug(f"订阅记录:{subscribe.name} 不是电视剧，不进行官组填充")
                    continue

                if self._debug:
                    logger.debug(f"处理订阅: {subscribe.name}, 当前配置: include={subscribe.include}, resolution={subscribe.resolution}, quality={subscribe.quality}, effect={subscribe.effect}, sites={subscribe.sites}")

                # 开始填充官组和站点
                context = event_data.get("context")
                _torrent = context.torrent_info
                _meta = context.meta_info

                if self._debug:
                    logger.debug(f"更新详情配置: {self._update_details}")

                # 填充数据
                update_dict = {}
                
                # 分辨率
                if "分辨率" in self._update_details and not subscribe.resolution:
                    resource_pix = _meta.resource_pix if _meta else None
                    if self._debug:
                        logger.debug(f"原始分辨率: {resource_pix}")
                    if resource_pix:
                        resource_pix = self.__parse_pix(resource_pix)
                        if resource_pix:
                            update_dict['resolution'] = resource_pix
                            if self._debug:
                                logger.debug(f"解析后分辨率: {resource_pix}")
                        else:
                            logger.warning(f"订阅记录:{subscribe.name} 未获取到分辨率信息")
                
                # 资源质量
                if "资源质量" in self._update_details and not subscribe.quality:
                    resource_type = _meta.resource_type if _meta else None
                    video_encode = _meta.video_encode if _meta else None
                    
                    if self._debug:
                        logger.debug(f"原始资源质量类型: {resource_type}")
                        logger.debug(f"原始视频编码: {video_encode}")
                    
                    parsed_quality = self.__parse_type(resource_type, video_encode)
                    
                    if parsed_quality:
                        update_dict['quality'] = parsed_quality
                        if self._debug:
                            logger.debug(f"解析后质量: {parsed_quality}")
                    else:
                        logger.warning(f"订阅记录:{subscribe.name} 未获取到资源质量信息 (type: {resource_type}, encode: {video_encode})")

                # 特效
                if "特效" in self._update_details and not subscribe.effect:
                    resource_effect = _meta.resource_effect if _meta else None
                    if self._debug:
                        logger.debug(f"原始特效: {resource_effect}")
                    if resource_effect:
                        resource_effect = self.__parse_effect(resource_effect)
                        if resource_effect:
                            update_dict['effect'] = resource_effect
                            if self._debug:
                                logger.debug(f"解析后特效: {resource_effect}")
                        else:
                            logger.warning(f"订阅记录:{subscribe.name} 未获取到特效信息")

                # Web源规则（制作组填充）- 简化版：只填入规则值，不拼接制作组
                if "制作组" in self._update_details and not subscribe.include:
                    web_source = _meta.web_source if _meta else None

                    if self._debug:
                        logger.debug(f"Web源: {web_source}")

                    include_value = None

                    # Web源规则匹配
                    if web_source and web_source in self._download_web_source_rules:
                        include_value = self._download_web_source_rules.get(web_source)
                        logger.info(f"订阅记录:{subscribe.name} 匹配到Web源规则:{web_source}，填充 include:{include_value}")
                        if self._debug:
                            logger.debug(f"Web源规则匹配: web_source={web_source}, 规则值={include_value}")
                    else:
                        if self._debug:
                            logger.debug(f"未匹配到Web源规则，web_source={web_source}")

                    if include_value:
                        update_dict['include'] = include_value
                        if self._debug:
                            logger.debug(f"最终include值: {include_value}")

                # 站点
                if "站点" in self._update_details and (
                        not subscribe.sites or (subscribe.sites and len(subscribe.sites) == 0)):
                    rss_sites = self.systemconfig.get(SystemConfigKey.RssSites) or []
                    if self._debug:
                        logger.debug(f"RSS站点列表: {rss_sites}")
                        logger.debug(f"当前种子站点ID: {_torrent.site if _torrent else None}")
                    
                    if _torrent and _torrent.site and int(_torrent.site) in rss_sites:
                        update_dict['sites'] = [_torrent.site]
                        if self._debug:
                            logger.debug(f"添加站点到订阅: {_torrent.site}")

                if len(update_dict.keys()) == 0:
                    logger.info(f"订阅记录:{subscribe.name} 无需填充")
                    continue

                if self._debug:
                    logger.debug(f"最终更新字典: {update_dict}")

                # 更新订阅记录
                self._subscribeoper.update(subscribe.id, update_dict)
                logger.info(f"订阅记录:{subscribe.name} 填充成功\n {update_dict}")

                # 读取历史记录
                history = self.get_data('history') or []
                history.append({
                    'name': subscribe.name,
                    'type': '种子下载自定义配置',
                    'content': json.dumps(update_dict),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                })
                # 保存历史
                self.save_data(key="history", value=history)

                # 保存已处理历史
                history_handle.append(f"{download_history.type}:{download_history.tmdbid}")
                self.save_data('history_handle', history_handle)
                if self._debug:
                    logger.debug(f"已处理记录添加: {download_history.type}:{download_history.tmdbid}")
                    logger.debug(f"当前已处理记录列表: {history_handle}")

    def __parse_pix(self, resource_pix):
        # 识别1080或者4k或720
        if re.match(r"1080[pi]|x1080", resource_pix, re.IGNORECASE):
            resource_pix = "1080[pi]|x1080"
            return resource_pix
        if re.match(r"4K|2160p|x2160", resource_pix, re.IGNORECASE):
            resource_pix = "4K|2160p|x2160"
            return resource_pix
        if re.match(r"720[pi]|x720", resource_pix, re.IGNORECASE):
            resource_pix = "720[pi]|x720"
            return resource_pix
        return resource_pix

    def __parse_type(self, resource_type, video_encode):
        """
        根据 resource_type (源) 和 video_encode (编码) 
        按照 "高级源 > 编码 > 低级源" 的优先级返回标准化的质量字符串
        """
        # 1. 优先匹配高级源 (来自 resource_type)
        if resource_type:
            if re.match(r"Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD", resource_type, re.IGNORECASE):
                return "Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD"
            if re.match(r"Remux", resource_type, re.IGNORECASE):
                return "Remux"
            if re.match(r"Blu-?Ray", resource_type, re.IGNORECASE):
                return "Blu-?Ray"
            if re.match(r"UHD|UltraHD", resource_type, re.IGNORECASE):
                return "UHD|UltraHD"

        # 2. 其次匹配编码 (来自 video_encode)
        if video_encode:
            if re.match(r"[Hx].?265|HEVC", video_encode, re.IGNORECASE):
                return "[Hx].?265|HEVC"
            if re.match(r"[Hx].?264|AVC", video_encode, re.IGNORECASE):
                return "[Hx].?264|AVC"

        # 3. 最后匹配低级源 (来自 resource_type)
        if resource_type:
            if re.match(r"WEB-?DL|WEB-?RIP", resource_type, re.IGNORECASE):
                return "WEB-?DL|WEB-?RIP"
            if re.match(r"HDTV", resource_type, re.IGNORECASE):
                return "HDTV"
        
        # 4. 降级检查：如果 video_encode 为空，但编码信息在 resource_type 中
        if resource_type:
            if re.match(r"[Hx].?265|HEVC", resource_type, re.IGNORECASE):
                return "[Hx].?265|HEVC"
            if re.match(r"[Hx].?264|AVC", resource_type, re.IGNORECASE):
                return "[Hx].?264|AVC"

        # 均未匹配
        return None

    def __parse_effect(self, resource_effect):
        if re.match(r"Dolby[\\s.]+Vision|DOVI|[\\s.]+DV[\\s.]+", resource_effect, re.IGNORECASE):
            resource_effect = "Dolby[\\s.]+Vision|DOVI|[\\s.]+DV[\\s.]+"
        if re.match(r"Dolby[\\s.]*\\+?Atmos|Atmos", resource_effect, re.IGNORECASE):
            resource_effect = "Dolby[\\s.]*\\+?Atmos|Atmos"
        if re.match(r"[\\s.]+HDR[\\s.]+|HDR10|HDR10\\+", resource_effect, re.IGNORECASE):
            resource_effect = "[\\s.]+HDR[\\s.]+|HDR10|HDR10\\+"
        if re.match(r"[\\s.]+SDR[\\s.]+", resource_effect, re.IGNORECASE):
            resource_effect = "[\\s.]+SDR[\\s.]+"
        return resource_effect

    def get_state(self) -> bool:
        return self._enabled or self._category

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '种子下载自定义填充',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'category',
                                            'label': '二级分类自定义填充',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清理历史记录',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear_handle',
                                            'label': '清理已处理记录',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'update_details',
                                            'label': '种子下载填充内容',
                                            'items': [
                                                {
                                                    "title": "资源质量",
                                                    "vale": "资源质量"
                                                },
                                                {
                                                    "title": "分辨率",
                                                    "vale": "分辨率"
                                                },
                                                {
                                                    "title": "特效",
                                                    "vale": "特效"
                                                },
                                                {
                                                    "title": "Web源规则",
                                                    "vale": "制作组"
                                                },
                                                {
                                                    "title": "站点",
                                                    "vale": "站点"
                                                }
                                            ]
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
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'web_source_confs',
                                            'label': 'Web源规则（制作组）',
                                            'rows': 5,
                                            'placeholder': 'Netflix:NF\n'
                                                           'KKTV:KKTV\n'
                                                           'Disney:D+\n'
                                                           'Amazon:APV\n'
                                                           'HBO:MAX\n'
                                                           'Apple:ATVP\n\n'
                                                           '格式：Web源名称:填充值\n'
                                                           '当种子识别到对应的Web源时，会在订阅的include字段填入:后面的值'
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'update_confs',
                                            'label': '二级分类自定义填充',
                                            'rows': 3,
                                            'placeholder': 'category:日番#include:.*(CR.*简繁|简繁英).RLWeb|ADWeb.#sites:观众,红叶PT\n'
                                                           'category:港台剧,日韩剧#include:国粤'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'Web源规则说明：\n'
                                                    '1. 格式为 "Web源名称:填充值"，例如 "Netflix:NF"\n'
                                                    '2. 当下载的种子识别出Web源（如Netflix、KKTV等）时，会自动将填充值填入订阅的include字段\n'
                                                    '3. 填充值可以是简单标识（如NF），也可以是正则表达式（如.*NF.*）\n'
                                                    '4. 此功能需要勾选种子下载填充内容中的"Web源规则"'
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '电视剧订阅未配置包含关键词、订阅站点等配置时，订阅或搜索下载后，'
                                                    '将下载种子的站点、Web源规则等信息填充到订阅信息中，以保证后续订阅资源的统一性。'
                                                    '（订阅新出的电视剧效果更佳。）'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'error',
                                            'variant': 'tonal',
                                            'text': '二级分类自定义填充：添加订阅才会填充订阅属性，会强制覆盖！用于根据二级分类自定义订阅规则，具体属性明细请查看电视剧订阅设置页面。'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'category:二级分类名称（多个分类名称逗号拼接）,resolution:分辨率,quality:质量,effect:特效,include:包含关键词,'
                                                    'exclude:排除关键词,sites:站点名称（多个站点用逗号拼接）,filter_groups:优先级规则组（多个规则组名称用逗号拼接）,savepath:保存路径/{name}（{name}为当前订阅的名称和年份）。'
                                                    'category必填，多组属性用#分割。例如category:动漫#resolution:1080p'
                                                    '（添加的动漫订阅，指定分辨率为1080p）。'
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
        }

    def get_page(self) -> List[dict]:
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

        if not isinstance(historys, list):
            historys = [historys]

        # 按照时间倒序
        historys = sorted(historys, key=lambda x: x.get("time") or 0, reverse=True)

        contens = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': history.get("time")
                    },
                    {
                        'component': 'td',
                        'text': history.get("name")
                    },
                    {
                        'component': 'td',
                        'text': history.get("type")
                    },
                    {
                        'component': 'td',
                        'text': history.get("content").encode('utf-8').decode('unicode_escape') if history.get(
                            "content") else ''
                    }
                ]
            } for history in historys
        ]

        # 拼装页面
        return [
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
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '执行时间'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '订阅名称'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '更新类型'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '更新内容'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': contens
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
