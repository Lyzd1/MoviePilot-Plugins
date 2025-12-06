# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a MoviePilot plugin (魔力兑换助手/Bonus Exchange Assistant) that automatically monitors PT site share ratios and executes bonus-to-upload exchanges when thresholds are met.

## Architecture

The plugin follows MoviePilot's plugin architecture (`_PluginBase`):

- `__init__.py` - Main plugin class `BonusExchangePlugin` with UI form definition, scheduler setup, and monitoring logic
- `bonus_exchange_config.py` - Configuration dataclasses (`BonusExchangeConfig`, `SiteExchangeConfig`, `ExchangeRule`) with rule parsing
- `exchange_*.py` - Site-specific exchange handlers implementing `execute_exchange()` method

## Key Dependencies

- MoviePilot core: `app.plugins._PluginBase`, `app.log.logger`, `app.helper.sites.SitesHelper`, `app.db.site_oper.SiteOper`
- APScheduler for cron-based scheduling
- Responds to `EventType.SiteRefreshed` events

## Exchange Rule Format

Rules are configured per-line: `站点名称 [上传量阈值] 等级 下载量 价格;等级 下载量 价格`

Example: `学校 500G 2 5G 2300;3 10G 4200` means when upload < 500GB and bonus > 2300, use option 2 to exchange 5GB.

## Site-Specific Logic

- Standard sites (`Exchange001`): Cookie auth, POST to `/mybonus.php?action=exchange`
- M-Team (`ExchangeMteam`): API key auth, quantity = `bonus // 500`, max 1 exchange per cycle

## Exchange Flow

- Continuous exchange: max 5 per cycle (1 for M-Team), 30s interval between exchanges
- Global state: `last_exchange_time` and `site_current_bonus` track exchange timing and dynamic bonus deduction
