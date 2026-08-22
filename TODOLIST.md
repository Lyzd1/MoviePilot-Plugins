# TODOLIST — 插件「站点分享率上传限速」(SiteRatioLimiter)

> 供其他程序 / Agent 消费的机器可读任务清单。
> 解析约定：
> - 每行一个任务：`- [ ]`（未完成）或 `- [x]`（已完成），后跟任务编号 `[T编号]` 与描述；
> - 行尾显式状态标记 `@pending` / `@in_progress` / `@done` / `@blocked`（与复选框保持一致）；
> - 目标版本 v1.3.2（`package.v2.json` 的 version 与插件类 `plugin_version` 必须一致）。

## 任务清单（v1.3.2 迭代）

- [x] [T25] 修复兜底恢复重试误取消本应保持限速的种子：_apply_site_limits 对「已限速」种子也登记为限速中（与 QB上传限速 原版对齐），兜底重试只处理真正不再限速的种子；清理已删除种子的残留记录；版本 v1.3.2 并发布 @done

## 任务清单（v1.3.1 迭代）

- [x] [T24] 档位状态标签改为行为语义（🔻不限速保持/🔺限速保持/⏸中间区间保持现状），README 补充“状态列=滞回记忆的执行策略、分享率列=实时值”说明，版本 v1.3.1 并发布 @done

## 任务清单（v1.3.0 迭代）

- [x] [T19] 恢复分享率上限阈值并改为滞回档位状态机：<=下限→低于下限（取消限速并保持到>上限）；>上限→达到上限（恢复限速并保持到<=下限）；中间区间保持现状不调用限速接口（防波动） @done
- [x] [T20] 站点阈值恢复双值格式（站点=下限,上限），兼容旧版单值（上限继承全局）；表单/通知/面板/配置签名同步恢复上限 @done
- [x] [T21] 作者改为 Lyzd1（插件类与 package.v2.json） @done
- [x] [T22] 版本号 v1.2.0 -> v1.3.0（插件类/package.v2.json/description/history/README/TODOLIST 同步）、py_compile/JSON/版本一致性校验 @done
- [x] [T23] 发布：git commit `c8aabd8` 并 push 到 github（origin main） @done

## 任务清单（v1.2.0 迭代）

- [x] [T14] 档位改为两档：分享率<=下限为「低于下限」（取消限速），其余一律为「正常」（全部限速）；移除分享率上限配置（表单/解析/通知文案/面板同步更新，兼容旧版「站点=下限,上限」配置只取下限） @done
- [x] [T15] 修复修改配置后面板「已限速种子」显示 0/总数：统计改为动作执行后按下载器种子实际当前限速统计（含外部限速），并在每次刷新/保存配置时更新 @done
- [x] [T16] 避免保存配置时无意义的限速 API 调用：新增 config_signature 持久化签名，仅当上传限速大小、下限阈值（导致档位变化）、站点/下载器范围变化时才调用接口调整限速；否则只刷新状态与统计 @done
- [x] [T17] 版本号更新 v1.1.0 -> v1.2.0（插件类/package.v2.json/description/history/README/TODOLIST 同步）、py_compile/JSON/版本一致性校验 @done
- [x] [T18] 发布：git commit `21cabd4` 并 push 到 github（origin main） @done

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

完成：25 / 25（v1.0.0 ~ v1.3.1 已发布，v1.3.2 待发布）
更新时间：2026-08-22