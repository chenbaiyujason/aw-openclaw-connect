from __future__ import annotations

from datetime import datetime

from aw_client.bucket_registry import BucketRegistry
from aw_client.intervals import clamp_interval, create_event_end
from aw_client.models import EventInterval, NormalizedBucket
from aw_client.rest_client import ActivityWatchRestClient


def load_events_for_family(
    client: ActivityWatchRestClient,
    registry: BucketRegistry,
    start: datetime,
    end: datetime,
    device_names: list[str],
    watcher_family: str,
) -> dict[str, list[EventInterval]]:
    """按设备加载某个 watcher family 的全部事件，并完成基础去重。"""
    events_by_device: dict[str, list[EventInterval]] = {}

    for device_name in device_names:
        family_buckets = registry.buckets_by_device.get(device_name, {}).get(watcher_family, [])
        if not family_buckets:
            continue

        device_events: list[EventInterval] = []
        for family_bucket in family_buckets:
            device_events.extend(load_events_for_bucket(client, family_bucket, start, end))

        events_by_device[device_name] = deduplicate_events(device_events)

    return events_by_device


def load_events_for_bucket(
    client: ActivityWatchRestClient,
    normalized_bucket: NormalizedBucket,
    start: datetime,
    end: datetime,
) -> list[EventInterval]:
    """按 bucket 拉取事件，并裁剪到请求时间窗口。"""
    if normalized_bucket.time_end is not None and normalized_bucket.time_end < start:
        return []
    if normalized_bucket.time_start is not None and normalized_bucket.time_start > end:
        return []

    raw_events = client.get_events(normalized_bucket.bucket_id, start=start, end=end)
    interval_events: list[EventInterval] = []

    for raw_event in raw_events:
        event_end = create_event_end(raw_event.timestamp, raw_event.duration_seconds)
        if raw_event.duration_seconds <= 0:
            if raw_event.timestamp < start or raw_event.timestamp > end:
                continue
            clamped_range = (raw_event.timestamp, raw_event.timestamp)
        else:
            clamped_range = clamp_interval(raw_event.timestamp, event_end, start, end)
        if clamped_range is None:
            continue

        clamped_start, clamped_end = clamped_range
        interval_events.append(
            EventInterval(
                event_id=raw_event.event_id,
                start=clamped_start,
                end=clamped_end,
                watcher_family=normalized_bucket.watcher_family,
                source_device=normalized_bucket.device_name,
                data=raw_event.data,
                source_buckets=(normalized_bucket.bucket_id,),
                source_priority=normalized_bucket.priority_rank,
            )
        )

    return interval_events


def deduplicate_events(events: list[EventInterval]) -> list[EventInterval]:
    """对同设备同 family 事件做稳定去重，并合并来源 bucket。"""
    deduped_events: dict[tuple[object, ...], EventInterval] = {}

    for event in sorted(events, key=lambda item: (item.start, item.end, item.source_priority)):
        event_key = (
            event.source_device,
            event.watcher_family,
            event.start,
            event.end,
            build_event_signature(event.watcher_family, event.data),
        )
        existing_event = deduped_events.get(event_key)
        if existing_event is None:
            deduped_events[event_key] = event
            continue

        # 同一事件来自多个物理 bucket 时，把来源合并并保留更高优先级版本。
        merged_buckets = tuple(sorted(set(existing_event.source_buckets) | set(event.source_buckets)))
        preferred_event = existing_event if existing_event.source_priority <= event.source_priority else event
        deduped_events[event_key] = EventInterval(
            event_id=preferred_event.event_id,
            start=preferred_event.start,
            end=preferred_event.end,
            watcher_family=preferred_event.watcher_family,
            source_device=preferred_event.source_device,
            data=dict(preferred_event.data),
            source_buckets=merged_buckets,
            active_devices=preferred_event.active_devices,
            source_priority=min(existing_event.source_priority, event.source_priority),
        )

    return sorted(deduped_events.values(), key=lambda item: (item.start, item.end, item.source_priority))


def build_event_signature(watcher_family: str, data: dict[str, object]) -> tuple[object, ...]:
    """根据 watcher family 生成事件内容签名。"""
    if watcher_family == "afk":
        return (data.get("status"),)
    if watcher_family == "window":
        return (data.get("app"), data.get("title"))
    if watcher_family == "web":
        return (data.get("url"), data.get("title"))
    if watcher_family == "vscode":
        # vscode 单轨里需要把停留/编辑状态也纳入签名，避免清洗时把两者误判成同类事件。
        return (data.get("eventName"), data.get("project"), data.get("file"), data.get("activityKind"))
    if watcher_family == "agent":
        return (data.get("eventName"), data.get("conversationId"), data.get("body"))

    return tuple(sorted((key, repr(value)) for key, value in data.items()))
