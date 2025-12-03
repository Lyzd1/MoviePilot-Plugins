# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a MoviePilot plugin for automatic cross-seeding using the IYUU API. It scans torrents in configured downloaders (qBittorrent/Transmission), queries IYUU for matching torrents on other PT sites, and automatically adds them to the downloader.

## Architecture

- `__init__.py` - Main plugin class `IYUUAutoSeed` extending `_PluginBase`. Handles:
  - Plugin configuration and UI form generation
  - Scheduled task execution via APScheduler
  - Torrent scanning from downloaders
  - Cross-seed matching and downloading
  - Torrent verification monitoring

- `iyuu_helper.py` - API client for IYUU service (`https://2025.iyuu.cn`). Handles:
  - Site list retrieval
  - Hash-based seed info queries
  - Token authentication

## Key Dependencies

This plugin runs within the MoviePilot framework and imports from:
- `app.plugins._PluginBase` - Base plugin class
- `app.helper.downloader.DownloaderHelper` - Downloader management
- `app.helper.sites.SitesHelper` - Site configuration
- `app.helper.torrent.TorrentHelper` - Torrent file handling
- `app.core.event.eventmanager` - Event system for site deletion handling

## Plugin Configuration

Key config fields stored via `update_config()`:
- `token` - IYUU API token
- `downloaders` - List of downloader names to scan
- `auto_downloader` - Optional separate downloader for cross-seeded torrents
- `sites` - Site IDs to cross-seed to
- Caching: `success_caches`, `error_caches`, `permanent_error_caches`

## Special Site Handling

The `__get_download_url` method has special logic for:
- M-Team: Uses API with x-api-key header
- Monika: Requires RSS key extraction
- GPW (GreatPosterWall): Parses download link from torrent page
- Sites with passkey/authkey in URLs: Scrapes from details page
