# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Plugin Overview

This is a MoviePilot plugin that automatically subscribes to content from various ranking sources based on user-defined criteria. The plugin periodically fetches data from supported ranking sources (TMDB, Douban, Bangumi) and automatically creates subscriptions for content that meets the configured filters.

## Key Features

- Supports multiple ranking sources including TMDB trending/movies/TV shows, Douban hot movies/TV shows, and Bangumi calendar
- Configurable filters for minimum rating, media type (movies/TV shows), and maximum items per source
- Automatic subscription creation for qualifying content
- Historical tracking of processed items to prevent duplicates
- Web API endpoints for managing history and retrieving source information

## Architecture

The plugin follows the MoviePilot plugin architecture pattern:

1. **Main Class**: `MoviePilotRankSubscribe` extends `_PluginBase`
2. **Configuration**: Web-based configuration form with Vuetify components
3. **Scheduling**: Uses APScheduler for periodic execution with cron expressions
4. **Data Flow**:
   - Ranking data → Filtering → Media recognition → Subscription creation
   - History tracking prevents duplicate processing
5. **Chains**: Integrates with MoviePilot's RecommendChain, SubscribeChain, and DownloadChain

## Important Methods

- `init_plugin()`: Initializes plugin state from configuration
- `__refresh_rankings()`: Main processing logic for fetching and processing rankings
- `__get_ranking_data()`: Retrieves data from various ranking sources
- `__add_subscription()`: Adds subscriptions for qualifying content
- `get_form()`: Defines the web configuration interface
- `get_page()`: Renders the plugin's detail page with history

## Configuration Options

- Enabled/Disabled state
- Execution schedule (cron expression)
- One-time execution flag
- Ranking sources selection
- Minimum vote threshold (0-10)
- Media type filtering (movies, TV shows, all)
- Maximum items per source
- History clearing option

## Data Storage

- Uses MoviePilot's built-in data storage for history tracking
- History includes title, type, year, vote, poster, source, and timestamp
- Unique identifiers prevent duplicate processing

## API Endpoints

- `/delete_history`: Delete specific history records
- `/get_sources`: Retrieve supported ranking sources

## Dependencies

- MoviePilot framework
- APScheduler for scheduling
- pytz for timezone handling
- Standard Python libraries (asyncio, threading, etc.)