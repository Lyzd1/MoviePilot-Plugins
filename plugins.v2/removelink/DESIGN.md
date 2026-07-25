# StrmCleaner 新插件设计文档

## 一、设计目标

从 `RemoveLink` 插件中独立出 STRM 文件/文件夹删除监控功能，重写为一个全新插件。

### 核心原则

| 原则 | 说明 |
|------|------|
| **仅 STRM** | 不包含硬链接清理，仅处理 `.strm` 文件/文件夹删除 |
| **仅 local + openlist** | 存储类型仅保留 `local` 和 `openlist`（原 alist，接口一致） |
| **主动识别文件/文件夹** | 插件内部通过批量收集 + 事后判定，自行判断是文件删除还是文件夹删除 |
| **统一路径映射 + 音乐前缀识别** | 单一 `path_mappings` 配置，通过 `music_prefixes` 配置判断是否走音乐删除逻辑 |
| **不递归清理空目录** | 删文件只删文件，删文件夹直接删整个文件夹（含其下所有内容），不逐级向上检查空目录 |
| **保留 openlist 本地路径同步** | openlist 删除时同步删除 `openlistlocal` 映射的本地目录 |
| **保留自动读取 openlist 配置** | 未填 URL/Token 时自动从系统存储配置获取 |
| **防雪崩+自动停用** | 短时间大量文件删除时，判定为存储异常，阻止删除并**自动停用插件**，等待手动排查后重新开启 |

> **术语说明**：本文档中 `openlist` 指 OpenList/AList 网盘存储，其 REST API 接口（`/api/fs/remove`）与原 alist 完全一致。`openlistlocal` 表示 openlist 存储对应的本地挂载目录。

---

## 二、与原插件的核心差异

| 维度 | 原 RemoveLink | 新 StrmCleaner |
|------|-------------|----------------|
| 硬链接清理 | 有 | 无 |
| 存储类型 | local, alipan, u115, rclone, alist | 仅 local, openlist |
| 文件 vs 文件夹判断 | 被动接收 OS 事件（先大量文件，后文件夹），靠 `deleted_strm_folders` 集合去重 | **批量收集后主动判断**：检查父目录是否还存在 |
| 日志输出 | 每个文件一行日志，冗长混乱 | 按批次输出，一次操作一条清晰日志 |
| 视频/音乐区分 | 混合映射，靠路径含 `/music/` 区分 | **统一路径映射，通过 `music_prefixes` 前缀配置**识别 |
| 递归空目录清理 | `_delete_storage_empty_folders` 逐级向上 | **不保留** |
| 延迟删除 | 滑动窗口 | 滑动窗口（保留） |
| 元数据清理 | `_delete_storage_scrap_files` + 标准海报 | 保留，新增 `-mediainfo.json` 识别 |
| 防雪崩保护 | 无 | **新增：触发后自动停用插件** |

---

## 三、文件 vs 文件夹识别的核心设计

### 3.1 问题

当用户通过 Emby 删除一个剧集文件夹时，OS 层面是逐文件 `unlink` 后 `rmdir`。watchdog 会依次触发：

```
on_deleted(S01E01.strm)
on_deleted(S01E02.strm)
... (大量元数据文件事件)
on_deleted(S01/)       ← 文件夹事件最后才到
```

原插件把每个事件都 log 一条，造成日志洪水。

### 3.2 方案：批量收集 + 事后判定

```
┌─────────────────────────────────────────────────────────────┐
│                    滑动窗口收集阶段                          │
│                                                             │
│  on_deleted(S01E01.strm) ─→ batch[S01].entries += [E01]    │
│  on_deleted(S01E02.strm) ─→ batch[S01].entries += [E02]    │
│  on_deleted(S01E03.strm) ─→ batch[S01].entries += [E03]    │
│  on_deleted(S01/)       ─→ batch[S01].has_folder_event=True │
│  ...              ← 每个事件重置滑动窗口定时器              │
│                                                             │
│                     ↓ 定时器到期                            │
│                                                             │
│             对于每个 parent_dir 分组:                        │
│                                                             │
│  if has_folder_event or not os.path.exists(parent_dir):     │
│      → 判定为"文件夹删除"                                   │
│      → 删除整个云盘文件夹                                   │
│      → 日志: "检测到文件夹删除 X，将删除云盘目录 Y"         │
│  else:                                                      │
│      → 判定为"文件删除"                                     │
│      → 逐个删除对应云盘文件                                 │
│      → 日志: "检测到 N 个STRM文件删除，将删除云盘文件"      │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 日志对比

**原插件**（删一个 14 集的剧集文件夹）：60+ 条日志

**新插件**（同场景）：

```
[INFO] 检测到文件夹删除: /strm/动漫/国漫/将夜/S01 (14个STRM文件)
[INFO] 将删除云盘文件夹: [openlist] /cloud/动漫/国漫/将夜/S01
[INFO] 同步删除本地目录: [openlistlocal] /mnt/media/动漫/国漫/将夜/S01
[INFO] 已加入延迟队列，等待 240 秒

[INFO] 延迟删除完成: 文件夹 [openlist]/cloud/动漫/国漫/将夜/S01 已删除
```

---

## 四、整体架构

```
StrmCleaner (_PluginBase)
│
├── 配置层
│   ├── [通用]     enabled, notify, delayed_deletion, delay_seconds
│   ├── [路径映射] path_mappings（统一格式，含 openlist 本地映射）
│   ├── [音乐识别] music_prefixes（匹配到则走音乐删除逻辑）
│   ├── [删除选项] delete_metadata, delete_history, exclude_keywords
│   └── [防雪崩]   flood_protection_enabled, flood_threshold, flood_window_seconds
│
├── 监控层
│   └── FileMonitorHandler（单个 handler 统一处理所有 STRM 目录）
│       ├── on_created → 记录到 file_state
│       ├── on_moved  → 维护 file_state
│       └── on_deleted
│           ├── 非 .strm 文件 → 忽略
│           ├── .strm 文件    → 匹配映射 → 判断音乐/视频 → 加入批次
│           └── 目录事件      → 加入批次（标记 has_folder_event）
│
├── 批次管理层
│   └── EventBatch
│       ├── 按 parent_dir 分组
│       ├── 每个分组: entries[], has_folder_event
│       ├── 防雪崩检查（处理前）
│       └── 滑动窗口定时器
│
├── 删除执行层
│   ├── execute_folder_deletion  → 整个云盘文件夹
│   │   ├── openlist: OpenList API /api/fs/remove
│   │   ├── local:  StorageChain.delete_file
│   │   └── 同步删除 openlistlocal 本地目录
│   ├── execute_video_file_deletion → 单个视频文件 + 同名前缀元数据
│   └── execute_music_file_deletion → 所有同名音乐文件 + 歌词文件
│
└── 通知层
    └── 批量通知 / 告警通知
```

---

## 五、配置项设计

### 5.1 通用配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | false | 插件总开关 |
| `notify` | bool | false | 发送通知 |
| `delayed_deletion` | bool | true | 启用延迟删除 |
| `delay_seconds` | int | 30 | 延迟时间(秒)，范围 10-300 |

### 5.2 路径映射

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `path_mappings` | textarea | "" | 路径映射，每行一条 |

**格式**：

```
STRM目录:存储类型:云盘目录[:openlistlocal:本地目录]
```

| 字段 | 说明 |
|------|------|
| `STRM目录` | 本地被监控的 STRM 文件所在目录 |
| `存储类型` | `local` 或 `openlist` |
| `云盘目录` | openlist 网盘路径 或 local 本地路径 |
| `openlistlocal:本地目录` | (可选) openlist 存储时同步删除的本地挂载目录 |

**示例**：

```
/strm/movie:local:/media/movie
/strm/anime:openlist:/cloud/anime:openlistlocal:/mnt/local/anime
/strm/music:openlist:/cloud/music:openlistlocal:/mnt/local/music
/strm/tvshow:openlist:/cloud/tvshow
```

> **注意**：`openlistlocal` 关键字仅在存储类型为 `openlist` 时有效，且映射的本地目录必须是 openlist 挂载的本地路径，删除时通过 OpenList API（`/api/fs/remove`）操作。

### 5.3 音乐识别

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `music_prefixes` | textarea | "" | 音乐目录前缀，每行一个 |

**判定规则**：删除的 STRM 文件/文件夹路径**以任一前缀开头**即判定为音乐，走音乐删除逻辑；否则走视频逻辑。

**示例**：

```
/strm/music
/strm/音乐
```

> `music_prefixes` 与 `path_mappings` 中的 STRM 前缀是独立的两组配置。一个 STRM 文件首先在 `path_mappings` 中找到映射以确定云盘路径，然后在 `music_prefixes` 中检查是否走音乐删除逻辑。

### 5.4 删除选项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `delete_metadata` | bool | false | 删除云盘元数据文件 |
| `delete_history` | bool | false | 删除转移记录 |
| `exclude_keywords` | textarea | "" | 排除关键词，每行一个 |

### 5.5 防雪崩保护

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `flood_protection_enabled` | bool | true | 启用防雪崩保护 |
| `flood_threshold` | int | 50 | 触发阈值（文件数） |
| `flood_window_seconds` | int | 60 | 统计窗口(秒) |

---

## 六、核心数据结构

### 6.1 EventEntry

```python
@dataclass
class EventEntry:
    path: Path                # 文件/文件夹路径
    is_directory: bool        # 是否为目录事件
    is_music: bool            # 是否属于音乐（由 music_prefixes 判定）
    storage_type: str         # "local" 或 "openlist"
    storage_path: str         # 云盘/本地对应路径
    local_storage_type: str   # "openlistlocal" 或 None
    local_storage_path: str   # 本地映射路径 或 None
```

### 6.2 BatchGroup

```python
@dataclass
class BatchGroup:
    parent_dir: str           # 分组的父目录路径
    entries: List[EventEntry] # 该组的所有事件
    has_folder_event: bool    # 是否收到过目录删除事件
```

### 6.3 DeletionTask

```python
@dataclass
class DeletionTask:
    task_type: str            # "folder" 或 "file"
    is_music: bool            # 是否走音乐逻辑
    file_paths: List[Path]    # 待删除 STRM 文件路径（file 模式）
    folder_path: Path         # 待删除 STRM 文件夹路径（folder 模式）
    storage_type: str
    storage_path: str         # 云盘文件/文件夹路径
    local_storage_type: str
    local_storage_path: str
    timestamp: datetime
    processed: bool
```

### 6.4 FloodRecord

```python
@dataclass
class FloodRecord:
    """防雪崩追踪：记录每次批次处理的时间戳和文件数"""
    timestamp: datetime
    file_count: int
```

---

## 七、详细流程

### 7.1 插件初始化 (`init_plugin`)

```
init_plugin(config):
  1. 解析全部配置
  2. stop_service() 停旧服务

  3. 自动获取 openlist 配置
     if path_mappings 中存在 openlist 且未填写 API 配置:
       从 StorageHelper.get_storagies() 读取第一个 openlist 存储配置
       填充 api_url, api_token

  4. 收集监控目录
     从 path_mappings 每行提取 STRM 前缀目录
     去重得到 all_dirs

  5. 启动文件监控
     for dir in all_dirs:
       选择最优 observer (inotify/WindowsApi/Polling)
       observer.schedule(FileMonitorHandler(dir), recursive=True)
       observer.start()

  6. 初始化状态
     file_state = {}          # 仅维护 on_moved/on_created
     batch = {}              # 事件批次
     deletion_queue = []     # 删除任务队列
     flood_records = []      # 防雪崩追踪列表
     _flood_triggered = False # 防雪崩是否已触发（用于阻止后续事件处理）

  7. 日志输出配置摘要
     [INFO] STRM 清理插件已启动，监控目录: [...]
     [INFO] 音乐前缀: [...]
     [INFO] 延迟删除: {delay_seconds}s | 防雪崩: {threshold}文件/{window}s
```

### 7.2 文件监控事件处理

```
on_created(file_path):
  if .strm:
    记录到 file_state（仅用于 on_moved 对比）

on_moved(src, dest):
  file_state 中移除 src，添加 dest

on_deleted(path):
  # 防雪崩已触发 → 完全忽略所有新事件
  if _flood_triggered:
    return

  # 目录事件
  if is_directory:
    判定 is_music: path 是否以任一 music_prefixes 开头
    entry = EventEntry(path, is_directory=True, is_music=..., ...)
    从 path_mappings 中匹配确定 storage_info
    add_to_batch(entry)
    return

  # 文件事件 - 仅处理 .strm
  if path.suffix.lower() != ".strm":
    return  # 非 strm 文件忽略

  # 排除关键词检查
  if match_exclude_keywords(path):
    return

  # 路径映射匹配
  storage_info = from_mapping(path)
  if not storage_info:
    logger.info("未找到路径映射，忽略")
    return

  # 音乐判定
  is_music = any(str(path).startswith(p) for p in music_prefixes)

  entry = EventEntry(path, is_directory=False, is_music=is_music, storage_info)
  add_to_batch(entry)
```

**关键点**：
- 非 `.strm` 文件（如 `.nfo`, `.jpg`）的删除事件**完全忽略**，不由 watchdog 事件驱动处理
- 元数据文件由插件在删除云盘文件时**主动查找删除**，不在监控层面处理
- 防雪崩触发后（`_flood_triggered = True`），所有新入站事件直接丢弃

### 7.3 批次收集与判定

```
add_to_batch(entry):
  parent_dir = os.path.dirname(entry.path)

  if parent_dir not in batch:
    batch[parent_dir] = BatchGroup(parent_dir, [], False)

  batch[parent_dir].entries.append(entry)
  if entry.is_directory:
    batch[parent_dir].has_folder_event = True

  # 重置滑动窗口定时器
  cancel_timer → start_timer(delay_seconds)

process_batch():
  """定时器到期后调用"""

  # ======== 防雪崩检查 ========
  total_files = sum(
    len([e for e in g.entries if not e.is_directory])
    for g in batch.values()
  )
  if check_flood_protection(total_files):
    # 触发熔断 → 自动停用插件
    batch.clear()
    deletion_queue.clear()
    cancel_timer()
    self.stop_service()       # 停止所有 observer
    self._enabled = False     # 标记插件为停用状态
    self._flood_triggered = True
    self.update_config({"enabled": False})  # 持久化停用状态
    return

  # ======== 逐组判定 ========
  for parent_dir, group in batch.items():
    is_folder_deletion = (
      group.has_folder_event
      or not os.path.exists(parent_dir)
    )

    if is_folder_deletion:
      # → 文件夹删除
      ref_entry = (找目录事件 or 第一个文件事件)
      task = DeletionTask(
        task_type="folder",
        is_music=ref_entry.is_music,
        folder_path=Path(parent_dir),
        storage_info=获取云盘文件夹路径(parent_dir),
        ...
      )
      logger.info(f"检测到文件夹删除: {parent_dir} ({文件数}个STRM) → 云盘: [{task.storage_type}] {task.storage_path}")
    else:
      # → 文件删除（逐个）
      strm_entries = [e for e in group.entries if not e.is_directory]
      logger.info(f"检测到 {len(strm_entries)} 个STRM文件删除: {parent_dir}")
      for entry in strm_entries:
        task = DeletionTask(task_type="file", is_music=entry.is_music, file_path=entry.path, ...)
        deletion_queue.append(task)

  batch.clear()
  process_deletion_queue()
```

### 7.4 防雪崩保护

```
flood_records: List[FloodRecord] = []
_flood_triggered: bool = False    # 熔断状态标记

check_flood_protection(file_count: int) → bool:
  """返回 True 表示触发熔断"""

  if not flood_protection_enabled or _flood_triggered:
    return False

  now = datetime.now()
  cutoff = now.timestamp() - flood_window_seconds

  # 清理过期记录
  flood_records = [r for r in flood_records if r.timestamp.timestamp() > cutoff]

  # 累加窗口内总数
  window_total = sum(r.file_count for r in flood_records) + file_count

  if window_total >= flood_threshold:
    logger.warning(
      f"防雪崩保护触发！{flood_window_seconds}秒内删除 {window_total} 个文件，"
      f"超过阈值 {flood_threshold}，插件已自动停用"
    )

    if notify:
      self.post_message(
        mtype=NotificationType.SiteMessage,
        title="⚠️ StrmCleaner 防雪崩保护触发 - 插件已自动停用",
        text=(
          f"检测到短时间内大量STRM文件删除事件，"
          f"可能为本地存储异常（磁盘故障、意外卸载等）。\n\n"
          f"时间窗口: {flood_window_seconds}s\n"
          f"删除数量: {window_total} 个\n"
          f"触发阈值: {flood_threshold} 个\n\n"
          f"插件已自动停用，所有云盘删除操作已阻止。\n"
          f"请检查本地存储状态，确认正常后手动重新启用插件。"
        ),
      )

    return True

  return False

# 在 process_deletion_queue 成功执行后调用:
record_flood_event(file_count):
  if flood_protection_enabled:
    flood_records.append(FloodRecord(datetime.now(), file_count))
```

**熔断后发生了什么**：

1. `batch` 和 `deletion_queue` 全部清空
2. 定时器取消
3. `stop_service()` 被调用，停止所有 watchdog observer
4. `_enabled = False`，插件逻辑标记为停用
5. `_flood_triggered = True`，所有新入站删除事件被直接丢弃
6. `update_config({"enabled": False})` 将停用状态持久化到配置
7. 发送告警通知

**用户恢复流程**：

1. 收到告警通知 → 检查本地存储状态（磁盘是否正常、挂载是否丢失等）
2. 确认存储正常后，进入 MoviePilot 插件管理页面
3. 将 StrmCleaner 插件的 `enabled` 重新设为 `true` → 触发 `init_plugin` 重新初始化
4. `_flood_triggered` 重置为 `False`，`flood_records` 清空
5. 手动处理云盘上因断电/故障未能同步删除的残留文件

### 7.5 视频 STRM 文件删除

```
execute_video_file_deletion(task):
  1. 验证 STRM 文件是否被重新创建
     if task.file_path.exists(): skip

  2. 从路径映射获取云盘路径
     storage_type, cloud_path = from_mapping(task.file_path)
     if not cloud_path: return

  3. 在云盘中查找对应视频文件
     parent_dir = Path(cloud_path).parent
     列出 parent_dir 文件
     base_name = Path(cloud_path).name
     found = [f for f in files
              if f.name.startswith(base_name)
              and f".{f.extension.lower()}" in RMT_MEDIAEXT]
     if not found:
       logger.info(f"未找到云盘视频文件: {cloud_path}")
       return

  4. 删除云盘视频文件
     StorageChain.delete_file(found[0])

  5. 删除元数据文件（如果启用 delete_metadata）
     for f in list_files(parent_dir):
       # 同名前缀匹配
       if f.stem.startswith(Path(cloud_path).stem):
         if f.suffix.lower() in METADATA_EXTENSIONS:
           StorageChain.delete_file(f)
         if f.name == f"{base_name}-mediainfo.json":
           StorageChain.delete_file(f)
     # 注意: 不删除 poster.jpg/backdrop.jpg 等目录级资产
     #       这些只在文件夹删除时随目录整体清理

  6. 删除转移记录（如果启用 delete_history）
     hist = TransferHistoryOper.get_by_dest(cloud_path)
     if hist: delete

日志示例:
  [INFO] 删除云盘视频: [openlist] /cloud/anime/将夜/S01/将夜 S01E01.mkv
  [INFO] 已清理元数据文件: 3 个 (.nfo, -thumb.jpg, -mediainfo.json)
```

### 7.6 音乐 STRM 文件删除

```
execute_music_file_deletion(task):
  1. 验证 STRM 文件是否被重新创建
     if task.file_path.exists(): skip

  2. 从路径映射获取云盘路径
     storage_type, cloud_path = from_mapping(task.file_path)
     if not cloud_path: return

  3. 在云盘中查找所有匹配的音乐/歌词文件
     parent_dir = Path(cloud_path).parent
     base_stem = Path(cloud_path).stem    # ★ 使用 stem 而非 name，修复歌词匹配问题
     列出 parent_dir 文件
     matched = [f for f in files
                if f.name.startswith(base_stem)     # "01 Song" 匹配 "01 Song.flac", "01 Song.lrc"
                and (ext in MUSIC_EXTENSIONS or ext in LYRICS_EXTENSIONS)]

     if not matched:
       logger.info(f"未找到云盘音乐文件: {cloud_path}")
       return

  4. 逐个删除匹配文件
     deleted = 0
     for item in matched:
       if StorageChain.delete_file(item):
         deleted += 1

  5. 删除转移记录（如果启用）
     对每个云盘文件路径调用 delete_history_by_dest

日志示例:
  [INFO] 音乐删除: [openlist] /cloud/music/Artist/Album/01 Song.flac
  [INFO] 已删除云盘文件: 2 个 (01 Song.flac, 01 Song.lrc)
```

### 7.7 文件夹删除（视频/音乐通用）

```
execute_folder_deletion(task):
  1. 验证 STRM 文件夹是否被重新创建
     if task.folder_path.exists(): skip

  2. 从路径映射获取云盘文件夹路径
     storage_type, cloud_folder_path = from_folder_mapping(task.folder_path)
     if not cloud_folder_path: return

  3. 删除云盘文件夹（整体删除，含其中所有元数据文件）
     if storage_type == "openlist" and api_configured:
       # OpenList API: POST /api/fs/remove
       parent = str(Path(cloud_folder_path).parent)
       name = Path(cloud_folder_path).name
       POST {api_url}/api/fs/remove
       body: {"dir": parent, "names": [name]}
     else:
       # StorageChain
       folder_item = FileItem(storage=storage_type, path=cloud_folder_path, type="dir")
       StorageChain.delete_file(folder_item)

  4. 同步删除本地映射目录（如果配置了 openlistlocal）
     if task.local_storage_type == "openlistlocal" and api_configured:
       OpenList API 删除本地目录
     elif task.local_storage_type:
       StorageChain 删除本地目录

日志示例:
  [INFO] 检测到文件夹删除: /strm/anime/将夜/S01 (14个STRM文件)
  [INFO] 已删除云盘文件夹: [openlist] /cloud/anime/将夜/S01
  [INFO] 同步删除本地目录: [openlistlocal] /mnt/local/anime/将夜/S01
```

### 7.8 openlist 本地路径同步删除

```
# 嵌入在 execute_folder_deletion 流程中的第 4 步

同步逻辑:
  1. 根据云盘文件夹路径反向查找 openlistlocal 映射
     _get_mapped_local_details_from_storage_path("openlist", "/cloud/anime/将夜/S01")
     → 遍历所有 path_mappings 中的 openlist 行
     → 匹配云盘路径前缀 → 计算相对路径
     → 拼接本地前缀 + 相对路径
     → 返回 ("openlistlocal", "/mnt/local/anime/将夜/S01")

  2. 调用 OpenList API 删除本地目录
     POST {api_url}/api/fs/remove
     body: { "dir": "/mnt/local/anime/将夜", "names": ["S01"] }

  3. 日志: "同步删除本地目录: [openlistlocal] /mnt/local/anime/将夜/S01"
```

---

## 八、元数据文件扩展名清单

```python
METADATA_EXTENSIONS = [
    # 元数据
    ".nfo", ".xml", ".json",
    # 图片
    ".jpg", ".jpeg", ".png", ".webp", ".tbn", ".fanart", ".gif", ".bmp",
    # 字幕
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".pgs", ".smi", ".rt", ".sbv",
]

# 额外命名模式匹配（文件删除时）
METADATA_PATTERNS = [
    "{name}-mediainfo.json",    # Emby/Jellyfin mediainfo
    "{name}-thumb.jpg",         # 缩略图
]
```

> **改动说明**：原插件不含 `.json`，导致 `-mediainfo.json` 不会被清理。新插件新增 `.json` + 额外命名模式匹配。

---

## 九、延迟删除与批量通知

### 9.1 滑动窗口

```
event → add_to_batch → cancel_timer → start_timer(delay_seconds)
event → add_to_batch → cancel_timer → start_timer(delay_seconds)
...
timer fires → process_batch
              ├── 防雪崩检查（触发则停用插件）
              ├── 文件/文件夹判定
              └── process_deletion_queue → send_batch_notification
```

### 9.2 批量通知格式

```
🧹 STRM文件清理 - 批量处理 3 个

📁 文件夹: 1 个
  └─ 将夜/S01 → [openlist] /cloud/anime/将夜/S01

📺 视频文件: 2 个
  └─ 电影A.strm → [openlist] /cloud/movie/电影A.mkv
  └─ 电影B.strm → [local] /media/movie/电影B.mkv

🎵 音乐文件: 1 个
  └─ 01 Song.flac.strm → [openlist] 2 个关联文件

🖼️ 已清理元数据文件 5 个
📝 已清理转移记录

⏰ 延迟删除完成 (240秒)
```

### 9.3 立即删除模式

```
event → add_to_batch → short_timer(3s) → process_batch → execute immediately → notify
```

使用固定 3 秒窗口做文件/文件夹判定后立即执行删除。

---

## 十、服务停止 (`stop_service`)

```
stop_service():
  1. 停止所有 watchdog observer
     observer.stop(); observer.join()
  2. 取消延迟删除定时器
  3. 处理 batch 中剩余事件
     if batch not empty: process_batch()   # 仍会经过防雪崩检查
  4. 处理 deletion_queue 中剩余任务
     for task in queue: execute_deletion(task)
  5. 清理状态
```

> **注意**：`stop_service` 在两种场景下被调用：(1) 插件正常关闭/重载；(2) 防雪崩触发后自动停用。两种场景都会善后处理 batch 和 queue。

---

## 十一、补充考虑

### 11.1 `.json` 作为元数据扩展名

原插件硬编码排除 `.json`，导致 Emby 生成的 `-mediainfo.json` 文件遗漏。新插件包含 `.json` 但匹配时增加 `-mediainfo.json` 命名模式过滤，防止误删其他 JSON 文件。

### 11.2 文件删除 vs 目录级资产

文件删除模式只删**同名前缀**的元数据文件，不删 `poster.jpg`、`backdrop.jpg` 等目录级资产。这些只在文件夹删除时随整个目录一起清理。

### 11.3 音乐歌词匹配修复

原插件用 `Path(base_path).name`（如 `01 Song.flac`）做前缀匹配，歌词文件 `01 Song.lrc` 前缀不匹配。**修复**：音乐模式改用 `Path(base_path).stem`（`01 Song`）做前缀匹配。

### 11.4 多个路径映射前缀重叠

如果同时配置 `/strm/video` 和 `/strm/video/anime`，某个 STRM 文件会匹配到两个。应遵循**最长前缀优先**原则选择映射。

### 11.5 批量处理部分失败容错

文件夹删除中个别云盘操作失败不中断整个流程，收集成功/失败结果统一汇总到通知。

### 11.6 防雪崩保护的「误触发」风险

正常场景：用户手动删除一个包含 60 集的电视剧文件夹（配置阈值 50）→ 触发熔断 → 误拦截。

**缓解措施**：
- 默认阈值设为保守值（如 100），用户根据实际情况调整
- **文件夹删除不计入防雪崩计数**（因为文件夹删除是一次性操作，不是异常信号）
- 只对**零散的文件删除**进行雪崩计数

实现：

```
check_flood_protection():
  # 只统计来自"文件删除判定"的条目
  file_deletion_count = sum(
    len([e for e in g.entries if not e.is_directory])
    for g in batch.values()
    if not (g.has_folder_event or not os.path.exists(g.parent_dir))
  )
  # ↑ 排除那些最终会被判定为文件夹删除的条目
```

### 11.7 防雪崩后重新启用插件

```
# init_plugin 入口处重置熔断状态:
self._flood_triggered = False
self.flood_records = []

# 这样用户手动重新勾选 enabled 时，插件恢复正常
```

### 11.8 延迟删除期间文件被重新创建

与原插件保持一致：延迟到期后检查 `file_path.exists()` 或 `folder_path.exists()`，如果重新存在则跳过删除。

---

## 十二、目录文件结构

```
plugins.v2/strmcleaner/
├── __init__.py          # 全部代码
└── README.md            # 用户使用说明
```
