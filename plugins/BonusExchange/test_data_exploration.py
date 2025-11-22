#!/usr/bin/env python3
"""
数据探索测试脚本
用于探索Moviepilot系统中可用的站点数据和统计信息
"""

import sys
import os

# 添加Moviepilot路径到系统路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.helper.sites import SitesHelper
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger


def explore_sites_data():
    """探索站点数据"""
    print("=== 站点数据探索 ===")

    sites_helper = SitesHelper()
    site_oper = SiteOper()

    # 获取所有站点
    sites = sites_helper.get_indexers()
    print(f"系统中可用的站点数量: {len(sites)}")

    for site in sites:
        print(f"\n站点信息:")
        print(f"  ID: {site.get('id')}")
        print(f"  名称: {site.get('name')}")
        print(f"  公开站点: {site.get('public')}")
        print(f"  域名: {site.get('domain')}")

        # 获取站点详细信息
        site_info = site_oper.get(site.get('id'))
        if site_info:
            print(f"  详细信息: {site_info}")
        else:
            print(f"  详细信息: 无")


def explore_user_data():
    """探索用户数据"""
    print("\n=== 用户数据探索 ===")

    site_oper = SiteOper()

    # 获取当天数据
    from datetime import datetime
    import pytz
    from app.core.config import settings

    current_day = datetime.now(tz=pytz.timezone(settings.TZ)).date()
    print(f"当前日期: {current_day}")

    # 获取用户数据
    user_data = site_oper.get_userdata_by_date(date=str(current_day))
    print(f"当天用户数据数量: {len(user_data) if user_data else 0}")

    if user_data:
        for data in user_data:
            print(f"\n用户数据:")
            print(f"  站点名称: {data.name}")
            print(f"  数据对象: {data}")

            # 转换为字典查看所有字段
            data_dict = data.to_dict() if hasattr(data, 'to_dict') else {}
            print(f"  数据字段:")
            for key, value in data_dict.items():
                print(f"    {key}: {value}")


def explore_system_config():
    """探索系统配置"""
    print("\n=== 系统配置探索 ===")

    system_config = SystemConfigOper()

    # 搜索站点配置
    indexer_sites = system_config.get(key="IndexerSites")
    print(f"搜索站点配置: {indexer_sites}")

    # 订阅站点配置
    rss_sites = system_config.get(key="RssSites")
    print(f"订阅站点配置: {rss_sites}")


if __name__ == "__main__":
    try:
        explore_sites_data()
        explore_user_data()
        explore_system_config()
        print("\n=== 数据探索完成 ===")
    except Exception as e:
        print(f"数据探索失败: {e}")
        import traceback
        traceback.print_exc()