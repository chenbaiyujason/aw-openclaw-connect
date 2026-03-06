---
name: aw-openclaw-connect
description: Query and export cleaned ActivityWatch events through the formal `aw-connect` CLI. Use when an agent needs to inspect recent user activity, sync what the user was doing after a pause, recover what happened during an absence window, or turn ActivityWatch logs into natural-language memory entries. For OpenClaw-style agents, use this proactively after a gap of 30+ minutes, when the user asks what happened during a time range, or when periodic memory backfill is due.
---

# AW OpenClaw Connect Skill

## 目的
通过 ActivityWatch 获取并导出清洗后的用户活动记录。

正式入口只有 `aw-connect` CLI：

- 一律通过 CLI 调用
- `query` 和 `export` 使用同一套过滤条件
- `query` 输出到标准输出
- `export` 把同样结果写入文件
- `watcher family` 是稳定语义层，优先兼容归类，不要求 agent 记住底层 bucket 名

## 安装

```bash
python -m pip install -e .
aw-connect --help
```

## 常用命令

```bash
aw-connect devices
aw-connect watchers
aw-connect watchers --device macbook
aw-connect query --minutes 15
aw-connect export --minutes 60
aw-connect export --start 2026-03-06T10:00:00Z --end 2026-03-06T11:00:00Z --device macbook --watcher web --output logs/session.csv
```

## 什么时候用

- 主人和你离开超过 30 分钟后再次对话
- 主人主动问“我刚刚 / 某段时间做了什么”
- 你需要补齐一段缺失上下文
- 你想确认主人离开期间是否继续推进当前任务
- 你需要把近期活动落回记忆文件，而当前记忆明显缺失

默认工作流：

1. 先检查记忆里有没有这段时间的记录
2. 有就直接用，不重复抓
3. 没有就抓 log
4. 按后文规则理解、清洗、总结
5. 如有长期记忆职责，再写回记忆文件

如果主人指定时间段：

1. 明确时间范围
2. 获取这段时间的 log
3. 清洗并总结
4. 如有长期记忆职责，再落回记忆

## 查询规则

- `--device` 按逻辑设备名过滤
- `--watcher` 按 watcher family 过滤，例如 `window`、`web`、`vscode`
- watcher family 过滤会自动包含对应的 synced bucket
- agent 不需要关心底层 bucket 名里的 `synced-from-*`
- `aw-watcher-cursor` 这类拆分来源统一兼容到 `vscode`
- `vscode` family 兼容文件活动、Agent 生命周期、Git commit 归档等语义
- 文件活动会保留 `activityKind`，例如 `dwell` / `edit`
- 只想快速补看近期活动时，优先只给时间条件，例如 `--minutes 15`

## 输出格式

CSV 由两部分组成：

- 头部：`#` 开头的 meta 行
- 正文：`o(s),d(s),dev,w,sub,items`

关键字段：

- `# start` / `# end`：查询范围
- `# afk`：是否应用 AFK 清洗
- `# dm`：设备短码映射
- `# ws`：本次结果出现的 watcher family
- `# ues`：跨设备并集后的有效时长，不是把每台设备时长直接相加
- `o(s)`：相对开始时间的偏移秒
- `d(s)`：事件段时长
- `dev`：事件来源设备，不代表这段时间只有这台设备重要
- `w`：watcher family
- `sub`：主题，例如应用名、项目名、域名
- `items`：聚合后的详细内容

`vscode` 结果约定：

- `w` 保持稳定的 `vscode`
- `sub` 通常是项目标识
- `items` 对文件活动尽量写成 `文件路径 [activityKind]:时长`
- 项目根明确时尽量写相对路径
- 项目根不明确时保留绝对路径，避免误截断

## 读 log 的流程

1. 先读 meta，确认时间范围、AFK 清洗、涉及设备和 watcher
2. 按时间顺序理解事件段，不要逐行机械复述
3. 把相邻且语义相近的事件合并成自然语言片段
4. 再决定是否写回记忆文件

目标不是复述字段，而是总结：

- 在什么时间
- 主人用什么设备
- 为了什么目的
- 做了什么
- 做到什么阶段

写法应接近：

- `14:10-14:35，主人在 A 设备上主要在 Cursor 里修改 aw-watcher-cursor，期间查看过 GitHub/文档，像是在推进 ActivityWatch 相关改动。`
- `19:40-19:55，没有明显活动。`
- `21:05-21:30，主人切到 B 设备后继续处理前面同一件事，并出现了 commit，说明已经完成并提交了一轮修改。`

## 如何判断主线

默认优先级：

- `commit > agent > edit > dwell > web/window`

总原则：

- 结果信号强于过程信号
- 过程信号强于背景信号
- `web` 和 `window` 多数用于补上下文，不要喧宾夺主
- 本地 AW 页面通常只表示主人在看时间记录，不是任务本身
- 如果多条事件指向同一目标，可以合并成一段自然语义
- 如果一段时间确实没有有效活动，要显式写出来

高价值信号：

- 明确的 `commit`
- 明确的 `edit`
- 和当前代码上下文强相关的 `agent`

低价值或背景信号：

- 只显示应用名的 `window`
- 本地统计页、设置页、监控页
- 只有 `dwell` 没有后续动作的停留记录

## 多设备综合判断

多设备同时出现时，先把它们看成“同一个人的并行活动”，不要按设备逐条记流水。

底线：

- `# ues` 是跨设备并集时长，重叠时间不能直接累加
- `dev` 只说明事件来源设备，不等于只有这台设备重要

判断步骤：

1. 先按时间对齐重叠片段
2. 先找 `commit`、`edit`、强相关 `agent`
3. 把强信号所在设备视为主线
4. 其他设备再区分为辅助信息还是背景噪音
5. 最后写成“主线 + 辅助 + 背景”
- 多台设备主题一致：通常合并成同一任务
- 多台设备都出现强信号：可以写成“跨设备并行推进”或“同时处理两条任务”
- 多台设备主题明显无关：直接承认主人在分心或并行做多件事，不要强行合成同一目标

写回时优先写“人”的状态，不要写“设备列表”：

- 好的写法：`20:10-20:40，主人主线在 A 设备上改 aw-openclaw-connect，同时用 B 设备查 ActivityWatch 文档；C 设备有挂机游戏，但看起来不是这一轮任务的核心。`
- 不好的写法：`20:10-20:40，A 设备 edit，B 设备 web，C 设备 window。`

空白时间也要记：

- `16:20-17:10，没有明显活动。`

## Watcher 速查

### `afk`

- `status` 常见值是 `not-afk` 或 `afk`
- `afk` 更像清洗边界，不像行为内容
- 如果结果已经启用 AFK 清洗，通常不用再把它当行为线索
- 它最适合回答“这段时间主人是不是还在电脑前”

### `window`

- 常见字段：`app`、`title`
- 适合判断主人大致在用什么应用
- 对 `Cursor` / `Code` / 浏览器这类壳子窗口，优先当补充上下文
- 如果同时有更具体的 `vscode` 或 `web`，优先相信更具体的记录
- 单看 `window`，不要轻易推断主人具体完成了什么

### `web`

- 常见字段：`url`、`title`
- 导出后常见表现：`sub=域名`，`items=更具体 URL`
- `web` 很适合补上下文，例如文档、PR、issue、搜索、后台页面
- `web` 本身不等于工作成果
- `localhost:5600` / `127.0.0.1:5600` 通常表示主人在看自己的时间记录或本地统计
- 本地面板、空白页、设置页、监控页一般不要当成“主人真正完成的事情”

### `vscode`

- 常见语义：文件活动、`activityKind`、Agent 生命周期、Git commit
- `sub` 通常是项目标识
- `items` 对文件活动通常是 `相对路径 [activityKind]`
- `fileActivity [dwell]` 表示在看，不等于在改
- `fileActivity [edit]` 表示明确在改
- `agent` 表示在借助 Cursor agent 推进
- `commit` 最接近“完成了一轮结果”

## 如何落回记忆文件

默认写回目标：

- 当天或对应日期的 `memory/YYYY-MM-DD.md`
- 如需日终汇总，再补到长期记忆文件，例如 `MEMORY.md`
- 原始或清洗后的 CSV 文件也保存在 `memory/` 下，方便回查

CSV 默认存储位置：

- `memory/activitywatch-logs/YYYY-MM-DD/`

文件名建议带时间范围和是否清洗，例如：

- `memory/activitywatch-logs/2026-03-06/2026-03-06T14-00_to_2026-03-06T16-00_cleaned.csv`
- `memory/activitywatch-logs/2026-03-06/2026-03-06T18-30_to_2026-03-06T21-00_cleaned.csv`

同一天补抓多次时不要覆盖旧文件。

每次有新的 CSV 落回时，都要在对应日期的 `memory/YYYY-MM-DD.md` 顶部维护一个索引块：

```md
## ActivityWatch Logs

- `2026-03-06T14-00_to_2026-03-06T16-00_cleaned.csv`
- `2026-03-06T18-30_to_2026-03-06T21-00_cleaned.csv`
```

写回规则：

- 先写时间范围，再写自然语义结论
- 一次落回只补之前缺失的时间段，不要反复全量重写
- 如果同一天后续又拿到了新设备的新记录，可以更新已有条目
- 新信息优先补充，不要把之前已确认的内容删没

例如：

- 白天先拿到 A 设备的 log，发现 `18:00-20:00` 没有明显活动
- 晚上 B 设备后来同步出 `19:10-19:40` 的真实记录
- 这时应回到同一天的记忆文件，把原先条目更新成更准确的版本，并更新顶部 CSV 索引

默认只优先补最近 24 小时：

- 主人明确要求时，再去补更早的数据
- 如果只是例行记忆回填，一般不用主动补太早历史

## 如何结合已有上下文

不要把 log 当成孤立流水账。要主动判断：

- 你现在正在做的事，和 log 里出现的活动是不是同一件事
- 记忆里主人之前提到过的事，和 log 里后续动作是不是连得上
- 主人离开前让你做 A，离开期间主人自己是不是继续把 A 推进到了 B

如果能判断出相关性，不要只记“主人做了 B”，而要更新成完整状态理解，例如：

- 主人先和你讨论了 A
- 离开期间主人自己继续推进，做到了 B
- 所以主人这次回来时，你应当把当前状态理解成 C，而不是还停留在 A

推断时优先看：

- 事件前后顺序
- 切换关系
- 一段时间内主题怎么递进
- 网页、代码、agent、commit 之间是不是在互相支撑
- 不要只看点状事件；很多真实意图是通过“先看什么，再切去哪里，最后回到哪里”暴露出来的

常见模式：

- 先出现 `web` 文档页，再出现 `vscode [edit]`：通常是先查资料，再回代码实现
- 先出现 `vscode [dwell]`，再切到搜索页 / issue / PR / 文档，最后回到 `vscode [edit]`：通常是先卡住，再查资料，再继续推进
- 不要只记“打开了哪个 URL”，要判断它是在查 API、看历史实现、还是继续探索，以及它有没有支撑后续 `edit` / `commit`
- URL 不只是页面名，也可能是线索来源；要区分哪些页面只是路过，哪些页面像是启发了后续动作
- 官方文档 URL：通常是在查 API、配置、行为定义
- GitHub PR / issue：通常是在看历史上下文、实现方式、bug 讨论
- 搜索结果页：通常说明主人还在探索

主动推测，但表达要区分强弱：

- 证据很强时，可以直接说“主人在推进 X”
- 证据中等时，可以说“看起来像是在排查 / 对照 / 查资料 / 为某个改动做准备”
- 证据较弱时，可以说“可能是在……”或“像是在……”

## OpenClaw 风格 agent 的额外约定

- 当主人和你离开超过 30 分钟后再次对话，先检查记忆里是不是已经有这段时间的记录
- 这里的“记忆”既包括当天的 `memory/YYYY-MM-DD.md`，也包括其他 session 可能已经提前写入的内容
- 如果已经有，就直接用，不重复抓
- 如果没有，就主动获取从上一次离开到这次回来的 log，并落回记忆

- 主人主动说“帮我了解某段时间做了什么”时：
- 优先按主人给的时间范围取 log
- 如果主人只表达了“离开期间”但没给具体时间，就补抓你不知晓的那段缺口

- 如果你有定时器或 heartbeat，建议每 2 小时检查一次是否需要补记忆：
- 先判断现在是不是主人的清醒时间
- 默认可按早上 8 点到晚上 12 点理解
- 也可以结合 `USER.md`、已有记忆和主人最近作息自行判断
- 如果不是清醒时间，这次检查直接跳过，不输出任何打扰
- 如果是在清醒时间，检查最近是否已经有对应时间段的记忆落回；如果没有，就补抓缺失时间段的 log 并落回当天记忆

- 如果触发落在当天晚上 12 点附近，再额外做日终整理：
- 读取当天所有相关记忆
- 结合当天补回的 log 和已有上下文
- 用简单几句话写回长期记忆文件
- 重点写主线任务、明确结果、仍在中间阶段的事，不要写成流水账

## 行为约定

- 系统只负责返回清洗后的事件
- 不再提供 `user_view` 之类的二次推断视角
- 用户在做什么、哪些事件应当归并、哪些行为和当前对话有关，由 agent 自己判断
- 当上游 watcher 新增 bucket、拆分轨道或调整字段时，优先继续通过现有 watcher family 语义消费结果
