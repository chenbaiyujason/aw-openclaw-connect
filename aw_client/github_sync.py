import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict
from urllib import parse

# 让脚本支持直接从仓库根目录执行。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aw_client.bucket_registry import detect_current_device_name
from aw_client.config import GitSyncConfig, load_git_sync_config
from aw_client.rest_client import ActivityWatchRestClient


class GitHubRepoRef(TypedDict):
    """参与扫描的 GitHub 仓库。"""

    name_with_owner: str
    default_branch: str
    pushed_at: datetime | None
    is_private: bool


class GitHubCommit(TypedDict):
    """归一化后的 GitHub commit。"""

    hash: str
    repository: str
    branch: str
    subject: str
    body: str
    timestamp: datetime
    author_name: str
    author_email: str
    commit_date: str
    author_date: str
    parent_hashes: list[str]


class SyncStats(TypedDict):
    """一次同步执行后的统计结果。"""

    inserted_count: int


class GitHubViewer(TypedDict):
    """当前 gh 登录身份。"""

    login: str
    name: str


MERGE_BRANCH_CACHE: dict[tuple[str, str], str] = {}
MAX_REPOSITORY_FETCH_WORKERS = 6
MAX_BUCKET_READ_WORKERS = 4


def _run_gh_command(command_args: list[str]) -> str:
    """执行 gh 命令并返回标准输出。"""
    completed_process = subprocess.run(
        command_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed_process.stdout


def _load_viewer_identity() -> GitHubViewer:
    """读取当前 gh 登录用户，后续所有归因都围绕这个身份。"""
    raw_payload = _run_gh_command(["gh", "api", "user"])
    parsed_payload = json.loads(raw_payload)
    login_value = parsed_payload.get("login")
    name_value = parsed_payload.get("name")
    if not isinstance(login_value, str) or not login_value:
        raise ValueError("无法从 gh 读取当前登录用户 login。")
    return {
        "login": login_value,
        "name": name_value.strip() if isinstance(name_value, str) and name_value.strip() else "",
    }


def _parse_github_datetime(value: object) -> datetime | None:
    """把 GitHub 时间文本稳定转成 UTC datetime。"""
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _normalize_commit_message(message_text: str) -> tuple[str, str]:
    """拆出 subject 与 body，保持和本地 watcher 的结构一致。"""
    normalized_message = message_text.replace("\r\n", "\n").replace("\r", "\n")
    message_parts = normalized_message.split("\n", maxsplit=1)
    subject_value = message_parts[0].strip()
    body_value = message_parts[1].strip() if len(message_parts) > 1 else ""
    return subject_value, body_value


def _is_merge_commit(message_subject: str, parent_hashes: list[str]) -> bool:
    """识别典型 GitHub merge commit，优先走 PR base branch 归因。"""
    return message_subject.startswith("Merge pull request #") or len(parent_hashes) > 1


def _resolve_commit_branch(
    repository_name: str,
    commit_hash: str,
    fallback_branch_name: str,
    message_subject: str,
    parent_hashes: list[str],
) -> str:
    """解析 commit 的真实归因分支，merge commit 优先使用 PR base branch。"""
    cache_key = (repository_name, commit_hash)
    cached_branch_name = MERGE_BRANCH_CACHE.get(cache_key)
    if isinstance(cached_branch_name, str) and cached_branch_name:
        return cached_branch_name

    resolved_branch_name = fallback_branch_name
    if _is_merge_commit(message_subject, parent_hashes):
        try:
            raw_output = _run_gh_command(
                [
                    "gh",
                    "api",
                    f"repos/{repository_name}/commits/{commit_hash}/pulls",
                ]
            )
            parsed_payload = json.loads(raw_output)
            if isinstance(parsed_payload, list):
                merged_pull_requests = [
                    pull_request
                    for pull_request in parsed_payload
                    if isinstance(pull_request, dict)
                    and pull_request.get("merge_commit_sha") == commit_hash
                    and isinstance(pull_request.get("merged_at"), str)
                    and pull_request.get("merged_at")
                ]
                if merged_pull_requests:
                    base_payload = merged_pull_requests[0].get("base")
                    base_ref = base_payload.get("ref") if isinstance(base_payload, dict) else None
                    if isinstance(base_ref, str) and base_ref:
                        resolved_branch_name = base_ref
        except subprocess.CalledProcessError:
            resolved_branch_name = fallback_branch_name

    MERGE_BRANCH_CACHE[cache_key] = resolved_branch_name
    return resolved_branch_name


def _email_matches_login(email_value: str, login_value: str) -> bool:
    """兼容 GitHub noreply 邮箱和显式登录名邮箱。"""
    normalized_email = email_value.strip().lower()
    normalized_login = login_value.strip().lower()
    if not normalized_email or not normalized_login:
        return False
    return normalized_email == f"{normalized_login}@users.noreply.github.com" or normalized_email.endswith(
        f"+{normalized_login}@users.noreply.github.com"
    )


def _commit_matches_viewer(commit_payload: dict[str, object], viewer: GitHubViewer) -> bool:
    """判断这条 commit 是否应归因到当前 gh 登录身份。"""
    author_payload = commit_payload.get("author")
    committer_payload = commit_payload.get("committer")
    nested_commit_payload = commit_payload.get("commit")
    if not isinstance(nested_commit_payload, dict):
        return False

    nested_author_payload = nested_commit_payload.get("author")
    nested_committer_payload = nested_commit_payload.get("committer")
    author_login = author_payload.get("login") if isinstance(author_payload, dict) else None
    committer_login = committer_payload.get("login") if isinstance(committer_payload, dict) else None
    author_email = nested_author_payload.get("email") if isinstance(nested_author_payload, dict) else None
    committer_email = nested_committer_payload.get("email") if isinstance(nested_committer_payload, dict) else None
    author_name = nested_author_payload.get("name") if isinstance(nested_author_payload, dict) else None
    committer_name = nested_committer_payload.get("name") if isinstance(nested_committer_payload, dict) else None

    if author_login == viewer["login"] or committer_login == viewer["login"]:
        return True
    if isinstance(author_email, str) and _email_matches_login(author_email, viewer["login"]):
        return True
    if isinstance(committer_email, str) and _email_matches_login(committer_email, viewer["login"]):
        return True
    if viewer["name"]:
        if isinstance(author_name, str) and author_name.strip() == viewer["name"]:
            return True
        if isinstance(committer_name, str) and committer_name.strip() == viewer["name"]:
            return True
    return False


def _load_repositories_for_owner(owner_name: str) -> list[GitHubRepoRef]:
    """拉取单个 owner 下的仓库列表。"""
    raw_output = _run_gh_command(
        [
            "gh",
            "repo",
            "list",
            owner_name,
            "--limit",
            "1000",
            "--json",
            "nameWithOwner,pushedAt,defaultBranchRef,isPrivate",
        ]
    )
    parsed_payload = json.loads(raw_output)
    if not isinstance(parsed_payload, list):
        return []

    repositories: list[GitHubRepoRef] = []
    for repository_payload in parsed_payload:
        if not isinstance(repository_payload, dict):
            continue
        name_with_owner = repository_payload.get("nameWithOwner")
        if not isinstance(name_with_owner, str) or not name_with_owner:
            continue
        pushed_at = _parse_github_datetime(repository_payload.get("pushedAt"))
        default_branch_payload = repository_payload.get("defaultBranchRef")
        default_branch_name = (
            default_branch_payload.get("name")
            if isinstance(default_branch_payload, dict)
            else None
        )
        repositories.append(
            {
                "name_with_owner": name_with_owner,
                "default_branch": default_branch_name if isinstance(default_branch_name, str) and default_branch_name else "unknown",
                "pushed_at": pushed_at,
                "is_private": bool(repository_payload.get("isPrivate")),
            }
        )
    return repositories


def _load_candidate_repositories(
    since: datetime,
    until: datetime,
    viewer: GitHubViewer,
    git_sync_config: GitSyncConfig,
) -> list[GitHubRepoRef]:
    """只保留用户本人和配置组织下在时间窗口内更新过的仓库。"""
    owners = [viewer["login"], *git_sync_config.organization_names]
    repositories_by_name: dict[str, GitHubRepoRef] = {}

    for owner_name in owners:
        try:
            owner_repositories = _load_repositories_for_owner(owner_name)
        except subprocess.CalledProcessError:
            continue
        for repository_ref in owner_repositories:
            pushed_at = repository_ref["pushed_at"]
            if pushed_at is None:
                continue
            if pushed_at < since or pushed_at > until:
                continue
            repositories_by_name[repository_ref["name_with_owner"]] = repository_ref

    return sorted(repositories_by_name.values(), key=lambda item: item["name_with_owner"])


def _build_target_branch_names(default_branch_name: str, git_sync_config: GitSyncConfig) -> list[str]:
    """每个仓库只查默认分支和配置里的附加分支。"""
    branch_names: list[str] = []
    if default_branch_name and default_branch_name != "unknown":
        branch_names.append(default_branch_name)
    for configured_branch_name in git_sync_config.branch_names:
        if configured_branch_name not in branch_names:
            branch_names.append(configured_branch_name)
    return branch_names


def _build_commit_from_payload(
    repository_name: str,
    branch_name: str,
    commit_payload: dict[str, object],
) -> GitHubCommit | None:
    """把 GitHub 原始 commit payload 转成统一结构。"""
    nested_commit_payload = commit_payload.get("commit")
    if not isinstance(nested_commit_payload, dict):
        return None

    sha_value = commit_payload.get("sha")
    nested_author_payload = nested_commit_payload.get("author")
    nested_committer_payload = nested_commit_payload.get("committer")
    message_value = nested_commit_payload.get("message")
    parents_payload = commit_payload.get("parents")

    if not isinstance(sha_value, str) or not sha_value:
        return None
    if not isinstance(nested_author_payload, dict):
        return None
    if not isinstance(nested_committer_payload, dict):
        return None
    if not isinstance(message_value, str) or not message_value:
        return None

    author_date = nested_author_payload.get("date")
    commit_date = nested_committer_payload.get("date")
    parsed_timestamp = _parse_github_datetime(author_date)
    if parsed_timestamp is None:
        return None

    subject_value, body_value = _normalize_commit_message(message_value)
    parent_hashes: list[str] = []
    if isinstance(parents_payload, list):
        for parent_payload in parents_payload:
            if not isinstance(parent_payload, dict):
                continue
            parent_sha = parent_payload.get("sha")
            if isinstance(parent_sha, str) and parent_sha:
                parent_hashes.append(parent_sha)

    resolved_branch_name = _resolve_commit_branch(
        repository_name=repository_name,
        commit_hash=sha_value,
        fallback_branch_name=branch_name,
        message_subject=subject_value,
        parent_hashes=parent_hashes,
    )

    return {
        "hash": sha_value,
        "repository": repository_name,
        "branch": resolved_branch_name,
        "subject": subject_value,
        "body": body_value,
        "timestamp": parsed_timestamp,
        "author_name": nested_author_payload.get("name") if isinstance(nested_author_payload.get("name"), str) else "unknown",
        "author_email": nested_author_payload.get("email") if isinstance(nested_author_payload.get("email"), str) else "unknown",
        "commit_date": commit_date if isinstance(commit_date, str) else parsed_timestamp.isoformat().replace("+00:00", "Z"),
        "author_date": author_date if isinstance(author_date, str) else parsed_timestamp.isoformat().replace("+00:00", "Z"),
        "parent_hashes": parent_hashes,
    }


def _safe_load_branch_commits(
    repository_name: str,
    branch_name: str,
    since: datetime,
    until: datetime,
    viewer: GitHubViewer,
) -> list[GitHubCommit]:
    """安全拉取单分支 commit，分支不存在时直接跳过。"""
    try:
        return _load_branch_commits(
            repository_name=repository_name,
            branch_name=branch_name,
            since=since,
            until=until,
            viewer=viewer,
        )
    except subprocess.CalledProcessError:
        return []


def _collect_repository_commits(
    repository_ref: GitHubRepoRef,
    since: datetime,
    until: datetime,
    viewer: GitHubViewer,
    git_sync_config: GitSyncConfig,
) -> list[GitHubCommit]:
    """收集单个仓库默认分支和配置分支上的相关 commit。"""
    commits_by_hash: dict[str, GitHubCommit] = {}
    for branch_name in _build_target_branch_names(repository_ref["default_branch"], git_sync_config):
        branch_commits = _safe_load_branch_commits(
            repository_name=repository_ref["name_with_owner"],
            branch_name=branch_name,
            since=since,
            until=until,
            viewer=viewer,
        )
        for branch_commit in branch_commits:
            existing_commit = commits_by_hash.get(branch_commit["hash"])
            if existing_commit is None:
                commits_by_hash[branch_commit["hash"]] = branch_commit
                continue
            if (
                existing_commit["branch"] != repository_ref["default_branch"]
                and branch_commit["branch"] == repository_ref["default_branch"]
            ):
                commits_by_hash[branch_commit["hash"]] = branch_commit
    return list(commits_by_hash.values())


def _load_branch_commits(
    repository_name: str,
    branch_name: str,
    since: datetime,
    until: datetime,
    viewer: GitHubViewer,
) -> list[GitHubCommit]:
    """拉取单个分支在时间窗口内的 commit，并按当前身份做归因。"""
    endpoint = (
        f"repos/{repository_name}/commits"
        f"?sha={parse.quote(branch_name, safe='')}"
        f"&since={since.isoformat().replace('+00:00', 'Z')}"
        f"&until={until.isoformat().replace('+00:00', 'Z')}"
        f"&per_page=100"
    )
    raw_output = _run_gh_command(["gh", "api", "--paginate", "--slurp", endpoint])
    parsed_pages = json.loads(raw_output)
    if not isinstance(parsed_pages, list):
        return []

    branch_commits: list[GitHubCommit] = []
    for page_payload in parsed_pages:
        if not isinstance(page_payload, list):
            continue
        for commit_payload in page_payload:
            if not isinstance(commit_payload, dict):
                continue
            if not _commit_matches_viewer(commit_payload, viewer):
                continue
            normalized_commit = _build_commit_from_payload(
                repository_name=repository_name,
                branch_name=branch_name,
                commit_payload=commit_payload,
            )
            if normalized_commit is not None:
                branch_commits.append(normalized_commit)
    return branch_commits


def fetch_recent_github_commits(
    since: datetime,
    until: datetime | None = None,
    git_sync_config: GitSyncConfig | None = None,
) -> list[GitHubCommit]:
    """
    从 gh 登录身份可访问的仓库和分支中，收集该身份的全部 commit。

    这里不再依赖 `search/commits`，因为它会漏掉组织私有仓库和部分 merge commit。
    """
    until_value = (until or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since_value = since.astimezone(timezone.utc)
    resolved_git_sync_config = git_sync_config or load_git_sync_config()
    viewer = _load_viewer_identity()
    repositories = _load_candidate_repositories(
        since=since_value,
        until=until_value,
        viewer=viewer,
        git_sync_config=resolved_git_sync_config,
    )
    commits_by_hash: dict[str, GitHubCommit] = {}

    with ThreadPoolExecutor(max_workers=MAX_REPOSITORY_FETCH_WORKERS) as executor:
        future_to_repository = {
            executor.submit(
                _collect_repository_commits,
                repository_ref,
                since_value,
                until_value,
                viewer,
                resolved_git_sync_config,
            ): repository_ref["name_with_owner"]
            for repository_ref in repositories
        }
        for future in as_completed(future_to_repository):
            repository_commits = future.result()
            for repository_commit in repository_commits:
                existing_commit = commits_by_hash.get(repository_commit["hash"])
                if existing_commit is None:
                    commits_by_hash[repository_commit["hash"]] = repository_commit

    return sorted(
        commits_by_hash.values(),
        key=lambda item: (item["timestamp"], item["hash"]),
    )


def _read_bucket_commit_hashes(
    client: ActivityWatchRestClient,
    bucket_id: str,
    since: datetime,
    until: datetime,
) -> set[str]:
    """读取单个 bucket 中已有的 commit hash。"""
    existing_hashes: set[str] = set()
    try:
        bucket_events = client.get_events(bucket_id, start=since, end=until)
    except Exception as error:
        print(f"警告: 无法读取 Bucket {bucket_id} 的数据: {error}")
        return existing_hashes

    for event in bucket_events:
        commit_hash = event.data.get("commitHashFull")
        if isinstance(commit_hash, str) and commit_hash:
            existing_hashes.add(commit_hash)
            continue
        fallback_hash = event.data.get("hash")
        if isinstance(fallback_hash, str) and fallback_hash:
            existing_hashes.add(fallback_hash)
    return existing_hashes


def _extract_existing_commit_hashes(
    client: ActivityWatchRestClient,
    git_bucket_ids: list[str],
    since: datetime,
    until: datetime,
) -> set[str]:
    """从本地 AW 现有 bucket 中提取已记录的 commit hash。"""
    existing_hashes: set[str] = set()
    with ThreadPoolExecutor(max_workers=MAX_BUCKET_READ_WORKERS) as executor:
        future_to_bucket = {
            executor.submit(_read_bucket_commit_hashes, client, bucket_id, since, until): bucket_id
            for bucket_id in git_bucket_ids
        }
        for future in as_completed(future_to_bucket):
            existing_hashes.update(future.result())
    return existing_hashes


def _build_aw_event_payload(commit_record: GitHubCommit) -> dict[str, object]:
    """构造与本地 watcher 尽量兼容的 commit 事件。"""
    workspace_name = commit_record["repository"].rsplit("/", maxsplit=1)[-1]
    return {
        "timestamp": commit_record["timestamp"].isoformat().replace("+00:00", "Z"),
        "duration": 0.0,
        "data": {
            "authorDate": commit_record["author_date"],
            "authorEmail": commit_record["author_email"],
            "authorName": commit_record["author_name"],
            "body": commit_record["body"],
            "branch": commit_record["branch"],
            "commitDate": commit_record["commit_date"],
            "commitHashFull": commit_record["hash"],
            "eventName": "commit_summary",
            "file": "unknown",
            "language": "unknown",
            "parentHashes": commit_record["parent_hashes"],
            "project": commit_record["repository"],
            "relatedAgentSessionId": "github_sync",
            "repoPath": commit_record["repository"],
            "subject": commit_record["subject"],
            "workspaceId": workspace_name,
            "isSyncedFromGitHub": True,
        },
    }


def sync_github_commits_for_range(start: datetime, end: datetime) -> SyncStats:
    """按给定时间范围同步 GitHub commit。"""
    client = ActivityWatchRestClient()
    since_value = start.astimezone(timezone.utc)
    until_value = end.astimezone(timezone.utc)
    git_sync_config = load_git_sync_config()

    if not git_sync_config.enabled:
        config_hint = (
            f"配置文件 {git_sync_config.source_path}"
            if git_sync_config.source_path is not None
            else "默认配置"
        )
        print(f"GitHub 提交同步已禁用，跳过本次补齐。来源: {config_hint}")
        return {"inserted_count": 0}

    print(f"正在从 GitHub 拉取 {since_value.isoformat()} 到 {until_value.isoformat()} 的提交记录...")
    github_commits = fetch_recent_github_commits(
        since=since_value,
        until=until_value,
        git_sync_config=git_sync_config,
    )
    if not github_commits:
        print("未发现新的 GitHub 提交。")
        return {"inserted_count": 0}

    all_buckets = client.list_buckets()
    git_bucket_ids = [bucket_id for bucket_id in all_buckets if "git-commit" in bucket_id]
    if not git_bucket_ids:
        print("错误: 未在 ActivityWatch 中发现任何 git-commit 类型的 Bucket。")
        return {"inserted_count": 0}

    print(f"发现 {len(git_bucket_ids)} 个 Git Bucket: {', '.join(git_bucket_ids)}")
    existing_hashes = _extract_existing_commit_hashes(
        client=client,
        git_bucket_ids=git_bucket_ids,
        since=since_value - timedelta(days=1),
        until=until_value + timedelta(days=1),
    )
    missing_commits = [commit_record for commit_record in github_commits if commit_record["hash"] not in existing_hashes]
    inserted_count = 0
    if missing_commits:
        current_device = detect_current_device_name(all_buckets)
        primary_bucket_id = next(
            (bucket_id for bucket_id in git_bucket_ids if current_device and current_device in bucket_id),
            git_bucket_ids[0],
        )
        events_to_post = [_build_aw_event_payload(commit_record) for commit_record in missing_commits]
        print(f"检测到 {len(events_to_post)} 条缺失 commit，正在补齐到 {primary_bucket_id} ...")
        client.post_events(primary_bucket_id, events_to_post)
        inserted_count = len(events_to_post)
    else:
        print("所有 GitHub 提交已在本地 ActivityWatch 中记录，无需补齐。")

    print(f"同步完成。补齐 {inserted_count} 条。")
    return {
        "inserted_count": inserted_count,
    }


def sync_all_github_commits(days: int = 7) -> int:
    """按最近若干天范围运行同步，并返回总处理条数。"""
    until_value = datetime.now(timezone.utc)
    since_value = until_value - timedelta(days=days)
    sync_stats = sync_github_commits_for_range(start=since_value, end=until_value)
    return sync_stats["inserted_count"]


def find_specific_commit(target_hash: str) -> bool:
    """在本地 AW git bucket 中按 hash 搜索 commit。"""
    client = ActivityWatchRestClient()
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=60)
    git_bucket_ids = [bucket_id for bucket_id in client.list_buckets() if "git-commit" in bucket_id]

    print(f"正在搜索 Commit: {target_hash} ...")
    found_commit = False
    for bucket_id in git_bucket_ids:
        try:
            bucket_events = client.get_events(bucket_id, start=start_time, end=end_time)
        except Exception:
            continue
        for event in bucket_events:
            commit_hash = event.data.get("commitHashFull")
            fallback_hash = event.data.get("hash")
            if commit_hash == target_hash or fallback_hash == target_hash:
                print(f"找到！在 Bucket: {bucket_id}")
                print(f"时间: {event.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"分支: {event.data.get('branch')}")
                print(f"信息: {event.data.get('subject')}")
                found_commit = True
    if not found_commit:
        print("在本地 ActivityWatch 数据库中未找到该 Commit。")
    return found_commit


if __name__ == "__main__":
    if len(sys.argv) > 1:
        find_specific_commit(sys.argv[1])
    else:
        sync_all_github_commits()
