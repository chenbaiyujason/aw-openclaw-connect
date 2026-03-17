from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re

from aw_client.models import EffectiveTimeSlice, EventInterval


FRACTIONAL_SECONDS_PATTERN = re.compile(r"(\.\d{6})\d+")


def parse_aw_timestamp(value: str | None) -> datetime | None:
    """把 ActivityWatch 常见的 ISO 时间字符串解析为 UTC aware datetime。"""
    if not value:
        return None

    normalized_value = FRACTIONAL_SECONDS_PATTERN.sub(r"\1", value.replace("Z", "+00:00"))
    parsed_value = datetime.fromisoformat(normalized_value)
    if parsed_value.tzinfo is None:
        return parsed_value.replace(tzinfo=timezone.utc)
    return parsed_value.astimezone(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """统一把时间转成 UTC，避免跨设备时区差异干扰比较。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def clamp_interval(
    start: datetime,
    end: datetime,
    clamp_start: datetime,
    clamp_end: datetime,
) -> tuple[datetime, datetime] | None:
    """把区间裁剪到查询时间窗口内。"""
    bounded_start = max(ensure_utc(start), ensure_utc(clamp_start))
    bounded_end = min(ensure_utc(end), ensure_utc(clamp_end))
    if bounded_start >= bounded_end:
        return None
    return bounded_start, bounded_end


def merge_touching_slices(slices: list[EffectiveTimeSlice]) -> list[EffectiveTimeSlice]:
    """合并同一设备下相邻或重叠的时间片。"""
    if not slices:
        return []

    ordered_slices = sorted(slices, key=lambda item: (item.start, item.end))
    merged_slices: list[EffectiveTimeSlice] = [ordered_slices[0]]

    for current_slice in ordered_slices[1:]:
        previous_slice = merged_slices[-1]
        if current_slice.start <= previous_slice.end and current_slice.active_devices == previous_slice.active_devices:
            merged_slices[-1] = EffectiveTimeSlice(
                start=previous_slice.start,
                end=max(previous_slice.end, current_slice.end),
                active_devices=tuple(sorted(set(previous_slice.active_devices) | set(current_slice.active_devices))),
                source_buckets=tuple(sorted(set(previous_slice.source_buckets) | set(current_slice.source_buckets))),
            )
            continue

        merged_slices.append(current_slice)

    return merged_slices


def build_labeled_slices(
    intervals_by_label: dict[str, list[tuple[datetime, datetime, tuple[str, ...]]]],
) -> list[EffectiveTimeSlice]:
    """把多设备区间转成带 active_devices 标记的并集时间片。"""
    boundaries: list[datetime] = []
    starts_by_time: dict[datetime, list[str]] = defaultdict(list)
    ends_by_time: dict[datetime, list[str]] = defaultdict(list)
    buckets_by_label: dict[str, set[str]] = defaultdict(set)

    for label, intervals in intervals_by_label.items():
        for start, end, source_buckets in intervals:
            if start >= end:
                continue
            boundaries.append(start)
            boundaries.append(end)
            starts_by_time[start].append(label)
            ends_by_time[end].append(label)
            buckets_by_label[label].update(source_buckets)

    ordered_boundaries = sorted(set(boundaries))
    active_labels: set[str] = set()
    merged_slices: list[EffectiveTimeSlice] = []

    for index, boundary in enumerate(ordered_boundaries[:-1]):
        for label in starts_by_time.get(boundary, []):
            active_labels.add(label)

        next_boundary = ordered_boundaries[index + 1]
        if active_labels and boundary < next_boundary:
            merged_slices.append(
                EffectiveTimeSlice(
                    start=boundary,
                    end=next_boundary,
                    active_devices=tuple(sorted(active_labels)),
                    source_buckets=tuple(
                        sorted(
                            {
                                bucket_id
                                for label in active_labels
                                for bucket_id in buckets_by_label.get(label, set())
                            }
                        )
                    ),
                )
            )

        for label in ends_by_time.get(next_boundary, []):
            active_labels.discard(label)

    return merge_touching_slices(merged_slices)


def clip_event_to_slices(
    event: EventInterval,
    valid_slices: list[EffectiveTimeSlice],
) -> list[EventInterval]:
    """把事件裁剪到有效时间片内，保留活跃设备信息。"""
    clipped_events: list[EventInterval] = []

    for valid_slice in valid_slices:
        if event.start == event.end:
            if valid_slice.start <= event.start <= valid_slice.end:
                clipped_events.append(
                    EventInterval(
                        event_id=event.event_id,
                        start=event.start,
                        end=event.end,
                        watcher_family=event.watcher_family,
                        source_device=event.source_device,
                        data=dict(event.data),
                        source_buckets=tuple(sorted(set(event.source_buckets) | set(valid_slice.source_buckets))),
                        active_devices=valid_slice.active_devices,
                        source_priority=event.source_priority,
                    )
                )
            continue

        clipped_range = clamp_interval(event.start, event.end, valid_slice.start, valid_slice.end)
        if clipped_range is None:
            continue

        clipped_start, clipped_end = clipped_range
        clipped_events.append(
            EventInterval(
                event_id=event.event_id,
                start=clipped_start,
                end=clipped_end,
                watcher_family=event.watcher_family,
                source_device=event.source_device,
                data=dict(event.data),
                source_buckets=tuple(sorted(set(event.source_buckets) | set(valid_slice.source_buckets))),
                active_devices=valid_slice.active_devices,
                source_priority=event.source_priority,
            )
        )

    return clipped_events


def create_event_end(start: datetime, duration_seconds: float) -> datetime:
    """根据事件起点和时长生成结束时间。"""
    return start + timedelta(seconds=max(duration_seconds, 0.0))
