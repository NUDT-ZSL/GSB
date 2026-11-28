"""多源事件读取、校验与归一化。"""
from __future__ import annotations

import json

from .models import Event, SOURCES
from .registry import DependencyRegistry


class EventValidationError(ValueError):
    """带文件位置信息的事件校验错误，避免静默丢弃。"""

    def __init__(self, path: str, line_no: int, reason: str, event_id: str | None = None):
        location = f"{path}:{line_no}"
        if event_id:
            location += f"(event_id={event_id})"
        super().__init__(f"事件校验失败 @ {location}: {reason}")
        self.location = location
        self.reason = reason


def load_events(path: str, registry: DependencyRegistry, default_batch: str) -> list[Event]:
    events: list[Event] = []
    seen_ids: set[str] = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EventValidationError(path, line_no, f"不是合法 JSON: {exc.msg}") from exc
            event = _build_event(path, line_no, payload, registry, default_batch, seen_ids)
            seen_ids.add(event.event_id)
            events.append(event)
    if not events:
        raise EventValidationError(path, 0, "事件文件为空，无法汇总")
    return events


def _build_event(path, line_no, payload, registry: DependencyRegistry, default_batch, seen_ids) -> Event:
    if not isinstance(payload, dict):
        raise EventValidationError(path, line_no, "每行必须是一个 JSON 对象")
    event_id = payload.get("event_id")
    if not event_id or not isinstance(event_id, str):
        raise EventValidationError(path, line_no, "缺少字符串类型的 event_id")
    if event_id in seen_ids:
        raise EventValidationError(path, line_no, "event_id 重复", event_id)

    source = payload.get("source")
    if source not in SOURCES:
        raise EventValidationError(
            path, line_no, f"source={source!r} 不在允许集合 {SOURCES}", event_id
        )

    service = payload.get("service")
    if not service or not isinstance(service, str):
        raise EventValidationError(path, line_no, "缺少字符串类型的 service", event_id)
    if service not in registry.services:
        raise EventValidationError(
            path,
            line_no,
            f"服务 {service!r} 未出现在依赖登记中，已登记服务: {sorted(registry.services)}",
            event_id,
        )

    code = payload.get("code")
    if not code or not isinstance(code, str):
        raise EventValidationError(path, line_no, "缺少字符串类型的 code", event_id)

    ts_ms = _normalize_ts(path, line_no, payload, event_id)
    status = payload.get("status", "trigger")
    if not isinstance(status, str):
        raise EventValidationError(path, line_no, "status 必须是字符串", event_id)
    severity = payload.get("severity", "info")
    batch_id = payload.get("batch") or default_batch
    if not isinstance(batch_id, str) or not batch_id:
        raise EventValidationError(path, line_no, "batch 必须是非空字符串", event_id)
    return Event(
        event_id=event_id,
        source=source,
        service=service,
        code=code,
        ts_ms=ts_ms,
        batch_id=batch_id,
        status=status,
        severity=severity,
        raw=payload,
    )


def _normalize_ts(path, line_no, payload, event_id) -> int:
    if "ts_ms" not in payload:
        raise EventValidationError(path, line_no, "缺少毫秒级逻辑时刻字段 ts_ms", event_id)
    value = payload["ts_ms"]
    if isinstance(value, bool):
        raise EventValidationError(path, line_no, "ts_ms 不能是布尔值", event_id)
    if isinstance(value, int):
        ts_ms = value
    elif isinstance(value, float) and value.is_integer():
        ts_ms = int(value)
    else:
        raise EventValidationError(path, line_no, f"ts_ms={value!r} 必须是整数毫秒", event_id)
    if ts_ms < 0:
        raise EventValidationError(path, line_no, f"ts_ms={ts_ms} 不能为负数", event_id)
    return ts_ms
