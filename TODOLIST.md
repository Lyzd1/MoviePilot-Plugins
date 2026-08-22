# TODOLIST — 插件「站点分享率上传限速」(SiteRatioLimiter)

> 供其他程序 / Agent 消费的机器可读任务清单。
> 解析约定：
> - 每行一个任务：`- [ ]`（未完成）或 `- [x]`（已完成），后跟任务编号 `[T编号]` 与描述；
> - 行尾显式状态标记 `@pending` / `@in_progress` / `@done` / `@blocked`（与复选框保持一致）；
> - 目标版本 v1.1.0（`package.v2.json` 的 version 与插件类 `plugin_version` 必须一致）。

## 任务清单（v1.1.0 迭代）

- [x] [T10] 移除「立即运行一次」按钮与相关调度，运行方式改为纯事件驱动：SiteRefreshed（site_id=*）触发获取各站点分享率并判断档位是否改变 @done
- [x] [T11] 分享率读取改为 SiteOper.get_userdata_by_date（按站点名，今天数据优先回退昨天），与流量管理/魔力兑换插件一致；面板增加数据日期列便于核对刷新是否生效 @done
- [x] [T12] 详情面板删除种子明细表，仅保留站点状态表（站点/分享率/数据日期/阈值/档位/已限速种子数） @done
- [x] [T13] 版本号更新 v1.0.0 -> v1.1.0（插件类与 package.v2.json 一致 + history 记录）、README/TODOLIST 同步 @done

## 任务清单（v1.0.0 已发布）

- [x] [T1] 插件骨架与元数据：`plugins.v2/siteratiolimiter/__init__.py` 类、plugin_* 字段、get_state/get_command/get_api @done
- [x] [T2] 配置与表单：get_form（启用/立即运行/通知渠道/下载器(qb)/站点筛选/全局分享率下限、上限/上传速度/站点单独阈值文本）+ 规范化工具 @done
- [x] [T3] 基础设施复用：DownloaderHelper 下载器连接、站点域名映射构建、站点识别链（下载历史/tracker域名/标签/分类）、SiteUserData 分享率快照读取 @done
- [x] [T4] 下载种子事件（DownloadAdded）：从插件内部站点状态读取档位，高于上限则直接限速该种子，否则不操作（不再实时查询 MoviePilot 站点数据） @done
- [x] [T5] 站点分享率刷新事件（SiteRefreshed，site_id=="*" 全站刷新完成）：刷新内部状态；低于下限取消该站种子限速；高于上限批量补限速；档位变化时才通知（过低/已达上限） @done
- [x] [T6] 限速/恢复统一封装：_limited/_restore 归属集合、跨会话持久化、停用/卸载恢复不限速、兜底恢复重试、状态变化通知 @done
- [x] [T7] 详情面板 get_page：每个配置站点的分享率 + 相对上下限的档位状态（下载时判定依据）+ 种子限速标识表 @done
- [x] [T8] 注册 `package.v2.json` 条目（末尾追加）+ README + py_compile/JSON 校验 + 代码自审 @done
- [x] [T9] 版本发布：版本号一致性复核（1.0.0），git commit `085a28e` 并 push 到 github（origin main） @done

## 进度统计

完成：13 / 13（v1.0.0 已发布，v1.1.0 待发布）
更新时间：2026-08-22