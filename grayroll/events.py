from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import GrayrollError
from .util import (
    require_dict,
    require_int,
    require_keys,
    require_number,
    require_str,
)


def normalize_event(value: Any, line_number: int | None = None) -> dict[str, Any]:
    code = "E_BAD_EVENT"
    field = f"event line {line_number}" if line_number else "event"
    event = require_dict(value, field, code)
    require_keys(
        event,
        ("batch_id", "event_id", "logical_ms", "service", "type"),
        field,
        code,
    )
    event_id = require_str(event["event_id"], f"{field}.event_id", code)
    kind = require_str(event["type"], f"{field}.type", code)
    service = require_str(event["service"], f"{field}.service", code)
    batch_id = require_str(event["batch_id"], f"{field}.batch_id", code)
    logical_ms = require_int(event["logical_ms"], f"{field}.logical_ms", code)
    if kind == "snapshot":
        require_keys(
            event,
            ("metrics", "sample_count", "tier_ratio"),
            field,
            code,
        )
        sample_count = require_int(event["sample_count"], f"{field}.sample_count", code)
        tier_ratio = require_number(event["tier_ratio"], f"{field}.tier_ratio", code)
        if tier_ratio < 0.0 or tier_ratio > 1.0:
            raise GrayrollError(code, f"{field}.tier_ratio must be within [0, 1]")
        metrics_raw = require_dict(event["metrics"], f"{field}.metrics", code)
        metrics: dict[str, float] = {}
        for metric, raw_value in metrics_raw.items():
            metrics[metric] = require_number(
                raw_value, f"{field}.metrics.{metric}", code
            )
        return {
            "batch_id": batch_id,
            "event_id": event_id,
            "logical_ms": logical_ms,
            "metrics": metrics,
            "sample_count": sample_count,
            "service": service,
            "tier_ratio": tier_ratio,
            "type": "snapshot",
        }
    if kind == "manual_abort":
        reason = require_str(
            event.get("reason", "manual_abort"), f"{field}.reason", code
        )
        return {
            "batch_id": batch_id,
            "event_id": event_id,
            "logical_ms": logical_ms,
            "reason": reason,
            "service": service,
            "type": "manual_abort",
        }
    raise GrayrollError(code, f"{field}.type must be snapshot or manual_abort")


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_clock: int | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise GrayrollError(
                        "E_BAD_EVENT", f"cannot parse event line {line_number}: {exc}"
                    ) from exc
                event = normalize_event(raw, line_number)
                if event["event_id"] in seen:
                    raise GrayrollError(
                        "E_BAD_EVENT", f"duplicate event_id: {event['event_id']}"
                    )
                seen.add(event["event_id"])
                if previous_clock is not None and event["logical_ms"] < previous_clock:
                    raise GrayrollError(
                        "E_BAD_EVENT",
                        "observation event stream is not ordered by logical_ms",
                    )
                previous_clock = event["logical_ms"]
                events.append(event)
    except FileNotFoundError as exc:
        raise GrayrollError(
            "E_BAD_EVENT", f"event file not found: {path.name}"
        ) from exc
    return events
