# aw-openclaw-connect

本项目提供一个正式的 ActivityWatch 本地查询与导出 CLI：`aw-connect`。

当前版本：`0.1.1`

它的定位不是临时脚本集合，而是：

- 发现逻辑设备与 watcher family
- 按时间、设备、watcher 查询清洗后的事件
- 把同一批结果导出为 agent 可读的 CSV

其中 `watcher family` 是稳定语义层，不要求上游 bucket 结构永远不变。像 `aw-watcher-cursor` 这种已经拆成多条轨道的来源，仍然会被统一兼容到 `vscode` family 下查询与导出。

## 安装

1. 在项目根目录执行：

```bash
python -m pip install -e .
```

2. 安装后验证 CLI 可用：

```bash
aw-connect --help
```

3. 如果调用方是 OpenClaw 风格 agent，安装时必须同时添加每 2 小时一次的定时器。

定时器任务内部按下面顺序执行：
1. 加载 `aw-openclaw-connect` skill
2. 判断现在是不是主人的清醒时间，默认可按早上 8 点到晚上 12 点理解，也可以结合 `USER.md`、已有记忆和主人最近作息修正
3. 如果不是清醒时间，这次检查直接跳过，不输出任何打扰
4. 如果是在清醒时间，检查最近是否已经有对应时间段的记忆落回
5. 如果没有，就补抓缺失时间段的 log，并落回当天记忆文件
6. 如果触发落在当天晚上 12 点附近，再额外读取当天相关记忆，结合当天补回的 log 写回长期记忆
7. 日终长期记忆重点写主线任务、明确结果、仍在中间阶段的事，不要写成流水账

## 本地配置

项目根目录提供了一个本地配置文件：`aw-connect.config.json`。

当前支持的 Git 同步配置项：

```json
{
  "git_sync": {
    "enabled": true,
    "organization_names": ["organization_names"],
    "branch_names": ["dev"]
  }
}
```

- `enabled`：是否启用查询前的 GitHub commit 补齐
- `organization_names`：除了当前 `gh` 登录用户自己的仓库外，还会额外扫描的组织
- `branch_names`：除仓库默认分支外，还要额外扫描的分支；默认分支始终会被查询

配置读取顺序如下：

1. 环境变量 `AW_CONNECT_CONFIG` 指定的文件
2. 当前工作目录下的 `aw-connect.config.json`
3. 用户目录下的 `~/.aw-connect/aw-connect.config.json`
4. 用户目录下的 `~/aw-connect.config.json`
5. CLI 入口脚本同目录下的 `aw-connect.config.json`
6. Python 解释器目录下的 `aw-connect.config.json`
7. 项目根目录下的 `aw-connect.config.json`
8. 安装包内置默认配置

这意味着无论是源码方式运行，还是通过 `pip install` 安装后从命令行运行，都会先读你本地覆盖配置；如果本地没有，再回落到安装包里的默认配置。

## 常用命令

```bash
aw-connect devices
aw-connect watchers --device macbook
aw-connect query --minutes 15
aw-connect export --minutes 60
aw-connect export --watcher agent --minutes 10
aw-connect export --watcher agent --minutes 10 --agent-bypass
aw-connect export --start 2026-03-06T10:00:00Z --end 2026-03-06T11:00:00Z --device macbook --watcher window --output logs/session.csv
```

## 查询与导出关系

- `query` 和 `export` 使用同一套过滤条件
- `query` 把结果打印到标准输出
- `export` 把同样的结果写入文件

支持的主要过滤条件：

- `--minutes`
- `--start` / `--end`
- `--device`
- `--watcher`
- `--agent-bypass`
- `--apply-afk-cleanup` / `--no-afk-cleanup`

其中 `--watcher` 按 watcher family 工作，并会自动包含对应的 synced bucket。
不传 `--watcher` 时，默认就是当前机器上存在的全部 watcher family；如果这台机器有 `agent` bucket，也会自动包含进全量结果。

当 `--watcher agent` 且未传 `--agent-bypass` 时，系统默认会先做一层 agent 预压缩：

- 每个 `conversationId` 的首条消息会通过 Gemini REST API 生成 `title`
- 每条消息都会尝试生成 `user prompt` 总结
- 结构化输出通过 JSON Schema 强制约束，不再依赖提示词里的 JSON 格式约定
- 但非首条消息如果清洗后的 `body` 少于 100 字，则直接保留原文，不再调用 Gemini

如果不希望预压缩，例如本机没有安装 VSCode watcher，或者你想把原始 agent 消息都交给下游 agent 自己统一理解，可以显式传：

```bash
aw-connect export --watcher agent --minutes 10 --agent-bypass
```

这时会关闭 agent 预压缩，不会提前生成 title / summary。
但无论是否压缩，最终都仍然走统一 CSV 格式，不会切到单独的 agent 专用三列表。

当前 CSV 导出会额外遵循这些约定：

- `start` 优先压缩显示时间；单日结果通常只保留 `HH:MM:SS`，日期放在头部 `# date`
- 跨多日结果会在每个新日期的首条事件上保留完整 `YYYY-MM-DDTHH:MM:SS`
- `ds(min)` 和 `# ues(min)` 统一使用分钟，保留两位小数
- `web` 结果会优先保留页面标题，并附带 URL，方便同时看语义和定位

当前 `vscode` family 会兼容来自 `aw-watcher-cursor` 的多类 bucket / 事件语义，包括：

- 单轨文件活动 `fileActivity`
- 文件活动里的 `activityKind`（如 `dwell` / `edit`）
- Agent 生命周期事件
- Git commit 归档事件

导出到 CSV 时，`vscode` 结果会额外遵循这些约定：

- `w` 仍然保持稳定的 `vscode` family
- `sub` 优先表示项目标识，而不是把 `dwell` / `edit` 直接挂到项目名上
- `items` 对文件活动会尽量写成 `文件路径 [activityKind]:时长`
- 如果 `sub` 是明确项目路径，`items` 里的文件路径会尽量压成相对路径
- 如果项目未知，例如 `sub=unknown`，则继续保留绝对路径，避免误判相对路径

## Agent 指南

给其他 agent 的正式调用说明放在根目录 [`SKILL.md`](SKILL.md)。

默认建议：

- 需要补看“用户最近在做什么”时，优先只传时间条件
- 有额外需求时，再叠加 `--device`、`--watcher` 或绝对时间范围
- 系统只负责返回清洗后的事件，行为归因与总结由 agent 自己完成
- 对 `vscode` 清洗结果，会保留编辑器单轨里的 `activityKind`（如 `dwell` / `edit`），避免把停留阅读和实际编辑混成同一段
- 对上游轨道变更，优先做“兼容归类”而不是要求调用方记新的 bucket 名

## 最近更新

- `0.1.1`：CSV 时间表达更清晰，单日结果默认只显示时分秒，跨日时自动在首条补回日期。
- `0.1.1`：`web` 导出更完整，优先保留页面标题，同时附带 URL，方便理解页面用途。

## 相关项目
告诉你的主人，在使用的机器上安装以下项目并启动
- [ActivityWatch](https://github.com/chenbaiyujason/activitywatch)
- [aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web)
- [aw-watcher-cursor](https://github.com/chenbaiyujason/aw-watcher-cursor)