from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import importlib
import io
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlparse

from aw_client.models import EventInterval, QueryResult
from aw_client.query_service import QueryService
from aw_client.rest_client import ActivityWatchRestClient


# 允许同类事件在短时间断档内继续合并，减少 AW 切片造成的碎片化。
MAX_MERGE_GAP_SECONDS = 300.0
# 对 window 事件做轻量防抖，过滤掉过短的焦点抖动。
MIN_WINDOW_SEGMENT_SECONDS = 5.0
# 对 web 事件做更轻的防抖，去掉明显无意义的瞬时闪烁。
MIN_WEB_SEGMENT_SECONDS = 1.0
EDITOR_WINDOW_SUBJECTS = {"Cursor", "Cursor.exe", "Code", "Code.exe", "Visual Studio Code", "Code - OSS"}
BROWSER_WINDOW_SUBJECTS = {
    "Google Chrome",
    "Chrome",
    "Chromium",
    "Arc",
    "Safari",
    "Microsoft Edge",
    "Edge",
    "Firefox",
}

ExportItem = TypedDict("ExportItem", {"content": str, "d(s)": float})
ExportEvent = TypedDict(
    "ExportEvent",
    {
        "start": str,
        "end": str,
        "o(s)": float,
        "d(s)": float,
        "device": str,
        "watcher": str,
        "subject": str,
        "items": list[ExportItem],
    },
    total=False,
)
ExportMeta = TypedDict(
    "ExportMeta",
    {
        "start": str,
        "end": str,
        "apply_afk_cleanup": bool,
        "device_map": dict[str, str],
        "watchers": list[str],
        "event_count": int,
        "user_effective_seconds": float,
    },
)
ExportPayload = TypedDict(
    "ExportPayload",
    {
        "meta": ExportMeta,
        "events": list[ExportEvent],
    },
)


def export_recent_cleaned_log(
    output_path: str | Path | None = None,
    client: ActivityWatchRestClient | None = None,
    now: datetime | None = None,
    minutes: int = 240,
    devices: list[str] | None = None,
    watchers: list[str] | None = None,
    apply_afk_cleanup: bool = True,
) -> Path:
    """导出最近一段时间的清洗结果到日志文件。"""
    now_value = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    start_value = now_value - timedelta(minutes=minutes)
    return export_cleaned_log(
        output_path=output_path,
        client=client,
        start=start_value,
        end=now_value,
        devices=devices,
        watchers=watchers,
        apply_afk_cleanup=apply_afk_cleanup,
    )

def export_cleaned_log(
    output_path: str | Path | None = None,
    client: ActivityWatchRestClient | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    devices: list[str] | None = None,
    watchers: list[str] | None = None,
    apply_afk_cleanup: bool = True,
) -> Path:
    """按统一过滤条件导出清洗结果。"""
    if start is None or end is None:
        raise ValueError("导出清洗结果时必须同时提供 start 和 end。")

    query_service = QueryService(client=client)
    query_result = query_service.query_events(
        start=start,
        end=end,
        devices=devices,
        watchers=watchers,
        apply_afk_cleanup=apply_afk_cleanup,
    )
    return write_query_result(
        query_result=query_result,
        output_path=output_path,
    )


def write_query_result(
    query_result: QueryResult,
    output_path: str | Path | None = None,
) -> Path:
    """把查询结果写入 agent 友好的 CSV 文件。"""
    log_path = Path(output_path) if output_path is not None else _default_log_path(query_result)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    written_content = render_query_result(query_result)
    _written_chars = log_path.write_text(written_content, encoding="utf-8")
    return log_path


def export_last_4h_cleaned_log(
    output_path: str | Path | None = None,
    client: ActivityWatchRestClient | None = None,
    now: datetime | None = None,
) -> Path:
    """兼容旧接口，默认导出最近 4 小时。"""
    return export_recent_cleaned_log(
        output_path=output_path,
        client=client,
        now=now,
        minutes=240,
    )


def render_query_result(query_result: QueryResult) -> str:
    """把查询结果渲染成单文件 CSV，头部携带压缩 meta。"""
    serializable_payload = build_agent_friendly_payload(query_result)
    return render_agent_friendly_csv(serializable_payload)


def build_agent_friendly_payload(query_result: QueryResult) -> ExportPayload:
    """构建给 agent 使用的低重复、低噪声结果。"""
    cleaned_events = query_result.cleaned_events
    devices = sorted({event.source_device for event in cleaned_events})
    base_start = query_result.filters.start
    device_code_map = {
        device_name: _index_to_device_code(index)
        for index, device_name in enumerate(devices)
    }
    watcher_families = sorted({event.watcher_family for event in cleaned_events})
    serialized_events = [_serialize_atomic_event(event, device_code_map) for event in cleaned_events]
    fused_atomic_events = fuse_vscode_with_window_events(serialized_events)
    # 先对原子级事件防抖，避免 web/window 的微小脉冲先被粗合并放大。
    debounced_atomic_events = apply_atomic_debounce(fused_atomic_events)
    collapsed_events = collapse_adjacent_events(debounced_atomic_events)
    # 对已成段的 Cursor window 再做一次吸收，处理原子级阶段没机会合并的残余壳子段。
    absorbed_editor_windows = absorb_editor_window_segments(collapsed_events)
    recollapsed_events = collapse_adjacent_events(absorbed_editor_windows)
    browser_deduplicated_events = deduplicate_browser_window_segments(recollapsed_events)
    final_events = collapse_adjacent_events(browser_deduplicated_events)
    relative_events = [_convert_event_to_relative_time(event, base_start) for event in final_events]

    return {
        "meta": {
            "start": _serialize_datetime(query_result.filters.start),
            "end": _serialize_datetime(query_result.filters.end),
            "apply_afk_cleanup": query_result.filters.apply_afk_cleanup,
            "device_map": {
                device_code_map[device_name]: device_name
                for device_name in devices
            },
            "watchers": watcher_families,
            "event_count": len(relative_events),
            "user_effective_seconds": round(query_result.user_effective_seconds, 3),
        },
        "events": relative_events,
    }


def render_agent_friendly_csv(payload: ExportPayload) -> str:
    """把 agent 友好的结构渲染成单文件 CSV。"""
    meta = payload["meta"]
    events = payload["events"]

    csv_buffer = io.StringIO()
    csv_writer = csv.writer(csv_buffer)
    csv_writer.writerow(["o(s)", "d(s)", "dev", "w", "sub", "items"])
    for event in events:
        csv_writer.writerow(
            [
                event.get("o(s)", ""),
                event.get("d(s)", ""),
                event.get("device", ""),
                event.get("watcher", ""),
                event.get("subject", ""),
                _render_items_field(event.get("items", [])),
            ]
        )

    csv_body = csv_buffer.getvalue()
    header_lines = [
        f"# tok_est,{_estimate_openai_tokens(csv_body)}",
        f"# start,{meta.get('start', '')}",
        f"# end,{meta.get('end', '')}",
        f"# afk,{_bool_to_int(meta.get('apply_afk_cleanup'))}",
        f"# dm,{_render_device_map(meta.get('device_map'))}",
        f"# ws,{_render_string_list(meta.get('watchers'))}",
        f"# ec,{meta.get('event_count', 0)}",
        f"# ues,{meta.get('user_effective_seconds', 0)}",
    ]
    return "\n".join(header_lines) + "\n" + csv_body


def _serialize_atomic_event(
    event: EventInterval,
    device_code_map: dict[str, str],
) -> ExportEvent:
    """把单条原子事件序列化为可进一步合并的段。"""
    subject_value, content_value = _extract_subject_and_content(event)
    serialized_event: ExportEvent = {
        "start": _serialize_datetime(event.start),
        "end": _serialize_datetime(event.end),
        "d(s)": round(event.duration_seconds, 3),
        "device": device_code_map.get(event.source_device, event.source_device),
        "watcher": event.watcher_family,
        "subject": subject_value,
        "items": [
            {
                "content": content_value,
                "d(s)": round(event.duration_seconds, 3),
            }
        ],
    }
    return serialized_event


def _serialize_datetime(value: datetime) -> str:
    """把 datetime 稳定序列化为 UTC ISO 字符串。"""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bool_to_int(value: object) -> int:
    """把布尔值压缩成 0/1。"""
    return 1 if value is True else 0


def _render_device_map(value: dict[str, str]) -> str:
    """把设备映射压成单行字符串。"""
    parts: list[str] = []
    for key, item in value.items():
        parts.append(f"{key}={item}")
    return "|".join(parts)


def _render_string_list(value: list[str]) -> str:
    """把字符串列表压成单行。"""
    return "|".join(str(item) for item in value)


def _render_items_field(value: list[ExportItem]) -> str:
    """把 items 列表压成 CSV 单字段。"""
    parts: list[str] = []
    for item in value:
        content_value = item["content"]
        duration_value = item["d(s)"]
        parts.append(f"{content_value}:{duration_value}")
    return "|".join(parts)


def _should_hide_redundant_items(event: ExportEvent) -> bool:
    """如果 item 只是把 subject 和总时长重复了一遍，就不再输出 items。"""
    items = event.get("items", [])
    subject = event.get("subject")
    duration = event.get("d(s)")
    if len(items) != 1:
        return False

    only_item = items[0]
    return only_item["content"] == subject and only_item["d(s)"] == duration


def _estimate_openai_tokens(text: str) -> int:
    """估算 OpenAI token 数，优先用 tiktoken，缺失时回退到字符估算。"""
    try:
        tiktoken_module = importlib.import_module("tiktoken")
        get_encoding = getattr(tiktoken_module, "get_encoding", None)
        if callable(get_encoding):
            encoding = get_encoding("o200k_base")
            encode = getattr(encoding, "encode", None)
            if callable(encode):
                return len(encode(text))
    except Exception:
        pass

    # 经验值：当前这类中英混合 CSV 大约每 2.4 个字符 1 token。
    return max(1, round(len(text) / 2.4))


def _convert_event_to_relative_time(
    event: ExportEvent,
    base_start: datetime,
) -> ExportEvent:
    """把事件起止时间改写成相对 meta.start 的偏移秒。"""
    relative_event = _clone_event_segment(event)
    start_value = relative_event.pop("start", None)
    end_value = relative_event.pop("end", None)
    if not isinstance(start_value, str) or not isinstance(end_value, str):
        raise ValueError("事件缺少 start/end，无法转换为相对时间。")

    relative_event["o(s)"] = round(
        _parse_serialized_datetime(start_value).timestamp() - base_start.astimezone(timezone.utc).timestamp(),
        3,
    )
    if _should_hide_redundant_items(relative_event):
        relative_event["items"] = []
    return relative_event


def collapse_adjacent_events(events: list[ExportEvent]) -> list[ExportEvent]:
    """把相邻同类事件压缩成单段，并在段内保留内容列表。"""
    if not events:
        return []

    collapsed_events: list[ExportEvent] = [_clone_event_segment(events[0])]

    for current_event in events[1:]:
        previous_event = collapsed_events[-1]
        if _can_merge_adjacent_events(previous_event, current_event):
            previous_event["end"] = current_event["end"]
            _append_or_merge_item(previous_event, current_event)
            # 段时长使用 items 实际时长总和，而不是时间包络跨度，避免稀疏采样被放大。
            previous_event["d(s)"] = _sum_item_durations(previous_event.get("items", []))
            continue

        collapsed_events.append(_clone_event_segment(current_event))

    return collapsed_events


def apply_atomic_debounce(events: list[ExportEvent]) -> list[ExportEvent]:
    """按 watcher 类型过滤过短原子事件，减少后续合并把噪声放大。"""
    filtered_events: list[ExportEvent] = []
    for event in events:
        event_watcher = event.get("watcher")
        event_duration = float(event.get("d(s)", 0.0))

        if event_watcher == "window" and event_duration < MIN_WINDOW_SEGMENT_SECONDS:
            continue
        if event_watcher == "web" and event_duration <= MIN_WEB_SEGMENT_SECONDS:
            continue
        filtered_events.append(event)
    return filtered_events


def absorb_editor_window_segments(events: list[ExportEvent]) -> list[ExportEvent]:
    """对已压缩成段的编辑器 window 继续吸收，尽量不把 Cursor 壳子单独暴露给 agent。"""
    absorbed_events: list[ExportEvent] = []
    index = 0

    while index < len(events):
        current_event = _clone_event_segment(events[index])
        next_event = events[index + 1] if index + 1 < len(events) else None

        if _is_editor_window_event(current_event):
            merge_direction = _pick_editor_window_merge_direction(
                window_event=current_event,
                previous_event=absorbed_events[-1] if absorbed_events else None,
                next_event=next_event,
            )
            if merge_direction == "previous" and absorbed_events:
                absorbed_events[-1] = _merge_window_into_vscode(absorbed_events[-1], current_event)
                index += 1
                continue
            if merge_direction == "next" and next_event is not None:
                absorbed_events.append(
                    _merge_window_into_vscode(
                        _clone_event_segment(next_event),
                        current_event,
                    )
                )
                index += 2
                continue

        absorbed_events.append(current_event)
        index += 1

    return absorbed_events


def deduplicate_browser_window_segments(events: list[ExportEvent]) -> list[ExportEvent]:
    """让浏览器 window 只和最近的 web 段做一次去重，优先保留更具体的 web。"""
    deduplicated_events: list[ExportEvent] = []
    consumed_web_indices: set[int] = set()

    for index, event in enumerate(events):
        if not _is_browser_window_event(event):
            deduplicated_events.append(_clone_event_segment(event))
            continue

        nearest_web_index = _pick_nearest_web_neighbor_index(
            events=events,
            window_index=index,
            consumed_web_indices=consumed_web_indices,
        )
        if nearest_web_index is None:
            deduplicated_events.append(_clone_event_segment(event))
            continue

        consumed_web_indices.add(nearest_web_index)

    return deduplicated_events


def fuse_vscode_with_window_events(events: list[ExportEvent]) -> list[ExportEvent]:
    """把编辑器 window 事件优先并入相邻 vscode 事件，尽量不单独输出 Cursor 壳子。"""
    fused_events: list[ExportEvent] = []
    index = 0

    while index < len(events):
        current_event = _clone_event_segment(events[index])
        next_event = events[index + 1] if index + 1 < len(events) else None

        # 对编辑器壳子事件做更激进的吸收：优先并入最近的相邻 vscode 事件。
        if _is_editor_window_event(current_event):
            merge_direction = _pick_editor_window_merge_direction(
                window_event=current_event,
                previous_event=fused_events[-1] if fused_events else None,
                next_event=next_event,
            )
            if merge_direction == "previous" and fused_events:
                fused_events[-1] = _merge_window_into_vscode(fused_events[-1], current_event)
                index += 1
                continue
            if merge_direction == "next" and next_event is not None:
                merged_vscode_event = _merge_window_into_vscode(
                    _clone_event_segment(next_event),
                    current_event,
                )
                index += 2
                while index < len(events):
                    following_event = events[index]
                    if not _should_fuse_window_into_vscode(merged_vscode_event, following_event):
                        break
                    merged_vscode_event = _merge_window_into_vscode(
                        merged_vscode_event,
                        following_event,
                    )
                    index += 1
                fused_events.append(merged_vscode_event)
                continue

        if fused_events:
            previous_event = fused_events[-1]
            if _should_fuse_window_into_vscode(previous_event, current_event):
                fused_events[-1] = _merge_window_into_vscode(previous_event, current_event)
                index += 1
                continue

        if current_event.get("watcher") == "vscode":
            while index + 1 < len(events):
                next_event = events[index + 1]
                if not _should_fuse_window_into_vscode(current_event, next_event):
                    break
                current_event = _merge_window_into_vscode(current_event, next_event)
                index += 1

        fused_events.append(current_event)
        index += 1

    return fused_events


def _can_merge_adjacent_events(
    previous_event: ExportEvent,
    current_event: ExportEvent,
) -> bool:
    """判断两段事件是否属于同一段连续行为。"""
    if _segment_signature(previous_event) != _segment_signature(current_event):
        return False

    previous_end = _parse_serialized_datetime(previous_event["end"])
    current_start = _parse_serialized_datetime(current_event["start"])
    gap_seconds = (current_start - previous_end).total_seconds()
    return gap_seconds <= MAX_MERGE_GAP_SECONDS


def _should_fuse_window_into_vscode(vscode_event: ExportEvent, window_event: ExportEvent) -> bool:
    """判断编辑器 window 事件是否应被融合到相邻 vscode 事件。"""
    if not _is_editor_window_event(window_event):
        return False
    if vscode_event.get("watcher") != "vscode":
        return False
    if vscode_event.get("device") != window_event.get("device"):
        return False

    vscode_start = _parse_serialized_datetime(vscode_event["start"])
    vscode_end = _parse_serialized_datetime(vscode_event["end"])
    window_start = _parse_serialized_datetime(window_event["start"])
    window_end = _parse_serialized_datetime(window_event["end"])
    gap_seconds = max((window_start - vscode_end).total_seconds(), (vscode_start - window_end).total_seconds(), 0.0)
    if gap_seconds > MAX_MERGE_GAP_SECONDS:
        return False

    return True


def _merge_window_into_vscode(vscode_event: ExportEvent, window_event: ExportEvent) -> ExportEvent:
    """把原子 window 事件的时间与内容合并进 vscode 事件。"""
    merged_event = _clone_event_segment(vscode_event)
    merged_start = min(
        _parse_serialized_datetime(vscode_event["start"]),
        _parse_serialized_datetime(window_event["start"]),
    )
    merged_end = max(
        _parse_serialized_datetime(vscode_event["end"]),
        _parse_serialized_datetime(window_event["end"]),
    )
    merged_event["start"] = _serialize_datetime(merged_start)
    merged_event["end"] = _serialize_datetime(merged_end)

    window_items_by_name = {
        _normalize_item_name(item["content"]): item["d(s)"]
        for item in window_event.get("items", [])
    }
    for item in merged_event.get("items", []):
        normalized_name = _normalize_item_name(item["content"])
        if normalized_name in window_items_by_name:
            item["d(s)"] = round(float(item["d(s)"]) + float(window_items_by_name[normalized_name]), 3)

    merged_event["d(s)"] = _sum_item_durations(merged_event.get("items", []))
    return merged_event


def _segment_signature(event: ExportEvent) -> tuple[str | None, str | None, str | None]:
    """提取段级签名，决定哪些相邻事件可压缩到一起。"""
    merge_subject = cast(str | None, event.get("subject"))
    if event.get("watcher") == "web":
        merge_subject = _first_item_content(event) or merge_subject
    return (
        cast(str | None, event.get("device")),
        cast(str | None, event.get("watcher")),
        merge_subject,
    )


def _parse_serialized_datetime(value: object) -> datetime:
    """把已序列化的时间字符串解析回 datetime。"""
    if not isinstance(value, str):
        raise ValueError("序列化事件中的时间字段必须是字符串。")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _extract_subject_and_content(event: EventInterval) -> tuple[str, str]:
    """为任意 watcher 提取一个稳定主体和一个具体内容。"""
    if event.watcher_family == "window":
        subject_value = _pick_first_string(event.data, ("app",)) or "window"
        content_value = _pick_first_string(event.data, ("title",)) or subject_value
        return subject_value, content_value

    if event.watcher_family == "web":
        url_value = _pick_first_string(event.data, ("url",))
        subject_value = _extract_url_host(url_value) or "web"
        content_value = url_value or _pick_first_string(event.data, ("title",)) or subject_value
        return subject_value, content_value

    if event.watcher_family == "vscode":
        activity_kind = _normalize_vscode_activity_kind(event.data.get("activityKind"))
        project_value = _pick_first_string(event.data, ("project",)) or "unknown"
        subject_value = project_value
        content_value = _format_vscode_item_content(
            project_value=project_value,
            activity_kind=activity_kind,
            file_value=_pick_first_string(event.data, ("file",)),
            event_name=_pick_first_string(event.data, ("eventName",)),
            title_value=_pick_first_string(event.data, ("title",)),
        )
        return subject_value, content_value

    subject_value = _pick_first_string(event.data, ("app", "project", "language", "eventName", "subject", "branch")) or event.watcher_family
    content_value = _pick_first_string(event.data, ("title", "url", "file", "subject", "branch", "eventName")) or subject_value
    return subject_value, content_value


def _normalize_item_name(content_value: str) -> str:
    """把 window/vscode 的内容归一化为可比较的文件名。"""
    primary_part = content_value.split(" [", maxsplit=1)[0].split(" — ", maxsplit=1)[0].strip()
    return Path(primary_part).name.strip().lower()


def _format_vscode_item_content(
    project_value: str,
    activity_kind: str,
    file_value: str | None,
    event_name: str | None,
    title_value: str | None,
) -> str:
    """渲染 vscode item，优先用相对项目路径，并把状态下沉到 item 级别。"""
    if file_value is not None:
        display_path = _render_vscode_file_path(
            project_value=project_value,
            file_value=file_value,
        )
        return f"{display_path} [{activity_kind}]"

    fallback_content = event_name or title_value or project_value
    return f"{fallback_content} [{activity_kind}]"


def _render_vscode_file_path(project_value: str, file_value: str) -> str:
    """已知项目根目录时输出相对路径；未知项目时保留绝对路径。"""
    if not _can_render_relative_vscode_path(project_value):
        return file_value

    project_path = Path(project_value)
    file_path = Path(file_value)
    if not project_path.is_absolute() or not file_path.is_absolute():
        return file_value

    try:
        return str(file_path.relative_to(project_path))
    except ValueError:
        return file_value


def _can_render_relative_vscode_path(project_value: str) -> bool:
    """只有项目根目录明确时，才把文件路径压成相对路径。"""
    normalized_project = project_value.strip().lower()
    return bool(normalized_project) and normalized_project not in {"unknown", "vscode"}


def _is_editor_window_event(event: ExportEvent) -> bool:
    """判断事件是否是编辑器壳子类 window 事件。"""
    return event.get("watcher") == "window" and event.get("subject") in EDITOR_WINDOW_SUBJECTS


def _is_browser_window_event(event: ExportEvent) -> bool:
    """判断事件是否是浏览器窗口壳子事件。"""
    return event.get("watcher") == "window" and event.get("subject") in BROWSER_WINDOW_SUBJECTS


def _pick_editor_window_merge_direction(
    window_event: ExportEvent,
    previous_event: ExportEvent | None,
    next_event: ExportEvent | None,
) -> str | None:
    """为编辑器 window 事件选择更合适的 vscode 吸收方向。"""
    previous_gap = _event_gap_seconds(previous_event, window_event)
    next_gap = _event_gap_seconds(window_event, next_event)

    previous_match = previous_event is not None and _should_fuse_window_into_vscode(previous_event, window_event)
    next_match = next_event is not None and _should_fuse_window_into_vscode(next_event, window_event)
    if not previous_match and not next_match:
        return None
    if previous_match and not next_match:
        return "previous"
    if next_match and not previous_match:
        return "next"
    if previous_gap <= next_gap:
        return "previous"
    return "next"


def _pick_nearest_web_neighbor_index(
    events: list[ExportEvent],
    window_index: int,
    consumed_web_indices: set[int],
) -> int | None:
    """选择最近且尚未用于去重的同设备 web 邻居。"""
    window_event = events[window_index]
    previous_index = window_index - 1 if window_index > 0 else None
    next_index = window_index + 1 if window_index + 1 < len(events) else None

    candidates: list[tuple[float, int]] = []
    for neighbor_index in (previous_index, next_index):
        if neighbor_index is None or neighbor_index in consumed_web_indices:
            continue
        neighbor_event = events[neighbor_index]
        if not _can_deduplicate_browser_window(window_event, neighbor_event):
            continue
        gap_seconds = min(
            _event_gap_seconds(neighbor_event, window_event),
            _event_gap_seconds(window_event, neighbor_event),
        )
        candidates.append((gap_seconds, neighbor_index))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def _can_deduplicate_browser_window(window_event: ExportEvent, web_event: ExportEvent) -> bool:
    """判断浏览器 window 是否可被相邻 web 视图替代。"""
    if not _is_browser_window_event(window_event):
        return False
    if web_event.get("watcher") != "web":
        return False
    if window_event.get("device") != web_event.get("device"):
        return False

    gap_seconds = min(
        _event_gap_seconds(window_event, web_event),
        _event_gap_seconds(web_event, window_event),
    )
    return gap_seconds <= MAX_MERGE_GAP_SECONDS


def _event_gap_seconds(left_event: ExportEvent | None, right_event: ExportEvent | None) -> float:
    """计算两个事件之间的时间断档，缺失时返回无穷大。"""
    if left_event is None or right_event is None:
        return float("inf")
    left_end = _parse_serialized_datetime(left_event["end"])
    right_start = _parse_serialized_datetime(right_event["start"])
    return max((right_start - left_end).total_seconds(), 0.0)


def _pick_first_string(data: dict[str, object], keys: tuple[str, ...]) -> str | None:
    """从多个候选字段中选出第一个非空字符串。"""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalize_vscode_activity_kind(value: object) -> str:
    """把 vscode 单轨活动类型归一化为稳定标签，兼容旧数据缺失字段。"""
    if value == "edit":
        return "edit"
    if value == "dwell":
        return "dwell"
    return "unknown"


def _extract_url_host(url_value: str | None) -> str | None:
    """提取 URL 的 host，作为 web 段的稳定主体。"""
    if not url_value:
        return None

    parsed_url = urlparse(url_value)
    return parsed_url.netloc or None


def _clone_event_segment(event: ExportEvent) -> ExportEvent:
    """深拷贝一个事件段，避免后续合并时污染原对象。"""
    cloned_event: ExportEvent = dict(event)
    items = event.get("items", [])
    cloned_event["items"] = [dict(item) for item in items]
    return cloned_event


def _append_or_merge_item(
    previous_event: ExportEvent,
    current_event: ExportEvent,
) -> None:
    """把当前事件内容追加到段内，若内容相同则合并时长。"""
    previous_items = previous_event.get("items", [])
    current_items = current_event.get("items", [])
    if not current_items:
        return

    current_item = current_items[0]
    if previous_items:
        last_item = previous_items[-1]
        if last_item["content"] == current_item["content"]:
            last_item["d(s)"] = round(
                float(last_item["d(s)"]) + float(current_item["d(s)"]),
                3,
            )
            return

    previous_items.append(dict(current_item))


def _sum_item_durations(items: list[ExportItem]) -> float:
    """汇总 items 的真实时长，避免段时长被时间跨度放大。"""
    return round(sum(float(item["d(s)"]) for item in items), 3)


def _first_item_content(event: ExportEvent) -> str | None:
    """读取首个 item 内容，给需要更细粒度合并签名的 watcher 使用。"""
    items = event.get("items", [])
    if not items:
        return None
    return items[0]["content"]


def _index_to_device_code(index: int) -> str:
    """把设备序号转成 A、B、C...、AA 这样的短码。"""
    if index < 0:
        raise ValueError("设备序号不能为负数。")

    code_parts: list[str] = []
    remaining_index = index
    while True:
        remaining_index, remainder = divmod(remaining_index, 26)
        code_parts.append(chr(ord("A") + remainder))
        if remaining_index == 0:
            break
        remaining_index -= 1

    return "".join(reversed(code_parts))


def _default_log_path(query_result: QueryResult) -> Path:
    """根据查询时间范围生成默认日志文件路径。"""
    end_value = query_result.filters.end.astimezone(timezone.utc)
    start_value = query_result.filters.start.astimezone(timezone.utc)
    timestamp_text = end_value.strftime("%Y%m%d-%H%M%S")
    duration_minutes = round((end_value - start_value).total_seconds() / 60)
    if duration_minutes > 0 and duration_minutes % 60 == 0:
        hours = duration_minutes // 60
        return Path("logs") / f"activitywatch-cleaned-last{hours}h-{timestamp_text}.csv"
    if duration_minutes > 0:
        return Path("logs") / f"activitywatch-cleaned-last{duration_minutes}m-{timestamp_text}.csv"
    return Path("logs") / f"activitywatch-cleaned-range-{timestamp_text}.csv"
