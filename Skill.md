---
name: aw-openclaw-connect
description: Query and export cleaned ActivityWatch events through the formal `aw-connect` CLI. Use when an agent needs to inspect recent user activity, sync what the user was doing after a pause, or export agent-readable timelines with time, device, and watcher filters.
---

# AW OpenClaw Connect Skill

## 目的

这个仓库的正式入口是 `aw-connect` CLI。

给其他 agent 的约定是：

- 统一通过正式 CLI 调用
- `query` 和 `export` 使用同一套过滤条件
- `query` 输出到标准输出
- `export` 把同样的结果写入文件

## 安装

在项目根目录执行：

```bash
python -m pip install -e .
```

安装后可直接使用：

```bash
aw-connect --help
```

## 核心命令

### 1. 查看设备

```bash
aw-connect devices
```

输出逻辑设备到 watcher family 的映射。

### 2. 查看 watcher

```bash
aw-connect watchers
```

```bash
aw-connect watchers --device macbook
```

## 查询与导出

`query` 和 `export` 的过滤条件完全一致，结果语义也一致。

区别只有：

- `query`：打印到标准输出
- `export`：写入文件

### 默认建议

当 agent 只是想补看“用户最近在做什么”时，默认优先只传时间条件。

常用例子：

```bash
aw-connect query --minutes 15
```

```bash
aw-connect export --minutes 60
```

### 完整过滤

如需更细粒度条件，可以继续叠加设备、watcher、绝对时间范围：

```bash
aw-connect query --start 2026-03-06T10:00:00Z --end 2026-03-06T11:00:00Z --device macbook --watcher window
```

```bash
aw-connect export --start 2026-03-06T10:00:00Z --end 2026-03-06T11:00:00Z --device macbook --watcher web --output logs/macbook-web.csv
```

## 过滤规则

- `--device` 按逻辑设备名过滤
- `--watcher` 按 watcher family 过滤，例如 `window`、`web`、`vscode`
- watcher family 过滤会自动包含对应的 synced bucket
- agent 不需要关心底层 bucket 名里的 `synced-from-*`

## 输出格式

`query` 默认输出、以及 `export` 写入的文件，都是 agent 友好的 CSV：

1. 头部是 `#` 开头的 meta 行
2. 正文列为 `o(s),d(s),dev,w,sub,items`

重点字段：

- `# start` / `# end`：查询时间范围
- `# afk`：是否应用 AFK 清洗
- `# dm`：设备短码映射
- `# ws`：结果中出现的 watcher family
- `# ues`：跨设备并集后的有效时长秒数
- `o(s)`：相对开始时间的偏移秒
- `d(s)`：该事件段实际时长
- `dev`：设备短码
- `w`：watcher family
- `sub`：主题，例如应用名或域名
- `items`：聚合后的详细内容

## 行为约定

- 系统只负责返回清洗后的事件
- 不再提供 `user_view` 之类的二次推断视角
- 用户在做什么、哪些事件应当归并、哪些行为和当前对话有关，由 agent 自己判断
