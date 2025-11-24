# Openlist 视频文件同步插件

## 插件概述
该插件（`OpenlistMover`）用于监控本地目录中的新视频文件，并自动通过 Openlist API 将这些文件移动到指定的云盘目录。它还支持移动任务监控、STRM 文件同步生成与复制，以及“洗版”模式（覆盖已存在文件）。

## 主要功能
*   **本地目录监控**: 监控用户配置的本地目录，检测新创建或移动（移入）的视频文件。
*   **Openlist 文件移动**: 当检测到符合条件的视频文件时，调用 Openlist API 将其从 Openlist 源目录移动到 Openlist 目标目录。
*   **STRM 文件同步**: 文件成功移动后，插件将触发 Openlist List API 生成 `.strm` 文件和 `-mediainfo.json` 文件，并将其从 STRM 驱动源目录复制到 STRM 本地目标目录。
*   **洗版模式**: 如果目标云盘目录中存在同名文件，可以配置为自动覆盖（洗版），并在洗版成功后删除旧的 STRM 文件并重新生成。
*   **任务状态监控**: 维护一个任务列表，显示文件移动和 STRM 复制的状态（等待中、进行中、成功、失败）。
*   **任务记录自动清空**: 根据配置的阈值，自动清空 Openlist API 中的成功任务记录和插件面板中的成功任务显示记录。
*   **全局扫描**: 定时扫描本地监控目录，处理在监控期间可能遗漏或未成功上传的文件。
*   **自动获取 Openlist 配置**: 如果插件配置中 Openlist URL 或 Token 为空，会尝试从 MoviePilot 系统存储中自动读取类型为 'alist' 或 'openlist' 的配置。

## 配置项

### Openlist API 配置
*   **启用插件 (`enabled`)**: 开关，控制插件的整体启用/禁用。
*   **发送通知 (`notify`)**: 开关，控制是否发送通知消息。
*   **Openlist URL (`openlist_url`)**: Openlist 服务的基础 URL，例如 `http://127.0.0.1:5244`。如果留空，将尝试从系统存储中自动获取。
*   **Openlist Token (`openlist_token`)**: Openlist 管理员 Token。如果留空，将尝试从系统存储中自动获取。

### 监控和映射配置
*   **本地监控目录 (`monitor_paths`)**: MoviePilot 可以访问到的本地绝对路径，每行一个。例如：`/downloads/watch`。
*   **文件移动路径映射 (`path_mappings`)**: 定义本地路径、Openlist 源路径和 Openlist 目标路径之间的映射关系。
    *   格式：`本地监控目录:Openlist源目录:Openlist目标目录`，每行一条规则。
    *   例如：`/downloads/watch:/Local/watch:/YP/Video`
        *   说明：当本地监控到 `/downloads/watch/电影/S01/E01.mkv` 时，Openlist 将会执行移动操作：源 `/Local/watch/电影/S01/E01.mkv` 到目标 `/YP/Video/电影/S01/E01.mkv`。

*   **STRM 复制路径映射 (`strm_path_mappings`)**: 定义 Openlist 目标路径、STRM 驱动源目录和 STRM 本地目标目录之间的映射关系。
    *   格式：`Openlist目标目录前缀:Strm驱动源目录前缀:Strm本地目标目录前缀`，每行一条规则。
    *   例如：`/YP/Video:/strm139:/strm`
        *   说明：当文件成功移动到 `/YP/Video/...` 后，插件将首先 `list /strm139/...` 触发 `.strm` 文件生成，然后将 `.strm` 文件从 `/strm139/...` 复制到 `/strm/...`。

### 洗版模式配置
*   **启用洗版模式 (`wash_mode_enabled`)**: 开关，当目标文件已存在时，是否自动使用覆盖模式重新移动。洗版成功后，会先删除旧的 STRM 文件，等待指定延迟后再重新生成。
*   **洗版延迟 (秒) (`wash_delay_seconds`)**: 删除旧 STRM 文件后等待的秒数，默认 60 秒。

### 任务记录自动清空配置
*   **清空Openlist任务API阈值 (次) (`clear_api_threshold`)**: 成功移动任务达到此次数后，自动清空 Openlist API 中的任务记录，默认 10 次。
*   **清空面板成功记录阈值 (次) (`clear_panel_threshold`)**: 成功移动任务达到此次数后，自动清空插件面板中的成功任务记录，默认 30 次。
*   **清空面板时保留数量 (`keep_successful_tasks`)**: 清空插件面板时，保留最新的成功任务数量，默认 3 个。

### 视频文件后缀配置
*   **视频文件后缀 (`video_extensions`)**: 定义哪些文件扩展名被视为视频文件。每行一个后缀，例如：`.mkv`、`.mp4` 等。

### 全局扫描配置
*   **启用全局扫描 (`global_scan_enabled`)**: 开关，是否每天定时扫描本地监控目录，检查是否有未成功上传的文件并重新上传。
*   **扫描时间 (HH:MM) (`global_scan_time`)**: 全局扫描的执行时间，例如：`02:00` (凌晨2点)。

## 工作流程
1.  插件监控“本地监控目录”中新增或移动的视频文件。
2.  检测到文件后，通过配置的“文件移动路径映射”调用 Openlist API 将文件移动到云盘。
3.  文件成功移动到“Openlist目标目录”后，插件根据“STRM 复制路径映射”进行后续操作：
    *   调用 Openlist List API (刷新) 触发 `.strm` 文件生成。
    *   将生成的 `.strm` 文件从 STRM 驱动源目录复制到本地 STRM 目录。
4.  在整个过程中，插件会监控任务状态，并在需要时进行通知、洗版处理和任务记录的自动清空。
5.  如果启用了全局扫描，插件会每天定时检查所有监控目录中是否有遗漏的视频文件并进行处理。
