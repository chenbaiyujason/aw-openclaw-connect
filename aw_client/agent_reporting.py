from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import shutil
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from aw_client.models import EventInterval, QueryResult


MAX_GEMINI_CONCURRENCY = 10
GEMINI_MODEL = "gemini-3-flash-preview"
AGENT_CACHE_SCHEMA_VERSION = 2
AGENT_CACHE_PATH = Path("logs") / "agent_prompt_cache.json"
AGENT_EVENT_NAME = "before_submit_prompt"
MAX_WORKSPACE_MATCH_GAP_SECONDS = 1800.0
INVALID_GLYPH_PATTERN = re.compile(r"[?\uFFFD]{3,}")
SUSPICIOUS_GLYPH_PATTERN = re.compile(r"[?\uFFFD]")
WHITESPACE_PATTERN = re.compile(r"\s+")
JSON_BLOCK_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
WINDOWS_DRIVE_PATTERN = re.compile(r"^/([a-zA-Z]:/)")
SEMANTIC_TEXT_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
MIN_MEANINGFUL_PROMPT_CHARS = 6
MIN_AGENT_SUMMARY_BODY_CHARS = 100


class AgentCacheEntry(TypedDict, total=False):
    """缓存一条对话的 Gemini 生成结果。"""

    event_id: str
    conversation_id: str
    title: str
    title_prompt_hash: str
    user_prompt: str
    summary_prompt_hash: str
    work: str
    updated_at: str


class AgentCachePayload(TypedDict):
    """落盘的缓存文件结构。"""

    version: int
    entries: dict[str, AgentCacheEntry]


@dataclass(slots=True)
class AgentPromptEvent:
    """单条 agent prompt 事件。"""

    event_id: str
    conversation_id: str
    timestamp: datetime
    source_device: str
    cleaned_body: str
    resolved_workspace: str
    workspace_roots: tuple[str, ...]


@dataclass(slots=True)
class AgentConversation:
    """按 conversationId 聚合后的对话。"""

    conversation_id: str
    first_event_id: str
    title_source_event_id: str
    title_source_body: str
    started_at: datetime
    resolved_workspace: str
    prompts: tuple[AgentPromptEvent, ...]


@dataclass(slots=True)
class AgentMessageCsvRow:
    """最终导出的逐消息 CSV 行。"""

    event_id: str
    work: str
    user_prompt: str
    title: str
    started_at: datetime


@dataclass(slots=True)
class GeminiGenerationRequest:
    """单条消息级 Gemini 生成请求。"""

    prompt_event: AgentPromptEvent
    conversation: AgentConversation
    work_label: str
    summary_prompt_hash: str
    title_prompt_hash: str | None
    needs_title: bool


@dataclass(slots=True)
class GeminiGenerationResult:
    """Gemini 返回的消息总结，以及首消息可选 title。"""

    event_id: str
    conversation_id: str
    user_prompt: str
    summary_prompt_hash: str
    title: str | None
    title_prompt_hash: str | None
    title_source_event_id: str | None
    work_label: str


def should_render_agent_csv(query_result: QueryResult) -> bool:
    """只有显式查询 agent watcher 时，才切到专用三列表导出。"""
    return set(query_result.filters.watchers) == {"agent"}


def render_agent_csv(query_result: QueryResult) -> str:
    """把 agent 生命周期事件按逐消息导出为工作、总结、标题三列表。"""
    context_events = [event for event in query_result.cleaned_events if event.watcher_family == "vscode"]
    prompt_events = _extract_agent_prompt_events(
        cleaned_events=query_result.cleaned_events,
        context_events=context_events,
    )
    if query_result.filters.agent_bypass:
        return _render_raw_agent_csv(query_result, prompt_events)
    first_prompt_by_conversation = _load_global_first_prompt_events(
        end=query_result.filters.end,
        target_conversation_ids={event.conversation_id for event in prompt_events},
    )
    conversations = _group_conversations(prompt_events, first_prompt_by_conversation)
    cache_payload = _load_agent_cache()
    rows = _resolve_csv_rows(conversations, cache_payload)
    _write_agent_cache(cache_payload)

    csv_buffer = io.StringIO()
    csv_writer = csv.writer(csv_buffer)
    csv_writer.writerow(["work", "user prompt", "title"])
    for row in sorted(rows, key=lambda item: (item.started_at, item.event_id)):
        csv_writer.writerow([row.work, row.user_prompt, row.title])
    return csv_buffer.getvalue()


def _render_raw_agent_csv(query_result: QueryResult, prompt_events: list[AgentPromptEvent]) -> str:
    """关闭预压缩时，保留 meta 与时间列，只输出 workspace/body。"""
    csv_buffer = io.StringIO()
    csv_writer = csv.writer(csv_buffer)
    csv_writer.writerow(["start", "workspace", "body"])
    for prompt_event in sorted(prompt_events, key=lambda item: (item.timestamp, item.event_id)):
        csv_writer.writerow(
            [
                _serialize_prompt_timestamp(prompt_event.timestamp),
                _workspace_to_work_label(prompt_event.resolved_workspace),
                prompt_event.cleaned_body,
            ]
        )
    csv_body = csv_buffer.getvalue()
    header_lines = [
        f"# start,{query_result.filters.start.isoformat()}",
        f"# end,{query_result.filters.end.isoformat()}",
        f"# afk,{1 if query_result.filters.apply_afk_cleanup else 0}",
        f"# ws,agent",
        f"# ec,{len(prompt_events)}",
    ]
    return "\n".join(header_lines) + "\n" + csv_body


def _extract_agent_prompt_events(
    cleaned_events: list[EventInterval],
    context_events: list[EventInterval],
) -> list[AgentPromptEvent]:
    """从通用事件流中抽出可用的 agent prompt 事件。"""
    prompt_events: list[AgentPromptEvent] = []
    for event in cleaned_events:
        if event.watcher_family != "agent":
            continue
        if event.data.get("eventName") != AGENT_EVENT_NAME:
            continue

        raw_body = event.data.get("body")
        conversation_id = event.data.get("conversationId")
        if not isinstance(raw_body, str) or not isinstance(conversation_id, str) or not conversation_id:
            continue

        cleaned_body = _clean_prompt_body(raw_body)
        if not _should_keep_prompt_event(raw_body, cleaned_body):
            continue

        workspace_roots = _extract_workspace_roots(event.data.get("workspaceRoots"))
        resolved_workspace = _resolve_workspace_for_agent_event(
            agent_event=event,
            workspace_roots=workspace_roots,
            context_events=context_events,
        )
        prompt_events.append(
            AgentPromptEvent(
                event_id=_stable_event_id(event),
                conversation_id=conversation_id,
                timestamp=event.start,
                source_device=event.source_device,
                cleaned_body=cleaned_body,
                resolved_workspace=resolved_workspace,
                workspace_roots=workspace_roots,
            )
        )

    return sorted(prompt_events, key=lambda item: (item.timestamp, item.event_id))


def _group_conversations(
    prompt_events: list[AgentPromptEvent],
    first_prompt_by_conversation: dict[str, AgentPromptEvent] | None = None,
) -> list[AgentConversation]:
    """按 conversationId 分组，并为每组确定真实工作区。"""
    grouped_events: dict[str, list[AgentPromptEvent]] = {}
    for prompt_event in prompt_events:
        grouped_events.setdefault(prompt_event.conversation_id, []).append(prompt_event)

    conversations: list[AgentConversation] = []
    for conversation_id, conversation_events in grouped_events.items():
        ordered_events = sorted(conversation_events, key=lambda item: (item.timestamp, item.event_id))
        title_source_event = (
            first_prompt_by_conversation.get(conversation_id)
            if isinstance(first_prompt_by_conversation, dict)
            else None
        ) or ordered_events[0]
        resolved_workspace = _resolve_conversation_workspace(ordered_events)
        conversations.append(
            AgentConversation(
                conversation_id=conversation_id,
                first_event_id=ordered_events[0].event_id,
                title_source_event_id=title_source_event.event_id,
                title_source_body=title_source_event.cleaned_body,
                started_at=ordered_events[0].timestamp,
                resolved_workspace=resolved_workspace,
                prompts=tuple(ordered_events),
            )
        )

    return sorted(conversations, key=lambda item: (item.started_at, item.first_event_id))


def _load_global_first_prompt_events(
    end: datetime,
    target_conversation_ids: set[str],
) -> dict[str, AgentPromptEvent]:
    """为当前导出涉及的 conversation 查找全历史第一条消息。"""
    if not target_conversation_ids:
        return {}

    from aw_client.query_service import QueryService

    query_service = QueryService()
    earliest_start = _discover_earliest_agent_start(query_service)
    full_result = query_service.query_events(
        start=earliest_start,
        end=end,
        watchers=["agent"],
        apply_afk_cleanup=False,
    )
    full_context_events = [event for event in full_result.cleaned_events if event.watcher_family == "vscode"]
    full_prompt_events = _extract_agent_prompt_events(
        cleaned_events=full_result.cleaned_events,
        context_events=full_context_events,
    )

    first_prompt_by_conversation: dict[str, AgentPromptEvent] = {}
    for prompt_event in full_prompt_events:
        if prompt_event.conversation_id not in target_conversation_ids:
            continue
        first_prompt_by_conversation.setdefault(prompt_event.conversation_id, prompt_event)
    return first_prompt_by_conversation


def _discover_earliest_agent_start(query_service) -> datetime:
    """读取 agent bucket 的最早开始时间，避免只看当前导出窗口。"""
    registry = query_service.discover_registry()
    earliest_start: datetime | None = None
    for watcher_map in registry.buckets_by_device.values():
        for bucket in watcher_map.get("agent", []):
            if bucket.time_start is None:
                continue
            if earliest_start is None or bucket.time_start < earliest_start:
                earliest_start = bucket.time_start
    if earliest_start is not None:
        return earliest_start
    return datetime.now().astimezone()


def _resolve_csv_rows(
    conversations: list[AgentConversation],
    cache_payload: AgentCachePayload,
) -> list[AgentMessageCsvRow]:
    """先命中缓存，再以消息为单位并发调用 Gemini。"""
    cache_entries = cache_payload["entries"]
    generation_requests: list[GeminiGenerationRequest] = []
    csv_rows: list[AgentMessageCsvRow] = []

    for conversation in conversations:
        first_prompt_event = conversation.prompts[0]
        title_prompt_hash = _hash_text(conversation.title_source_body)
        first_cache_entry = cache_entries.get(conversation.title_source_event_id, {})
        title_is_fresh = (
            isinstance(first_cache_entry.get("title"), str)
            and bool(first_cache_entry.get("title"))
            and first_cache_entry.get("title_prompt_hash") == title_prompt_hash
        )

        for prompt_event in conversation.prompts:
            summary_prompt_hash = _hash_text(prompt_event.cleaned_body)
            work_label = _workspace_to_work_label(prompt_event.resolved_workspace or conversation.resolved_workspace)
            cache_entry = cache_entries.get(prompt_event.event_id, {})
            summary_is_fresh = (
                isinstance(cache_entry.get("user_prompt"), str)
                and bool(cache_entry.get("user_prompt"))
                and cache_entry.get("summary_prompt_hash") == summary_prompt_hash
            )
            needs_title = prompt_event.event_id == first_prompt_event.event_id and not title_is_fresh
            if not needs_title and len(prompt_event.cleaned_body) < MIN_AGENT_SUMMARY_BODY_CHARS:
                if not summary_is_fresh:
                    cache_entries[prompt_event.event_id] = {
                        **cache_entry,
                        "event_id": prompt_event.event_id,
                        "conversation_id": conversation.conversation_id,
                        "user_prompt": prompt_event.cleaned_body,
                        "summary_prompt_hash": summary_prompt_hash,
                        "work": work_label,
                        "updated_at": datetime.utcnow().isoformat() + "Z",
                    }
                continue
            if summary_is_fresh and not needs_title:
                continue

            generation_requests.append(
                GeminiGenerationRequest(
                    prompt_event=prompt_event,
                    conversation=conversation,
                    work_label=work_label,
                    summary_prompt_hash=summary_prompt_hash,
                    title_prompt_hash=title_prompt_hash if needs_title else None,
                    needs_title=needs_title,
                )
            )

    if generation_requests:
        generation_results = asyncio.run(_generate_missing_metadata(generation_requests))
        for generation_result in generation_results:
            existing_entry = dict(cache_entries.get(generation_result.event_id, {}))
            updated_entry: AgentCacheEntry = {
                **existing_entry,
                "event_id": generation_result.event_id,
                "conversation_id": generation_result.conversation_id,
                "user_prompt": generation_result.user_prompt,
                "summary_prompt_hash": generation_result.summary_prompt_hash,
                "work": generation_result.work_label,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            if generation_result.title is not None:
                title_cache_key = generation_result.title_source_event_id or generation_result.event_id
                title_entry = dict(cache_entries.get(title_cache_key, {}))
                title_entry["event_id"] = title_cache_key
                title_entry["conversation_id"] = generation_result.conversation_id
                title_entry["title"] = generation_result.title
                title_entry["title_prompt_hash"] = generation_result.title_prompt_hash or ""
                title_entry["updated_at"] = datetime.utcnow().isoformat() + "Z"
                cache_entries[title_cache_key] = title_entry
            if generation_result.title_prompt_hash is not None:
                updated_entry["title_prompt_hash"] = generation_result.title_prompt_hash
            cache_entries[generation_result.event_id] = updated_entry

    for conversation in conversations:
        first_cache_entry = cache_entries.get(conversation.title_source_event_id, {})
        conversation_title = first_cache_entry.get("title")
        title_value = conversation_title if isinstance(conversation_title, str) and conversation_title else ""
        for prompt_event in conversation.prompts:
            cache_entry = cache_entries.get(prompt_event.event_id, {})
            cached_user_prompt = cache_entry.get("user_prompt")
            if not isinstance(cached_user_prompt, str):
                continue
            work_label = cache_entry.get("work")
            csv_rows.append(
                AgentMessageCsvRow(
                    event_id=prompt_event.event_id,
                    work=work_label if isinstance(work_label, str) and work_label else _workspace_to_work_label(prompt_event.resolved_workspace or conversation.resolved_workspace),
                    user_prompt=cached_user_prompt,
                    title=title_value,
                    started_at=prompt_event.timestamp,
                )
            )

    return csv_rows


async def _generate_missing_metadata(
    requests: list[GeminiGenerationRequest],
) -> list[GeminiGenerationResult]:
    """使用 Gemini CLI 并发生成标题与总结。"""
    semaphore = asyncio.Semaphore(MAX_GEMINI_CONCURRENCY)
    tasks = [
        asyncio.create_task(_generate_single_metadata(request, semaphore))
        for request in requests
    ]
    return await asyncio.gather(*tasks)


async def _generate_single_metadata(
    request: GeminiGenerationRequest,
    semaphore: asyncio.Semaphore,
) -> GeminiGenerationResult:
    """单条消息生成 summary，首消息可额外生成 title。"""
    async with semaphore:
        return await asyncio.to_thread(_run_single_gemini_request, request)


def _run_single_gemini_request(request: GeminiGenerationRequest) -> GeminiGenerationResult:
    """在线程中执行一次阻塞 Gemini CLI 调用，避免 Windows asyncio 子进程问题。"""
    prompt_text = _build_gemini_prompt(request)
    gemini_command = _resolve_gemini_command()
    process = subprocess.run(
        [
            *gemini_command,
            "-m",
            GEMINI_MODEL,
            "--output-format",
            "json",
            "-p",
            prompt_text,
        ],
        env=_build_gemini_subprocess_env(),
        capture_output=True,
        text=False,
        check=False,
    )
    stdout_text = process.stdout.decode("utf-8", errors="replace").strip()
    stderr_text = process.stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise ValueError(
            "Gemini CLI 调用失败，请先配置 `GEMINI_API_KEY` 或完成 Gemini 登录。"
            f" stderr={stderr_text or 'empty'}"
        )

    title_value, user_prompt_value = _parse_gemini_json(stdout_text, expect_title=request.needs_title)
    return GeminiGenerationResult(
        event_id=request.prompt_event.event_id,
        conversation_id=request.conversation.conversation_id,
        user_prompt=user_prompt_value,
        summary_prompt_hash=request.summary_prompt_hash,
        title=title_value,
        title_prompt_hash=request.title_prompt_hash,
        title_source_event_id=request.conversation.title_source_event_id if request.needs_title else None,
        work_label=request.work_label,
    )


def _build_gemini_prompt(request: GeminiGenerationRequest) -> str:
    """给单条消息构造纯净的 Gemini 系统提示词。"""
    prompt_event = request.prompt_event
    prompt_lines = [
        "你是一个整理用户输入意图的程序。",
        "请只输出一个合法 JSON 对象，不要输出代码块，不要输出解释，不要输出 JSON 以外的任何文本。",
    ]
    if request.needs_title:
        prompt_lines.extend(
            [
                "请根据用户的输入，总结最多100字以内的用户输入目的总结。",
                "请额外总结一个20字内的title。",
                'JSON 格式必须是：{"title":"...","user_prompt":"..."}',
            ]
        )
    else:
        prompt_lines.extend(
            [
                "请根据用户的输入，总结最多100字以内的用户输入目的总结。",
                'JSON 格式必须是：{"user_prompt":"..."}',
            ]
        )
    return "\n".join(prompt_lines) + "\n\n" + prompt_event.cleaned_body


def _parse_gemini_json(stdout_text: str, expect_title: bool) -> tuple[str | None, str]:
    """按 Gemini CLI headless JSON 输出格式解析模型结果。"""
    cli_payload = json.loads(stdout_text)
    if not isinstance(cli_payload, dict):
        raise ValueError("Gemini CLI 顶层输出不是 JSON 对象。")

    error_payload = cli_payload.get("error")
    if isinstance(error_payload, dict):
        error_message = error_payload.get("message")
        raise ValueError(f"Gemini CLI 返回错误: {error_message if isinstance(error_message, str) else error_payload}")

    response_value = cli_payload.get("response")
    if not isinstance(response_value, str):
        raise ValueError("Gemini CLI 顶层 JSON 缺少 `response` 字段。")

    parsed_payload = _parse_model_response_json(response_value)

    raw_user_prompt = parsed_payload.get("user_prompt")
    if not isinstance(raw_user_prompt, str):
        raise ValueError("Gemini CLI 返回缺少 `user_prompt`。")

    user_prompt_value = _trim_text(_clean_prompt_body(raw_user_prompt), max_length=100) or "未生成总结"
    if not expect_title:
        return None, user_prompt_value

    raw_title = parsed_payload.get("title")
    if not isinstance(raw_title, str):
        raise ValueError("Gemini CLI 返回缺少 `title`。")
    title_value = _trim_text(_clean_prompt_body(raw_title), max_length=20) or "未命名对话"
    return title_value, user_prompt_value


def _parse_model_response_json(response_text: str) -> dict[str, object]:
    """从 CLI 的 response 文本中提取模型输出的 JSON 对象。"""
    stripped_text = response_text.strip()
    json_candidate = stripped_text
    fenced_parts = stripped_text.split("```")
    if len(fenced_parts) >= 3:
        json_candidate = fenced_parts[1]
        if json_candidate.lower().startswith("json"):
            json_candidate = json_candidate[4:].strip()

    json_match = JSON_BLOCK_PATTERN.search(json_candidate)
    if json_match is not None:
        json_candidate = json_match.group(0)

    parsed_payload = json.loads(json_candidate)
    if not isinstance(parsed_payload, dict):
        raise ValueError("模型 response 不是 JSON 对象。")
    return parsed_payload


def _load_agent_cache() -> AgentCachePayload:
    """读取本地缓存；不存在时返回空结构。"""
    if not AGENT_CACHE_PATH.exists():
        return {"version": AGENT_CACHE_SCHEMA_VERSION, "entries": {}}

    try:
        payload = json.loads(AGENT_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": AGENT_CACHE_SCHEMA_VERSION, "entries": {}}

    if not isinstance(payload, dict):
        return {"version": AGENT_CACHE_SCHEMA_VERSION, "entries": {}}
    if payload.get("version") != AGENT_CACHE_SCHEMA_VERSION:
        return {"version": AGENT_CACHE_SCHEMA_VERSION, "entries": {}}

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, dict):
        raw_entries = {}

    entries: dict[str, AgentCacheEntry] = {}
    for key, value in raw_entries.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        entries[key] = value

    return {"version": AGENT_CACHE_SCHEMA_VERSION, "entries": entries}


def _write_agent_cache(cache_payload: AgentCachePayload) -> None:
    """把缓存稳定写回本地。"""
    AGENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_CACHE_PATH.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _clean_prompt_body(raw_text: str) -> str:
    """移除明显的乱码占位符，并压缩空白。"""
    normalized_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines: list[str] = []
    for line in normalized_text.split("\n"):
        line_without_noise = INVALID_GLYPH_PATTERN.sub("", line).strip()
        line_without_noise = WHITESPACE_PATTERN.sub(" ", line_without_noise).strip()
        if not line_without_noise:
            continue
        if not SEMANTIC_TEXT_PATTERN.search(line_without_noise):
            continue
        cleaned_lines.append(line_without_noise)
    return "\n".join(cleaned_lines).strip()


def _should_keep_prompt_event(raw_text: str, cleaned_text: str) -> bool:
    """在调用 Gemini 前过滤掉明显乱码或无意义的 agent 消息。"""
    if not cleaned_text:
        return False

    semantic_char_count = len(SEMANTIC_TEXT_PATTERN.findall(cleaned_text))
    if semantic_char_count < MIN_MEANINGFUL_PROMPT_CHARS:
        return False

    visible_raw_chars = [char for char in raw_text if not char.isspace()]
    suspicious_char_count = len(SUSPICIOUS_GLYPH_PATTERN.findall(raw_text))
    if visible_raw_chars and suspicious_char_count / len(visible_raw_chars) >= 0.25:
        return False

    if INVALID_GLYPH_PATTERN.search(raw_text) and semantic_char_count < MIN_MEANINGFUL_PROMPT_CHARS * 2:
        return False

    return True


def _extract_workspace_roots(value: object) -> tuple[str, ...]:
    """读取 workspaceRoots，并统一成可比较的路径文本。"""
    if not isinstance(value, list):
        return tuple()

    normalized_roots: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized_path = _normalize_path_text(item)
        if normalized_path:
            normalized_roots.append(normalized_path)
    return tuple(dict.fromkeys(normalized_roots))


def _resolve_workspace_for_agent_event(
    agent_event: EventInterval,
    workspace_roots: tuple[str, ...],
    context_events: list[EventInterval],
) -> str:
    """优先用附近的 vscode 文件活动，把多根工作区收敛到真实根目录。"""
    if not workspace_roots:
        return ""
    if len(workspace_roots) == 1:
        return workspace_roots[0]

    best_match: tuple[float, str] | None = None
    for context_event in context_events:
        if context_event.source_device != agent_event.source_device:
            continue
        gap_seconds = abs((context_event.start - agent_event.start).total_seconds())
        if gap_seconds > MAX_WORKSPACE_MATCH_GAP_SECONDS:
            continue

        file_value = _pick_event_string(context_event.data, ("file",))
        project_value = _pick_event_string(context_event.data, ("project",))
        for candidate_path in (file_value, project_value):
            matched_root = _match_workspace_root(candidate_path, workspace_roots)
            if matched_root is None:
                continue
            candidate_score = (gap_seconds, matched_root)
            if best_match is None or candidate_score < best_match:
                best_match = candidate_score

    if best_match is not None:
        return best_match[1]
    return workspace_roots[0]


def _resolve_conversation_workspace(prompt_events: list[AgentPromptEvent]) -> str:
    """用会话内出现次数最多的真实根目录作为该对话工作区。"""
    workspace_counter: dict[str, int] = {}
    for prompt_event in prompt_events:
        if not prompt_event.resolved_workspace:
            continue
        workspace_counter[prompt_event.resolved_workspace] = workspace_counter.get(prompt_event.resolved_workspace, 0) + 1

    if workspace_counter:
        return max(
            workspace_counter.items(),
            key=lambda item: (item[1], item[0]),
        )[0]
    return prompt_events[0].resolved_workspace


def _match_workspace_root(candidate_path: str | None, workspace_roots: tuple[str, ...]) -> str | None:
    """判断文件或项目路径属于哪个 workspace root。"""
    if not isinstance(candidate_path, str) or not candidate_path:
        return None

    normalized_candidate = _normalize_path_text(candidate_path)
    if not normalized_candidate:
        return None

    for workspace_root in workspace_roots:
        if normalized_candidate == workspace_root or normalized_candidate.startswith(f"{workspace_root}/"):
            return workspace_root
    return None


def _normalize_path_text(value: str) -> str:
    """把 Windows/Posix 风格路径统一成便于比较的文本。"""
    normalized_value = value.strip().replace("\\", "/")
    if not normalized_value:
        return ""
    drive_match = WINDOWS_DRIVE_PATTERN.match(normalized_value)
    if drive_match is not None:
        normalized_value = drive_match.group(1) + normalized_value[len(drive_match.group(0)) :]
    if len(normalized_value) >= 2 and normalized_value[1] == ":":
        normalized_value = normalized_value[0].upper() + normalized_value[1:]
    while "//" in normalized_value:
        normalized_value = normalized_value.replace("//", "/")
    return normalized_value.rstrip("/")


def _workspace_to_work_label(workspace_path: str) -> str:
    """CSV 的 work 列只保留工作区名。"""
    if not workspace_path:
        return "unknown"
    workspace_name = Path(workspace_path).name.strip()
    return workspace_name or workspace_path


def _pick_event_string(data: dict[str, object], keys: tuple[str, ...]) -> str | None:
    """从事件 data 中读取第一个非空字符串字段。"""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _stable_event_id(event: EventInterval) -> str:
    """优先使用原始 event_id，缺失时回退到时间和会话组成的稳定键。"""
    if isinstance(event.event_id, int):
        return str(event.event_id)
    if isinstance(event.event_id, str) and event.event_id:
        return event.event_id

    conversation_id = event.data.get("conversationId")
    conversation_part = conversation_id if isinstance(conversation_id, str) and conversation_id else "unknown"
    timestamp_text = event.start.isoformat()
    return f"{conversation_part}:{timestamp_text}"


def _hash_text(value: str) -> str:
    """为 prompt 内容生成稳定摘要键。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trim_text(value: str, max_length: int) -> str:
    """简单按字符数裁剪模型输出，避免超出列宽预期。"""
    normalized_value = value.strip()
    if len(normalized_value) <= max_length:
        return normalized_value
    return normalized_value[:max_length].rstrip()


def _serialize_prompt_timestamp(value: datetime) -> str:
    """稳定输出 agent raw CSV 的时间列。"""
    return value.isoformat().replace("+00:00", "Z")


def _build_gemini_subprocess_env() -> dict[str, str]:
    """为 Gemini 子进程准备环境变量，兼容旧 shell 未刷新的情况。"""
    process_env = dict(os.environ)
    if process_env.get("GEMINI_API_KEY"):
        return process_env

    persisted_key = _load_windows_user_env("GEMINI_API_KEY")
    if persisted_key:
        process_env["GEMINI_API_KEY"] = persisted_key
    return process_env


def _resolve_gemini_command() -> tuple[str, ...]:
    """解析可直接给 subprocess 使用的 Gemini 可执行命令。"""
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        candidate_paths: list[str] = []
        if appdata:
            candidate_paths.extend(
                [
                    str(Path(appdata) / "npm" / "gemini.cmd"),
                    str(Path(appdata) / "npm" / "gemini.ps1"),
                ]
            )
        resolved_from_path = shutil.which("gemini.cmd") or shutil.which("gemini")
        if resolved_from_path:
            candidate_paths.append(resolved_from_path)
        for candidate_path in candidate_paths:
            if candidate_path and Path(candidate_path).exists():
                return (candidate_path,)
        raise ValueError("未找到 Gemini CLI 可执行文件，请确认 `@google/gemini-cli` 已正确安装。")

    resolved_command = shutil.which("gemini")
    if resolved_command:
        return (resolved_command,)
    return ("gemini",)


def _load_windows_user_env(name: str) -> str | None:
    """在 Windows 下从用户环境变量读取 Gemini key。"""
    if not sys.platform.startswith("win"):
        return None

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env_key:
            value, _ = winreg.QueryValueEx(env_key, name)
    except (FileNotFoundError, OSError):
        return None

    return value if isinstance(value, str) and value else None
