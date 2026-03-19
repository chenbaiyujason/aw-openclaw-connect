import json
import os
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_FILE_NAME = "aw-connect.config.json"
CONFIG_ENV_VAR_NAME = "AW_CONNECT_CONFIG"
USER_CONFIG_DIRECTORY_NAME = ".aw-connect"


@dataclass(frozen=True)
class GitSyncConfig:
    """Git 同步相关配置。"""

    enabled: bool
    organization_names: tuple[str, ...]
    branch_names: tuple[str, ...]
    source_path: Path | None


def _normalize_name_list(raw_value: object) -> tuple[str, ...]:
    """把配置里的字符串列表做去重和清洗。"""
    if not isinstance(raw_value, list):
        return ()

    normalized_values: list[str] = []
    for item in raw_value:
        if not isinstance(item, str):
            continue
        stripped_item = item.strip()
        if not stripped_item or stripped_item in normalized_values:
            continue
        normalized_values.append(stripped_item)
    return tuple(normalized_values)


def _append_unique_path(candidate_paths: list[Path], candidate_path: Path) -> None:
    """按解析后的绝对路径去重，避免重复读取同一位置。"""
    resolved_path = candidate_path.expanduser().resolve()
    if resolved_path not in candidate_paths:
        candidate_paths.append(resolved_path)


def get_config_candidate_paths() -> list[Path]:
    """返回源码运行和已安装 CLI 都可复用的配置搜索路径。"""
    candidate_paths: list[Path] = []

    current_working_directory = Path.cwd()
    env_config_path = os.environ.get(CONFIG_ENV_VAR_NAME, "")
    if env_config_path.strip():
        _append_unique_path(candidate_paths, Path(env_config_path.strip()))

    _append_unique_path(candidate_paths, current_working_directory / DEFAULT_CONFIG_FILE_NAME)
    _append_unique_path(candidate_paths, Path.home() / USER_CONFIG_DIRECTORY_NAME / DEFAULT_CONFIG_FILE_NAME)
    _append_unique_path(candidate_paths, Path.home() / DEFAULT_CONFIG_FILE_NAME)

    # 源码开发和少数脚本入口场景继续兼容。
    script_directory = Path(sys.argv[0]).resolve().parent
    _append_unique_path(candidate_paths, script_directory / DEFAULT_CONFIG_FILE_NAME)
    executable_directory = Path(sys.executable).resolve().parent
    _append_unique_path(candidate_paths, executable_directory / DEFAULT_CONFIG_FILE_NAME)
    _append_unique_path(candidate_paths, PROJECT_ROOT / DEFAULT_CONFIG_FILE_NAME)
    return candidate_paths


def _load_default_git_sync_config() -> GitSyncConfig:
    """从安装包内置默认配置读取兜底值。"""
    default_config_resource = resources.files("aw_client").joinpath("defaults/aw-connect.config.json")
    raw_payload = json.loads(default_config_resource.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict):
        raise ValueError("内置默认配置格式错误。")

    git_sync_payload = raw_payload.get("git_sync")
    if not isinstance(git_sync_payload, dict):
        raise ValueError("内置默认配置缺少 git_sync。")

    enabled_value = git_sync_payload.get("enabled")
    organization_names = _normalize_name_list(git_sync_payload.get("organization_names"))
    branch_names = _normalize_name_list(git_sync_payload.get("branch_names"))
    return GitSyncConfig(
        enabled=enabled_value if isinstance(enabled_value, bool) else True,
        organization_names=organization_names,
        branch_names=branch_names,
        source_path=None,
    )


def load_git_sync_config() -> GitSyncConfig:
    """读取 Git 同步配置；未找到文件时返回默认值。"""
    default_config = _load_default_git_sync_config()

    for candidate_path in get_config_candidate_paths():
        if not candidate_path.is_file():
            continue

        raw_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, dict):
            raise ValueError(f"配置文件格式错误: {candidate_path}")
        git_sync_payload = raw_payload.get("git_sync")
        if not isinstance(git_sync_payload, dict):
            return GitSyncConfig(
                enabled=default_config.enabled,
                organization_names=default_config.organization_names,
                branch_names=default_config.branch_names,
                source_path=candidate_path,
            )

        enabled_value = git_sync_payload.get("enabled")
        if "organization_names" in git_sync_payload:
            organization_names = _normalize_name_list(git_sync_payload.get("organization_names"))
        else:
            organization_names = default_config.organization_names
        if "branch_names" in git_sync_payload:
            branch_names = _normalize_name_list(git_sync_payload.get("branch_names"))
        else:
            branch_names = default_config.branch_names
        return GitSyncConfig(
            enabled=enabled_value if isinstance(enabled_value, bool) else default_config.enabled,
            organization_names=organization_names,
            branch_names=branch_names,
            source_path=candidate_path,
        )

    return default_config
