from __future__ import annotations

import json
from datetime import datetime
from typing import cast
from urllib import parse, request

from aw_client.intervals import parse_aw_timestamp
from aw_client.models import BucketDescriptor, RawEvent


class ActivityWatchRestClient:
    """直接访问本地 ActivityWatch REST API 的轻量客户端。"""

    def __init__(self, base_url: str = "http://localhost:5600/api/0", timeout_seconds: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def list_buckets(self) -> dict[str, BucketDescriptor]:
        """拉取全部 buckets，并解析成结构化对象。"""
        response_payload = self._request_json("GET", "/buckets")
        if not isinstance(response_payload, dict):
            raise ValueError("ActivityWatch `/buckets` 返回结果不是对象。")

        buckets: dict[str, BucketDescriptor] = {}
        for bucket_id, bucket_payload in response_payload.items():
            if not isinstance(bucket_id, str) or not isinstance(bucket_payload, dict):
                continue
            buckets[bucket_id] = self._parse_bucket(bucket_id, bucket_payload)
        return buckets

    def get_events(
        self,
        bucket_id: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[RawEvent]:
        """按 bucket 和时间范围拉取原始事件。"""
        query_params = {
            "start": self._format_datetime(start),
            "end": self._format_datetime(end),
        }
        if limit is not None:
            query_params["limit"] = str(limit)

        encoded_bucket_id = parse.quote(bucket_id, safe="")
        endpoint = f"/buckets/{encoded_bucket_id}/events?{parse.urlencode(query_params)}"
        response_payload = self._request_json("GET", endpoint)
        if not isinstance(response_payload, list):
            raise ValueError("ActivityWatch `/events` 返回结果不是数组。")

        events: list[RawEvent] = []
        for event_payload in response_payload:
            if not isinstance(event_payload, dict):
                continue
            parsed_event = self._parse_event(bucket_id, event_payload)
            if parsed_event is not None:
                events.append(parsed_event)

        return events

    def get_settings(self, key: str | None = None) -> dict[str, object]:
        """读取全部设置或单个设置键。"""
        endpoint = "/settings" if key is None else f"/settings/{parse.quote(key, safe='')}"
        response_payload = self._request_json("GET", endpoint)
        if isinstance(response_payload, dict):
            return cast(dict[str, object], response_payload)
        return {"value": response_payload}

    def post_event(self, bucket_id: str, event: dict[str, object]) -> None:
        """向指定 bucket 发送单个事件。"""
        encoded_bucket_id = parse.quote(bucket_id, safe="")
        endpoint = f"/buckets/{encoded_bucket_id}/events"
        self._request_json("POST", endpoint, event)

    def post_events(self, bucket_id: str, events: list[dict[str, object]]) -> None:
        """向指定 bucket 批量发送事件。"""
        encoded_bucket_id = parse.quote(bucket_id, safe="")
        endpoint = f"/buckets/{encoded_bucket_id}/events"
        self._request_json("POST", endpoint, events)

    def _request_json(self, method: str, endpoint: str, payload: object | None = None) -> object:
        """统一封装 HTTP 请求与 JSON 解析。"""
        target_url = f"{self.base_url}{endpoint}"
        encoded_payload: bytes | None = None
        headers = {"Accept": "application/json"}

        if payload is not None:
            encoded_payload = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        http_request = request.Request(
            url=target_url,
            data=encoded_payload,
            method=method,
            headers=headers,
        )

        with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
            response_text = response.read().decode("utf-8")
            if not response_text.strip():
                return None
            return json.loads(response_text)

    def _parse_bucket(self, bucket_id: str, payload: dict[str, object]) -> BucketDescriptor:
        """把原始 bucket JSON 映射为强类型模型。"""
        data_payload = payload.get("data")
        metadata_payload = payload.get("metadata")

        metadata_start: datetime | None = None
        metadata_end: datetime | None = None
        if isinstance(metadata_payload, dict):
            metadata_start = parse_aw_timestamp(self._to_optional_str(metadata_payload.get("start")))
            metadata_end = parse_aw_timestamp(self._to_optional_str(metadata_payload.get("end")))

        data_value = dict(data_payload) if isinstance(data_payload, dict) else {}
        return BucketDescriptor(
            bucket_id=bucket_id,
            bucket_type=self._to_optional_str(payload.get("type")) or "unknown",
            client=self._to_optional_str(payload.get("client")) or "unknown",
            hostname=self._to_optional_str(payload.get("hostname")) or "unknown",
            created_at=parse_aw_timestamp(self._to_optional_str(payload.get("created"))),
            data=data_value,
            metadata_start=metadata_start,
            metadata_end=metadata_end,
        )

    def _parse_event(self, bucket_id: str, payload: dict[str, object]) -> RawEvent | None:
        """把原始 event JSON 映射为强类型模型。"""
        timestamp_value = parse_aw_timestamp(self._to_optional_str(payload.get("timestamp")))
        if timestamp_value is None:
            return None

        duration_value = payload.get("duration")
        duration_seconds = float(duration_value) if isinstance(duration_value, (int, float)) else 0.0
        data_value = payload.get("data")

        return RawEvent(
            bucket_id=bucket_id,
            event_id=payload.get("id") if isinstance(payload.get("id"), int | str) else None,
            timestamp=timestamp_value,
            duration_seconds=duration_seconds,
            data=dict(data_value) if isinstance(data_value, dict) else {},
        )

    def _format_datetime(self, value: datetime) -> str:
        """把 datetime 格式化成 ActivityWatch 可接受的 ISO 字符串。"""
        return value.astimezone().isoformat()

    def _to_optional_str(self, value: object) -> str | None:
        """安全地把 JSON 字段转成可选字符串。"""
        return value if isinstance(value, str) else None
