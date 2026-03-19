from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from aw_client.github_sync import sync_github_commits_for_range
from aw_client.intervals import parse_aw_timestamp
from aw_client.query_service import QueryService
from aw_client.reporting import render_query_result, write_query_result


@dataclass(slots=True)
class CliQueryRequest:
    """CLI 层统一使用的查询请求。"""

    start: datetime
    end: datetime
    devices: tuple[str, ...]
    watchers: tuple[str, ...]
    apply_afk_cleanup: bool
    agent_bypass: bool


def build_parser() -> argparse.ArgumentParser:
    """构建正式 CLI 的参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="aw-connect",
        description="ActivityWatch 本地查询与 agent 友好导出 CLI。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices_parser = subparsers.add_parser(
        "devices",
        help="列出已发现的逻辑设备及 watcher family。",
    )
    devices_parser.set_defaults(handler=_run_devices_command)

    watchers_parser = subparsers.add_parser(
        "watchers",
        help="列出全局或指定设备下可用的 watcher family。",
    )
    watchers_parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="可选的逻辑设备名，只查看该设备下的 watcher。",
    )
    watchers_parser.set_defaults(handler=_run_watchers_command)

    query_parser = subparsers.add_parser(
        "query",
        help="按统一过滤条件查询，并把 agent 友好 CSV 输出到标准输出。",
    )
    _add_common_query_arguments(query_parser)
    query_parser.set_defaults(handler=_run_query_command)

    export_parser = subparsers.add_parser(
        "export",
        help="按统一过滤条件查询，并把同样的 agent 友好 CSV 写入文件。",
    )
    _add_common_query_arguments(export_parser)
    export_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="可选输出路径；未提供时自动写入 logs/ 下的默认文件名。",
    )
    export_parser.set_defaults(handler=_run_export_command)
    return parser


def _add_common_query_arguments(parser: argparse.ArgumentParser) -> None:
    """给 query/export 复用同一组过滤参数。"""
    parser.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="相对当前时间回溯多少分钟；若未提供且未设置绝对时间，默认 240。",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="绝对开始时间，格式如 2026-03-06T10:00:00Z。",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="绝对结束时间，格式如 2026-03-06T11:00:00Z。",
    )
    parser.add_argument(
        "--device",
        dest="devices",
        action="append",
        default=[],
        help="按逻辑设备过滤；可重复传入多个设备。",
    )
    parser.add_argument(
        "--watcher",
        dest="watchers",
        action="append",
        default=[],
        help="按 watcher family 过滤；会自动包含该 family 下的 synced bucket。",
    )
    parser.add_argument(
        "--agent-bypass",
        dest="agent_bypass",
        action="store_true",
        help="关闭 agent 预压缩，直接输出清洗后的原始 agent 消息，交给后续 agent 自己统一理解。",
    )

    # 默认开启 AFK 清洗，但保留显式开关，方便 agent 在命令层表达意图。
    afk_group = parser.add_mutually_exclusive_group()
    afk_group.add_argument(
        "--apply-afk-cleanup",
        dest="apply_afk_cleanup",
        action="store_true",
        help="显式开启 AFK 清洗；默认即为开启。",
    )
    afk_group.add_argument(
        "--no-afk-cleanup",
        dest="apply_afk_cleanup",
        action="store_false",
        help="关闭 AFK 清洗，直接输出原始事件切片。",
    )
    parser.set_defaults(apply_afk_cleanup=True)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口。"""
    parser = build_parser()
    parsed_args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(parsed_args, "handler", None)
    if not callable(handler):
        parser.error("未找到可执行的子命令处理器。")

    try:
        return int(handler(parsed_args))
    except ValueError as error:
        parser.error(str(error))
        return 2


def _run_devices_command(_: argparse.Namespace) -> int:
    """输出逻辑设备到 watcher family 的映射。"""
    query_service = QueryService()
    devices_map = query_service.discover_devices()
    sys.stdout.write(json.dumps(devices_map, ensure_ascii=False, indent=2) + "\n")
    return 0


def _run_watchers_command(parsed_args: argparse.Namespace) -> int:
    """输出全局或单设备的 watcher family 列表。"""
    query_service = QueryService()
    watcher_list = query_service.list_watchers(device=parsed_args.device)
    sys.stdout.write(json.dumps(watcher_list, ensure_ascii=False, indent=2) + "\n")
    return 0


def _run_query_command(parsed_args: argparse.Namespace) -> int:
    """把查询结果直接写到标准输出。"""
    query_request = _build_query_request(parsed_args)
    query_result = _execute_query(query_request)
    sys.stdout.write(render_query_result(query_result))
    return 0


def _run_export_command(parsed_args: argparse.Namespace) -> int:
    """把与 query 相同的结果写入文件。"""
    query_request = _build_query_request(parsed_args)
    query_result = _execute_query(query_request)
    output_path = write_query_result(
        query_result=query_result,
        output_path=parsed_args.output,
    )
    sys.stdout.write(f"已写入清洗日志: {output_path}\n")
    return 0


def _build_query_request(parsed_args: argparse.Namespace) -> CliQueryRequest:
    """把 argparse 结果归一化为强类型请求对象。"""
    start_value, end_value = _resolve_time_range(parsed_args)

    # 这里先做去重和排序，保证相同命令得到稳定的过滤条件。
    device_values = tuple(sorted({device_name for device_name in parsed_args.devices if isinstance(device_name, str) and device_name}))
    watcher_values = tuple(
        sorted({watcher_name for watcher_name in parsed_args.watchers if isinstance(watcher_name, str) and watcher_name})
    )
    return CliQueryRequest(
        start=start_value,
        end=end_value,
        devices=device_values,
        watchers=watcher_values,
        apply_afk_cleanup=bool(parsed_args.apply_afk_cleanup),
        agent_bypass=bool(parsed_args.agent_bypass),
    )


def _resolve_time_range(parsed_args: argparse.Namespace) -> tuple[datetime, datetime]:
    """统一处理相对时间与绝对时间两种输入方式。"""
    minutes_value = parsed_args.minutes
    start_text = parsed_args.start
    end_text = parsed_args.end

    if minutes_value is not None and (start_text is not None or end_text is not None):
        raise ValueError("`--minutes` 与 `--start/--end` 不能同时使用。")

    # 若用户没有显式提供时间条件，沿用历史脚本的 4 小时默认窗口。
    if minutes_value is None and start_text is None and end_text is None:
        minutes_value = 240

    if minutes_value is not None:
        if minutes_value <= 0:
            raise ValueError("`--minutes` 必须是正整数。")
        end_value = datetime.now(tz=timezone.utc)
        start_value = end_value - timedelta(minutes=minutes_value)
        return start_value, end_value

    if start_text is None or end_text is None:
        raise ValueError("使用绝对时间时必须同时提供 `--start` 和 `--end`。")

    start_value = parse_aw_timestamp(start_text)
    end_value = parse_aw_timestamp(end_text)
    if start_value is None:
        raise ValueError(f"无法解析开始时间: {start_text}")
    if end_value is None:
        raise ValueError(f"无法解析结束时间: {end_text}")
    if start_value >= end_value:
        raise ValueError("开始时间必须早于结束时间。")
    return start_value, end_value


def _execute_query(query_request: CliQueryRequest):
    """执行统一查询，确保 query/export 共享完全相同的结果。"""
    _sync_github_commits_if_needed(query_request)
    query_service = QueryService()
    return query_service.query_events(
        start=query_request.start,
        end=query_request.end,
        devices=list(query_request.devices) or None,
        watchers=list(query_request.watchers) or None,
        apply_afk_cleanup=query_request.apply_afk_cleanup,
        agent_bypass=query_request.agent_bypass,
    )


def _sync_github_commits_if_needed(query_request: CliQueryRequest) -> None:
    """只有查询可能包含 vscode/git 结果时，才在拉取前执行 GitHub 补齐。"""
    if query_request.watchers and "vscode" not in query_request.watchers:
        return

    try:
        sync_github_commits_for_range(
            start=query_request.start,
            end=query_request.end,
        )
    except Exception as error:
        # 同步失败不应阻断主查询流程，只在 stderr 给出提示。
        print(f"警告: GitHub commit 补齐失败，已跳过。原因: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
