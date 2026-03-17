from __future__ import annotations

from dataclasses import dataclass
import re

from aw_client.models import BucketDescriptor, NormalizedBucket


WATCHER_PREFIX_PATTERN = re.compile(r"^aw-watcher-([^_]+)")


@dataclass(slots=True)
class BucketRegistry:
    """按逻辑设备与 watcher family 组织的 bucket 索引。"""

    buckets_by_device: dict[str, dict[str, list[NormalizedBucket]]]

    def device_names(self) -> list[str]:
        """返回已发现的逻辑设备名。"""
        return sorted(self.buckets_by_device)

    def watcher_families(self, device_name: str | None = None) -> list[str]:
        """返回指定设备或全局的 watcher family 列表。"""
        if device_name is not None:
            return sorted(self.buckets_by_device.get(device_name, {}))

        families: set[str] = set()
        for watcher_map in self.buckets_by_device.values():
            families.update(watcher_map)
        return sorted(families)


def build_bucket_registry(buckets: dict[str, BucketDescriptor]) -> BucketRegistry:
    """把 `/buckets` 结果归一化为 `device -> watcher_family -> buckets[]`。"""
    normalized_buckets: list[NormalizedBucket] = []
    current_device_name = detect_current_device_name(buckets)

    for bucket_id, bucket_descriptor in buckets.items():
        normalized_buckets.append(
            normalize_bucket(
                bucket_id=bucket_id,
                descriptor=bucket_descriptor,
                current_device_name=current_device_name,
            )
        )

    buckets_by_device: dict[str, dict[str, list[NormalizedBucket]]] = {}
    for normalized_bucket in normalized_buckets:
        watcher_map = buckets_by_device.setdefault(normalized_bucket.device_name, {})
        watcher_map.setdefault(normalized_bucket.watcher_family, []).append(normalized_bucket)

    for watcher_map in buckets_by_device.values():
        for watcher_family, family_buckets in watcher_map.items():
            watcher_map[watcher_family] = sorted(
                family_buckets,
                key=lambda item: (
                    item.priority_rank,
                    0 if item.time_end is None else -item.time_end.timestamp(),
                    0 if item.time_start is None or item.time_end is None else -(item.time_end - item.time_start).total_seconds(),
                    item.bucket_id,
                ),
            )

    return BucketRegistry(buckets_by_device=buckets_by_device)


def normalize_bucket(
    bucket_id: str,
    descriptor: BucketDescriptor,
    current_device_name: str | None = None,
) -> NormalizedBucket:
    """把单个 bucket 转成统一语义模型。"""
    sync_origin = extract_sync_origin(bucket_id, descriptor)
    watcher_family = detect_watcher_family(bucket_id, descriptor.client, descriptor.bucket_type)
    source_kind = "synced" if is_synced_bucket(bucket_id, descriptor) else "local"
    device_name = sync_origin or descriptor.hostname or extract_device_suffix(bucket_id) or "unknown-device"
    is_current_device_candidate = current_device_name is not None and device_name == current_device_name

    return NormalizedBucket(
        bucket_id=bucket_id,
        watcher_family=watcher_family,
        device_name=device_name,
        source_kind=source_kind,
        sync_origin=sync_origin,
        hostname=descriptor.hostname,
        client=descriptor.client,
        bucket_type=descriptor.bucket_type,
        time_start=descriptor.metadata_start,
        time_end=descriptor.metadata_end,
        is_current_device_candidate=is_current_device_candidate,
        priority_rank=compute_bucket_priority(source_kind, descriptor),
    )


def detect_current_device_name(buckets: dict[str, BucketDescriptor]) -> str | None:
    """基于本地 bucket 出现频次，推断当前机器对应的逻辑设备名。"""
    local_host_counter: dict[str, int] = {}
    for bucket_id, descriptor in buckets.items():
        if is_synced_bucket(bucket_id, descriptor):
            continue
        if descriptor.hostname:
            local_host_counter[descriptor.hostname] = local_host_counter.get(descriptor.hostname, 0) + 1

    if not local_host_counter:
        return None

    return max(local_host_counter.items(), key=lambda item: item[1])[0]


def detect_watcher_family(bucket_id: str, client: str, bucket_type: str) -> str:
    """自动把 bucket 归并到稳定的 watcher family。"""
    if bucket_id.startswith("aw-watcher-afk_") or client == "aw-watcher-afk" or bucket_type == "afkstatus":
        return "afk"
    if bucket_id.startswith("aw-watcher-window_") or client == "aw-watcher-window" or bucket_type == "currentwindow":
        return "window"
    if bucket_id.startswith("aw-watcher-vscode-agent_") or bucket_type == "com.activitywatch.cursor.agent.lifecycle":
        return "agent"
    if bucket_id.startswith("aw-watcher-vscode_") or client == "aw-watcher-vscode" or bucket_type == "app.editor.activity":
        return "vscode"
    if bucket_id.startswith("aw-watcher-web-") or client == "aw-client-web" or bucket_type.startswith("web."):
        return "web"

    prefix_match = WATCHER_PREFIX_PATTERN.match(bucket_id)
    if prefix_match:
        return prefix_match.group(1)

    if client:
        return client.replace("aw-watcher-", "").replace("aw-client-", "")

    return bucket_type or "unknown"


def extract_sync_origin(bucket_id: str, descriptor: BucketDescriptor) -> str | None:
    """优先从元数据中提取真实来源设备，再回退到 bucket 名解析。"""
    sync_origin = descriptor.data.get("$aw.sync.origin")
    if isinstance(sync_origin, str) and sync_origin:
        return sync_origin

    if "-synced-from-" in bucket_id:
        return bucket_id.rsplit("-synced-from-", maxsplit=1)[-1] or None

    return None


def is_synced_bucket(bucket_id: str, descriptor: BucketDescriptor) -> bool:
    """识别是否为同步导入的 bucket。"""
    if "-synced-from-" in bucket_id:
        return True

    sync_origin = descriptor.data.get("$aw.sync.origin")
    return isinstance(sync_origin, str) and bool(sync_origin)


def extract_device_suffix(bucket_id: str) -> str | None:
    """从 bucket id 中提取设备后缀，作为兜底设备名。"""
    if "_" not in bucket_id:
        return None

    suffix = bucket_id.split("_", maxsplit=1)[-1]
    if "-synced-from-" in suffix:
        suffix = suffix.split("-synced-from-", maxsplit=1)[0]

    return suffix or None


def compute_bucket_priority(source_kind: str, descriptor: BucketDescriptor) -> int:
    """给 bucket 一个稳定优先级，后续冲突解析时复用。"""
    priority_value = 0 if source_kind == "local" else 10
    if descriptor.metadata_end is None:
        priority_value += 5
    return priority_value
