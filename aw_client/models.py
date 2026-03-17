from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# 统一约束 bucket 来源，避免后续逻辑散落字符串字面量。
BucketSourceKind = Literal["local", "synced"]


@dataclass(slots=True)
class BucketDescriptor:
    """ActivityWatch `/buckets` 接口返回的单个 bucket 元数据。"""

    bucket_id: str
    bucket_type: str
    client: str
    hostname: str
    created_at: datetime | None
    data: dict[str, object] = field(default_factory=dict)
    metadata_start: datetime | None = None
    metadata_end: datetime | None = None


@dataclass(slots=True)
class NormalizedBucket:
    """按逻辑设备与 watcher family 归一化后的 bucket 描述。"""

    bucket_id: str
    watcher_family: str
    device_name: str
    source_kind: BucketSourceKind
    sync_origin: str | None
    hostname: str
    client: str
    bucket_type: str
    time_start: datetime | None
    time_end: datetime | None
    is_current_device_candidate: bool
    priority_rank: int


@dataclass(slots=True)
class RawEvent:
    """ActivityWatch `/events` 返回的原始事件。"""

    bucket_id: str
    event_id: int | str | None
    timestamp: datetime
    duration_seconds: float
    data: dict[str, object] = field(default_factory=dict)

    @property
    def end(self) -> datetime:
        """计算事件结束时间，统一后续时间区间逻辑。"""
        from datetime import timedelta

        return self.timestamp + timedelta(seconds=self.duration_seconds)


@dataclass(slots=True)
class EventInterval:
    """归一化后的通用事件区间。"""

    event_id: int | str | None
    start: datetime
    end: datetime
    watcher_family: str
    source_device: str
    data: dict[str, object] = field(default_factory=dict)
    source_buckets: tuple[str, ...] = field(default_factory=tuple)
    active_devices: tuple[str, ...] = field(default_factory=tuple)
    source_priority: int = 0

    @property
    def duration_seconds(self) -> float:
        """统一从起止时间反推时长，避免多处重复存储。"""
        return max((self.end - self.start).total_seconds(), 0.0)


@dataclass(slots=True)
class EffectiveTimeSlice:
    """清洗后保留的有效时间片。"""

    start: datetime
    end: datetime
    active_devices: tuple[str, ...] = field(default_factory=tuple)
    source_buckets: tuple[str, ...] = field(default_factory=tuple)

    @property
    def duration_seconds(self) -> float:
        """返回时间片长度，方便汇总统计。"""
        return max((self.end - self.start).total_seconds(), 0.0)


@dataclass(slots=True)
class DeviceActivityWindow:
    """单设备 AFK 清洗后的活跃窗口集合。"""

    device_name: str
    active_intervals: list[EffectiveTimeSlice] = field(default_factory=list)

    @property
    def total_active_seconds(self) -> float:
        """累加单设备活跃时长。"""
        return sum(interval.duration_seconds for interval in self.active_intervals)


@dataclass(slots=True)
class QueryFilters:
    """统一承载查询入参，避免接口间参数风格不一致。"""

    start: datetime
    end: datetime
    devices: tuple[str, ...] = field(default_factory=tuple)
    watchers: tuple[str, ...] = field(default_factory=tuple)
    apply_afk_cleanup: bool = True
    agent_bypass: bool = False


@dataclass(slots=True)
class QueryResult:
    """统一查询结果结构，便于日志导出与测试断言。"""

    filters: QueryFilters
    buckets_by_device: dict[str, dict[str, list[NormalizedBucket]]] = field(default_factory=dict)
    device_activity_windows: list[DeviceActivityWindow] = field(default_factory=list)
    user_effective_intervals: list[EffectiveTimeSlice] = field(default_factory=list)
    cleaned_events: list[EventInterval] = field(default_factory=list)

    @property
    def user_effective_seconds(self) -> float:
        """按跨设备并集计算后的用户有效总时长。"""
        return sum(interval.duration_seconds for interval in self.user_effective_intervals)
