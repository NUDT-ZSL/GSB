"""NDJSON 事件流加载与强校验。"""
from __future__ import annotations

import json
from typing import Any

from .errors import E_EVENT, GovernanceError, Issue
from .models import RESULT_ALIASES, Event
from .validation import is_strict_int


def load_events(path: str) -> list[Event]:
    """逐行读取 NDJSON，任一非法以 E_EVENT 整体拒绝，并附行号与字段路径。"""
    issues: list[Issue] = []
    events: list[Event] = []
    last_ts: int | None = None
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append(Issue("$", f"NDJSON 语法错误: {exc.msg}", line=lineno))
                continue
            if not isinstance(payload, dict):
                issues.append(Issue("$", "事件必须为 JSON 对象", line=lineno))
                continue
            event, line_issues = _parse_event(payload, lineno)
            issues.extend(line_issues)
            if event is None:
                continue
            if last_ts is not None and event.timestamp_ms < last_ts:
                issues.append(Issue(
                    "timestamp_ms",
                    f"时间戳必须单调不减，当前 {event.timestamp_ms} < 上一事件 {last_ts}",
                    line=lineno,
                    value=event.timestamp_ms,
                ))
            else:
                last_ts = event.timestamp_ms
            events.append(event)
    if issues:
        raise GovernanceError(E_EVENT, "事件流校验失败，整体拒绝加载", issues)
    return events


def _parse_event(payload: dict[str, Any], lineno: int) -> tuple[Event | None, list[Issue]]:
    issues: list[Issue] = []
    ts = payload.get("timestamp_ms")
    service_id = payload.get("service_id")
    result = payload.get("result")
    latency = payload.get("latency_ms")

    if not is_strict_int(ts) or ts < 0:
        issues.append(Issue("timestamp_ms", "时间戳必须为非负毫秒整数", line=lineno, value=ts))
    if not isinstance(service_id, str) or not service_id:
        issues.append(Issue("service_id", "服务标识必须为非空字符串", line=lineno, value=service_id))
    canonical = RESULT_ALIASES.get(result) if isinstance(result, str) else None
    if canonical is None:
        issues.append(Issue("result",
                            "结果必须属于 success/failure/timeout（或 成功/失败/超时）",
                            line=lineno, value=result))
    if not is_strict_int(latency) or latency < 0:
        issues.append(Issue("latency_ms", "耗时必须为非负整数毫秒", line=lineno, value=latency))
    if issues:
        return None, issues
    return Event(timestamp_ms=ts, service_id=service_id,
                 result=canonical, latency_ms=latency), []
