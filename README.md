# aw-openclaw-connect

本项目提供一个正式的 ActivityWatch 本地查询与导出 CLI：`aw-connect`。

它的定位不是临时脚本集合，而是：

- 发现逻辑设备与 watcher family
- 按时间、设备、watcher 查询清洗后的事件
- 把同一批结果导出为 agent 可读的 CSV

## 安装

在项目根目录执行：

```bash
python -m pip install -e .
```

安装后可直接使用：

```bash
aw-connect --help
```

## 常用命令

```bash
aw-connect devices
aw-connect watchers --device macbook
aw-connect query --minutes 15
aw-connect export --minutes 60
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
- `--apply-afk-cleanup` / `--no-afk-cleanup`

其中 `--watcher` 按 watcher family 工作，并会自动包含对应的 synced bucket。

## Agent 指南

给其他 agent 的正式调用说明放在根目录 [`Skill.md`](Skill.md)。

默认建议：

- 需要补看“用户最近在做什么”时，优先只传时间条件
- 有额外需求时，再叠加 `--device`、`--watcher` 或绝对时间范围
- 系统只负责返回清洗后的事件，行为归因与总结由 agent 自己完成

## 相关项目

- [ActivityWatch](https://github.com/ActivityWatch/activitywatch)
- [aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web)
- [aw-watcher-cursor](https://github.com/chenbaiyujason/aw-watcher-cursor)