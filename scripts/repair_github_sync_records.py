from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import parse, request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aw_client.github_sync import (  # noqa: E402
    _build_aw_event_payload,
    _load_branch_commits,
    _load_repository_branches,
    _load_viewer_identity,
)
from aw_client.rest_client import ActivityWatchRestClient  # noqa: E402


def delete_event(base_url: str, bucket_id: str, event_id: int | str) -> None:
    """临时修复脚本专用：删除一条 AW 事件。"""
    encoded_bucket_id = parse.quote(bucket_id, safe="")
    encoded_event_id = parse.quote(str(event_id), safe="")
    request_url = f"{base_url.rstrip('/')}/buckets/{encoded_bucket_id}/events/{encoded_event_id}"
    http_request = request.Request(
        url=request_url,
        method="DELETE",
        headers={"Accept": "application/json"},
    )
    with request.urlopen(http_request, timeout=15):
        return


def repair_specific_commit(target_hash: str) -> bool:
    """按单个 hash 修复旧版错误同步记录。"""
    client = ActivityWatchRestClient()
    viewer = _load_viewer_identity()
    all_buckets = client.list_buckets()
    git_bucket_ids = [bucket_id for bucket_id in all_buckets if "git-commit" in bucket_id]
    if not git_bucket_ids:
        print("错误: 未在 ActivityWatch 中发现任何 git-commit 类型的 Bucket。")
        return False

    repaired = False
    for bucket_id in git_bucket_ids:
        try:
            bucket_events = client.get_events(
                bucket_id,
                start=datetime.now(timezone.utc) - timedelta(days=60),
                end=datetime.now(timezone.utc),
            )
        except Exception:
            continue

        for event in bucket_events:
            commit_hash = event.data.get("commitHashFull")
            if not isinstance(commit_hash, str) or not commit_hash:
                legacy_hash = event.data.get("hash")
                commit_hash = legacy_hash if isinstance(legacy_hash, str) else ""
            if commit_hash != target_hash or event.event_id is None:
                continue

            repository_name = event.data.get("project")
            if not isinstance(repository_name, str) or "/" not in repository_name:
                continue

            candidate_commit = None
            try:
                branch_names = _load_repository_branches(repository_name)
            except Exception:
                branch_names = []

            for branch_name in branch_names:
                try:
                    branch_commits = _load_branch_commits(
                        repository_name=repository_name,
                        branch_name=branch_name,
                        since=event.timestamp - timedelta(days=2),
                        until=event.timestamp + timedelta(days=2),
                        viewer=viewer,
                    )
                except Exception:
                    continue

                candidate_commit = next(
                    (commit_record for commit_record in branch_commits if commit_record["hash"] == target_hash),
                    None,
                )
                if candidate_commit is not None:
                    break

            if candidate_commit is None:
                continue

            delete_event(client.base_url, bucket_id, event.event_id)
            client.post_events(bucket_id, [_build_aw_event_payload(candidate_commit)])
            print(
                json.dumps(
                    {
                        "bucket": bucket_id,
                        "hash": target_hash,
                        "branch": candidate_commit["branch"],
                        "subject": candidate_commit["subject"],
                    },
                    ensure_ascii=False,
                )
            )
            repaired = True

    if not repaired:
        print("未找到可修复的旧记录，或 GitHub 上未能解析出对应 commit。")
    return repaired


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("用法: python scripts/repair_github_sync_records.py <commit-hash>")
    repair_specific_commit(sys.argv[1])
