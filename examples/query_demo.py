from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

# 让示例脚本支持直接从仓库根目录执行。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aw_client.query_service import QueryService


def main() -> int:
    """演示如何查询最近一小时的清洗结果。"""
    query_service = QueryService()
    end_time = datetime.now(tz=UTC)
    start_time = end_time - timedelta(hours=1)

    query_result = query_service.query_events(
        start=start_time,
        end=end_time,
        devices=None,
        watchers=["window", "web"],
        apply_afk_cleanup=True,
    )

    print(f"设备数: {len(query_result.buckets_by_device)}")
    print(f"有效事件数: {len(query_result.cleaned_events)}")
    print(f"用户有效时长(秒): {query_result.user_effective_seconds:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
