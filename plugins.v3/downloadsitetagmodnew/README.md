> 本仓库 fork 自 [Seed680/MoviePilot-Plugins](https://github.com/Seed680/MoviePilot-Plugins)，在其 v2.0.0 基础上做了本地改造，长期自维护。

## 本地改造

- v2.1.0：**音乐统一分类**。MoviePilot 的音乐分类（专辑 / EP / 单曲 / 未分类）不再细分写入 qbit，音乐类型统一打「音乐」分类；影视仍按原有「二级分类」或「按路径分类」规则处理。已存在分类的种子不受影响（补全任务沿用「已有分类不覆盖」）。

# MoviePilot 插件远程组件示例

这是 MoviePilot 插件远程组件的示例项目，展示了如何正确配置和开发与主应用兼容的远程组件。本示例实现了三个标准组件：Page（详情页面）、Config（配置页面）和Dashboard（仪表板组件）。

## 1. 开发环境准备

### 安装依赖

```bash
npm install
# 或
yarn
```

### 开发模式运行

```bash
npm run dev
# 或
yarn dev
```

## 2. 项目结构

```
plugin-component/
├── src/
│   ├── components/
│   │   ├── Page.vue       # 插件详情页面组件
│   │   ├── Config.vue     # 插件配置页面组件
│   │   └── Dashboard.vue  # 插件仪表板组件
│   ├── App.vue            # 本地开发入口组件
│   └── main.js            # 本地开发入口文件
├── vite.config.js         # Vite和模块联邦配置
├── index.html             # 本地开发HTML入口
└── package.json           # 依赖配置
```

## 3. 开发指引

- [模块联邦开发指南](../../docs/module-federation-guide.md)
- [模块联邦问题排查指南](../../docs/federation-troubleshooting.md)。
