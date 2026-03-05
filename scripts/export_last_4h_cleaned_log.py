from __future__ import annotations

import argparse
from pathlib import Path
import sys

# 让脚本支持直接从仓库根目录执行，无需额外设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aw_client.cli import main as cli_main


def build_argument_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    argument_parser = argparse.ArgumentParser(description="导出最近一段时间的 ActivityWatch 清洗结果到日志文件。")
    argument_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="可选的日志输出路径，默认写入 logs/ 目录。",
    )
    argument_parser.add_argument(
        "--minutes",
        type=int,
        default=240,
        help="导出最近多少分钟的数据，默认 240 分钟。",
    )
    return argument_parser


def main() -> int:
    """兼容旧脚本入口，并转发到正式 CLI。"""
    argument_parser = build_argument_parser()
    parsed_args = argument_parser.parse_args()
    forwarded_argv: list[str] = [
        "export",
        "--minutes",
        str(parsed_args.minutes),
    ]
    if parsed_args.output is not None:
        forwarded_argv.extend(["--output", str(parsed_args.output)])

    # 保留兼容脚本，但明确提示正式入口已经迁移。
    print("提示: `scripts/export_last_4h_cleaned_log.py` 已迁移到 `aw-connect export`。", file=sys.stderr)
    return cli_main(forwarded_argv)


if __name__ == "__main__":
    raise SystemExit(main())
