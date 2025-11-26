# 插件被动触发执行机制文档

本文档旨在说明如何实现类似于 `bonusexchangeplugin` 中的 `exchange_monitor` 的被动触发机制。该机制允许插件在特定事件发生后自动执行其核心功能。

## 核心概念：事件驱动

插件的被动执行依赖于系统的事件管理器 (`eventmanager`)。当系统中发生特定事件时（例如，所有站点的数据刷新完成），事件管理器会通知所有注册了该事件的监听器（函数），并触发它们的执行。

在 `bonusexchangeplugin` 中，核心的触发器是 `EventType.SiteRefreshed` 事件。

## `exchange_monitor` 函数解析

`exchange_monitor` 是 `bonusexchangeplugin` 响应站点数据刷新事件的核心函数。

### 1. 事件注册

通过使用 `@eventmanager.register(EventType.SiteRefreshed)` 装饰器，`exchange_monitor` 函数被注册为 `SiteRefreshed` 事件的监听器。

```python
@eventmanager.register(EventType.SiteRefreshed)
def exchange_monitor(self, event: Event = None):
    """
    魔力兑换监控服务
    """
    # ... 函数体 ...
```

### 2. 触发条件

`SiteRefreshed` 事件在每个站点数据刷新后都会被触发一次，并在所有站点都刷新完成后，会额外触发一次，并携带一个特殊的标志。插件需要通过检查事件数据来判断是否是“全局刷新完成”的信号。

```python
if event:
    event_data = event.event_data
    # 检查 event_data 是否存在，以及 site_id 是否为 "*",
    # "*" 表示所有站点的数据刷新已全部完成。
    if not event_data or event_data.get("site_id") != "*":
        return
    else:
        # 当所有站点都刷新完成后，才执行核心逻辑
        logger.info("站点数据刷新完成，立即运行一次魔力兑换助手服务")
```

### 3. 执行逻辑

一旦确认是全局刷新事件，函数将继续执行其核心任务：

- **获取锁 (`with lock:`)**: 防止并发执行导致数据冲突。
- **验证配置**: 确保插件配置正确。
- **获取数据 (`__get_site_statistics`)**: 从数据库或其他来源获取最新的站点统计数据。
- **执行核心业务 (`__monitor_sites`)**: 根据获取到的数据，执行插件的主要逻辑（如检查分享率、魔力值等）。
- **发送通知**: 将执行结果通过消息推送给用户。

## 如何在 `torrenttransfer` 插件中实现

你可以参考以下步骤在你的 `torrenttransfer` 插件中实现类似的功能：

1.  **定义一个监听函数**: 在你的插件主类中，创建一个函数，例如 `transfer_monitor`。
2.  **注册事件**: 使用 `@eventmanager.register(EventType.SiteRefreshed)` 装饰器来监听同一个全局刷新事件。
3.  **实现函数逻辑**:

    -   添加对 `event_data.get("site_id") != "*"` 的判断，确保仅在所有站点刷新完毕后才执行。
    -   在函数内部实现你的核心转移逻辑，例如：
        -   检查插件配置是否启用。
        -   从数据库或API获取需要转移的种子信息。
        -   根据最新的站点数据（分享率、空间等）判断是否满足转移条件。
        -   执行转移操作。
        -   记录日志并发送通知。

### 示例代码

```python
# 在你的 torrenttransfer 插件的 __init__.py 中

from app.core.event import Event, eventmanager
from app.schemas.types import EventType
from app.log import logger

class TorrentTransferPlugin(_PluginBase):
    # ... 其他插件属性 ...

    @eventmanager.register(EventType.SiteRefreshed)
    def transfer_monitor(self, event: Event = None):
        """
        种子自动转移监控服务
        """
        if not self.get_state(): # 检查插件是否启用
            return

        if event:
            event_data = event.event_data
            # 确保是所有站点刷新完成的信号
            if not event_data or event_data.get("site_id") != "*":
                return
            else:
                logger.info("站点数据刷新完成，开始执行种子转移任务...")

        # --- 在这里开始你的核心逻辑 ---
        # with lock: (如果需要，也使用锁来防止并发)
        try:
            # 1. 验证配置
            # 2. 获取需要处理的种子列表
            # 3. 遍历站点数据，判断转移条件
            # 4. 执行转移
            # 5. 发送结果通知
            logger.info("种子转移任务执行完毕。")
        except Exception as e:
            logger.error(f"种子转移任务执行失败：{e}")
            self.post_message(mtype=NotificationType.Plugin, title="种子转移助手", text=f"任务执行失败：{e}")

    # ... 其他插件方法 ...
```
