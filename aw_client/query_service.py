from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from aw_client.bucket_registry import BucketRegistry, build_bucket_registry
from aw_client.event_loader import deduplicate_events, load_events_for_family
from aw_client.intervals import build_labeled_slices, clip_event_to_slices, ensure_utc, parse_aw_timestamp
from aw_client.models import (
    DeviceActivityWindow,
    EffectiveTimeSlice,
    EventInterval,
    QueryFilters,
    QueryResult,
)
from aw_client.rest_client import ActivityWatchRestClient


class QueryService:
    """统一管理 bucket 发现、AFK 清洗与跨设备查询。"""

    def __init__(self, client: ActivityWatchRestClient | None = None) -> None:
        self.client = client or ActivityWatchRestClient()

    def discover_registry(self) -> BucketRegistry:
        """扫描当前 ActivityWatch buckets 并构建归一化索引。"""
        return build_bucket_registry(self.client.list_buckets())

    def discover_devices(self) -> dict[str, list[str]]:
        """列出逻辑设备以及每个设备拥有的 watcher family。"""
        registry = self.discover_registry()
        return {
            device_name: sorted(watcher_map)
            for device_name, watcher_map in registry.buckets_by_device.items()
        }

    def list_watchers(self, device: str | None = None) -> list[str]:
        """列出某个设备或全局的 watcher family。"""
        registry = self.discover_registry()
        return registry.watcher_families(device)

    def query_effective_time(
        self,
        start: datetime | str,
        end: datetime | str,
        devices: list[str] | None = None,
    ) -> QueryResult:
        """只返回 AFK 驱动的设备活跃时间和用户有效时间。"""
        normalized_filters = self._build_filters(start=start, end=end, devices=devices, watchers=["afk"], apply_afk_cleanup=True)
        registry = self.discover_registry()
        selected_devices = self._resolve_device_names(registry, normalized_filters.devices)
        device_windows = self._build_device_activity_windows(registry, normalized_filters.start, normalized_filters.end, selected_devices)
        user_effective_intervals = self._build_user_effective_intervals(device_windows)

        return QueryResult(
            filters=normalized_filters,
            buckets_by_device=self._select_buckets_view(registry, selected_devices, normalized_filters.watchers),
            device_activity_windows=device_windows,
            user_effective_intervals=user_effective_intervals,
            cleaned_events=[],
        )

    def query_events(
        self,
        start: datetime | str,
        end: datetime | str,
        devices: list[str] | None = None,
        watchers: list[str] | None = None,
        apply_afk_cleanup: bool = True,
    ) -> QueryResult:
        """返回清洗后的应用事件。"""
        normalized_filters = self._build_filters(
            start=start,
            end=end,
            devices=devices,
            watchers=watchers,
            apply_afk_cleanup=apply_afk_cleanup,
        )
        return self.query_events_for_filters(normalized_filters)

    def query_events_for_filters(self, filters: QueryFilters) -> QueryResult:
        """按已经归一化的过滤条件执行查询。"""
        normalized_filters = filters
        registry = self.discover_registry()
        selected_devices = self._resolve_device_names(registry, normalized_filters.devices)
        selected_watchers = self._resolve_watcher_families(registry, selected_devices, normalized_filters.watchers)
        buckets_view = self._select_buckets_view(registry, selected_devices, selected_watchers)

        device_windows = self._build_device_activity_windows(registry, normalized_filters.start, normalized_filters.end, selected_devices)
        user_effective_intervals = self._build_user_effective_intervals(device_windows)
        device_slices_map = {
            window.device_name: window.active_intervals
            for window in device_windows
        }

        all_cleaned_events: list[EventInterval] = []
        for watcher_family in selected_watchers:
            if watcher_family == "afk":
                continue

            events_by_device = load_events_for_family(
                client=self.client,
                registry=registry,
                start=normalized_filters.start,
                end=normalized_filters.end,
                device_names=selected_devices,
                watcher_family=watcher_family,
            )
            cleaned_events = self._apply_event_filters(
                events_by_device=events_by_device,
                device_slices_map=device_slices_map,
                apply_afk_cleanup=normalized_filters.apply_afk_cleanup,
            )
            all_cleaned_events.extend(cleaned_events)

        return QueryResult(
            filters=normalized_filters,
            buckets_by_device=buckets_view,
            device_activity_windows=device_windows,
            user_effective_intervals=user_effective_intervals,
            cleaned_events=sorted(
                deduplicate_events(all_cleaned_events),
                key=lambda item: (item.start, item.end, item.watcher_family, item.source_device),
            ),
        )

    def query_cross(
        self,
        device_filters: list[str] | None,
        watcher_filters: list[str] | None,
        start: datetime | str,
        end: datetime | str,
        group_by: str | None = None,
    ) -> QueryResult:
        """交叉查询统一入口，当前先复用 query_events 的结构化结果。"""
        _ = group_by
        return self.query_events(
            start=start,
            end=end,
            devices=device_filters,
            watchers=watcher_filters,
            apply_afk_cleanup=True,
        )

    def _build_filters(
        self,
        start: datetime | str,
        end: datetime | str,
        devices: list[str] | None = None,
        watchers: list[str] | None = None,
        apply_afk_cleanup: bool = True,
    ) -> QueryFilters:
        """统一把输入参数归一化为强类型查询过滤器。"""
        normalized_start = self._coerce_datetime(start)
        normalized_end = self._coerce_datetime(end)
        if normalized_start >= normalized_end:
            raise ValueError("查询开始时间必须早于结束时间。")

        return QueryFilters(
            start=normalized_start,
            end=normalized_end,
            devices=tuple(sorted(devices or [])),
            watchers=tuple(sorted(watchers or [])),
            apply_afk_cleanup=apply_afk_cleanup,
        )

    def _coerce_datetime(self, value: datetime | str) -> datetime:
        """同时兼容 datetime 与 ISO 字符串输入。"""
        if isinstance(value, datetime):
            return ensure_utc(value)

        parsed_value = parse_aw_timestamp(value)
        if parsed_value is None:
            raise ValueError(f"无法解析时间值: {value}")
        return parsed_value

    def _resolve_device_names(self, registry: BucketRegistry, requested_devices: tuple[str, ...]) -> list[str]:
        """解析最终要查询的设备集合。"""
        if requested_devices:
            return [device_name for device_name in requested_devices if device_name in registry.buckets_by_device]
        return registry.device_names()

    def _resolve_watcher_families(
        self,
        registry: BucketRegistry,
        selected_devices: list[str],
        requested_watchers: tuple[str, ...],
    ) -> list[str]:
        """解析最终要查询的 watcher family 集合。"""
        if requested_watchers:
            return list(requested_watchers)

        watchers: set[str] = set()
        for device_name in selected_devices:
            watchers.update(registry.buckets_by_device.get(device_name, {}))
        return sorted(watchers)

    def _select_buckets_view(
        self,
        registry: BucketRegistry,
        selected_devices: list[str],
        selected_watchers: tuple[str, ...] | list[str],
    ) -> dict[str, dict[str, list]]:
        """裁剪出本次查询相关的 bucket 视图。"""
        selected_watcher_set = set(selected_watchers)
        buckets_view: dict[str, dict[str, list]] = {}

        for device_name in selected_devices:
            family_map = registry.buckets_by_device.get(device_name, {})
            buckets_view[device_name] = {
                family_name: buckets
                for family_name, buckets in family_map.items()
                if not selected_watcher_set or family_name in selected_watcher_set
            }

        return buckets_view

    def _build_device_activity_windows(
        self,
        registry: BucketRegistry,
        start: datetime,
        end: datetime,
        device_names: list[str],
    ) -> list[DeviceActivityWindow]:
        """按设备构建 AFK 清洗后的活跃时间窗口。"""
        afk_events_by_device = load_events_for_family(
            client=self.client,
            registry=registry,
            start=start,
            end=end,
            device_names=device_names,
            watcher_family="afk",
        )

        device_windows: list[DeviceActivityWindow] = []
        for device_name in device_names:
            active_intervals = resolve_device_active_intervals(afk_events_by_device.get(device_name, []))
            device_windows.append(
                DeviceActivityWindow(
                    device_name=device_name,
                    active_intervals=active_intervals,
                )
            )

        return device_windows

    def _build_user_effective_intervals(
        self,
        device_windows: list[DeviceActivityWindow],
    ) -> list[EffectiveTimeSlice]:
        """按跨设备并集生成用户有效时间。"""
        intervals_by_device: dict[str, list[tuple[datetime, datetime, tuple[str, ...]]]] = {}
        for device_window in device_windows:
            intervals_by_device[device_window.device_name] = [
                (interval.start, interval.end, interval.source_buckets)
                for interval in device_window.active_intervals
            ]

        return build_labeled_slices(intervals_by_device)

    def _apply_event_filters(
        self,
        events_by_device: dict[str, list[EventInterval]],
        device_slices_map: dict[str, list[EffectiveTimeSlice]],
        apply_afk_cleanup: bool,
    ) -> list[EventInterval]:
        """根据设备活跃时间裁剪事件。"""
        cleaned_events: list[EventInterval] = []

        for device_name, device_events in events_by_device.items():
            if not apply_afk_cleanup:
                cleaned_events.extend(device_events)
                continue

            device_slices = device_slices_map.get(device_name, [])
            for device_event in device_events:
                cleaned_events.extend(clip_event_to_slices(device_event, device_slices))

        return cleaned_events


def resolve_device_active_intervals(afk_events: list[EventInterval]) -> list[EffectiveTimeSlice]:
    """对单设备 AFK 事件做冲突解析，产出最终活跃区间。"""
    if not afk_events:
        return []

    boundaries = sorted({event.start for event in afk_events} | {event.end for event in afk_events})
    resolved_slices: list[EffectiveTimeSlice] = []

    for current_start, current_end in zip(boundaries, boundaries[1:], strict=False):
        if current_start >= current_end:
            continue

        covering_events = [
            event
            for event in afk_events
            if event.start <= current_start and event.end >= current_end
        ]
        if not covering_events:
            continue

        chosen_event = min(
            covering_events,
            key=lambda item: (
                item.source_priority,
                -item.start.timestamp(),
                -item.end.timestamp(),
            ),
        )
        if chosen_event.data.get("status") != "not-afk":
            continue

        resolved_slices.append(
            EffectiveTimeSlice(
                start=current_start,
                end=current_end,
                active_devices=(chosen_event.source_device,),
                source_buckets=chosen_event.source_buckets,
            )
        )

    return merge_device_slices(resolved_slices)


def merge_device_slices(slices: list[EffectiveTimeSlice]) -> list[EffectiveTimeSlice]:
    """合并单设备内部连续的活跃时间片。"""
    if not slices:
        return []

    ordered_slices = sorted(slices, key=lambda item: (item.start, item.end))
    merged_slices: list[EffectiveTimeSlice] = [ordered_slices[0]]

    for current_slice in ordered_slices[1:]:
        previous_slice = merged_slices[-1]
        if current_slice.start <= previous_slice.end:
            merged_slices[-1] = EffectiveTimeSlice(
                start=previous_slice.start,
                end=max(previous_slice.end, current_slice.end),
                active_devices=previous_slice.active_devices,
                source_buckets=tuple(sorted(set(previous_slice.source_buckets) | set(current_slice.source_buckets))),
            )
            continue

        merged_slices.append(current_slice)

    return merged_slices
