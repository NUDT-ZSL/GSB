"""Session-event JSONL validation and failure collection."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .canonical import quantize

REQUIRED_EVENT_FIELDS = ("session_id", "turn_no", "speaker", "text", "timestamp")


def failure(
    *,
    code: str,
    stage: str,
    message: str,
    session_id: Any = None,
    turn_no: Any = None,
    line_no: int | None = None,
    window_start: int | None = None,
    batch_id: str | None = None,
    injection_id: str | None = None,
    exception_type: str | None = None,
) -> dict[str, Any]:
    return {
        "business_code": code,
        "stage": stage,
        "message": message,
        "session_id": session_id,
        "turn_no": turn_no,
        "line_no": line_no,
        "window_start": window_start,
        "batch_id": batch_id,
        "injection_id": injection_id,
        "exception_type": exception_type,
    }


def failure_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(item.get("session_id") or ""),
        int(item.get("turn_no") or 0),
        int(item.get("window_start") or 0),
        int(item.get("line_no") or 0),
        str(item.get("batch_id") or ""),
        str(item.get("business_code") or ""),
        str(item.get("stage") or ""),
    )


def sort_failures(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=failure_sort_key)


def parse_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"timestamp is not parseable: {value!r}") from exc
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string or Unix timestamp")
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"timestamp is not parseable: {value!r}") from exc
    return parsed.isoformat()


def validate_event(
    raw: Any,
    *,
    line_no: int | None,
    expected_turn: int | None = None,
    stage: str = "ingest",
    batch_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    injection_id = raw.get("injection_id") if isinstance(raw, dict) else None
    session_id = raw.get("session_id") if isinstance(raw, dict) else None
    turn_no = raw.get("turn_no") if isinstance(raw, dict) else None
    if not isinstance(raw, dict):
        return None, failure(
            code="E_TURN_INVALID",
            stage=stage,
            message="JSON line must be an object",
            line_no=line_no,
            batch_id=batch_id,
            injection_id=injection_id,
        )
    missing = [field for field in REQUIRED_EVENT_FIELDS if field not in raw]
    if missing:
        return None, failure(
            code="E_TURN_INVALID",
            stage=stage,
            message=f"missing required fields: {','.join(missing)}",
            session_id=session_id,
            turn_no=turn_no,
            line_no=line_no,
            batch_id=batch_id,
            injection_id=injection_id,
        )
    if not isinstance(session_id, str) or not session_id.strip():
        return None, failure(
            code="E_TURN_INVALID",
            stage=stage,
            message="session_id must be a non-empty string",
            session_id=session_id,
            turn_no=turn_no,
            line_no=line_no,
            batch_id=batch_id,
            injection_id=injection_id,
        )
    if not isinstance(turn_no, int) or isinstance(turn_no, bool) or turn_no < 1:
        return None, failure(
            code="E_TURN_INVALID",
            stage=stage,
            message="turn_no must be an integer >= 1",
            session_id=session_id,
            turn_no=turn_no,
            line_no=line_no,
            batch_id=batch_id,
            injection_id=injection_id,
        )
    if expected_turn is not None and turn_no != expected_turn:
        return None, failure(
            code="E_TURN_INVALID",
            stage=stage,
            message=f"turn_no must be {expected_turn}; got {turn_no}",
            session_id=session_id,
            turn_no=turn_no,
            line_no=line_no,
            batch_id=batch_id,
            injection_id=injection_id,
        )
    speaker = raw.get("speaker")
    if not isinstance(speaker, str) or not speaker.strip():
        return None, failure(
            code="E_TURN_INVALID",
            stage=stage,
            message="speaker must be a non-empty string",
            session_id=session_id,
            turn_no=turn_no,
            line_no=line_no,
            batch_id=batch_id,
            injection_id=injection_id,
        )
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, failure(
            code="E_TURN_INVALID",
            stage=stage,
            message="text must be a non-empty string",
            session_id=session_id,
            turn_no=turn_no,
            line_no=line_no,
            batch_id=batch_id,
            injection_id=injection_id,
        )
    try:
        timestamp_iso = parse_timestamp(raw.get("timestamp"))
    except ValueError as exc:
        return None, failure(
            code="E_TURN_INVALID",
            stage=stage,
            message=str(exc),
            session_id=session_id,
            turn_no=turn_no,
            line_no=line_no,
            batch_id=batch_id,
            injection_id=injection_id,
        )
    event = dict(raw)
    event["session_id"] = session_id
    event["turn_no"] = turn_no
    event["speaker"] = speaker
    event["text"] = text
    event["timestamp_iso"] = timestamp_iso
    return event, None


def load_events(path: Path) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    events: dict[tuple[str, int], dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    sequence_cursor: dict[str, int] = {}
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read event stream {path}: {exc}") from exc
    with handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                failures.append(failure(
                    code="E_TURN_INVALID",
                    stage="ingest",
                    message="blank JSONL line",
                    line_no=line_no,
                ))
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                failures.append(failure(
                    code="E_TURN_INVALID",
                    stage="ingest",
                    message=f"invalid JSON: {exc.msg}",
                    line_no=line_no,
                ))
                continue
            raw_session = raw.get("session_id") if isinstance(raw, dict) else None
            raw_turn = raw.get("turn_no") if isinstance(raw, dict) else None
            expected = None
            structurally_indexable = (
                isinstance(raw_session, str)
                and bool(raw_session.strip())
                and isinstance(raw_turn, int)
                and not isinstance(raw_turn, bool)
                and raw_turn >= 1
            )
            if structurally_indexable:
                expected = sequence_cursor.get(raw_session, 0) + 1
                sequence_cursor[raw_session] = raw_turn
            event, error = validate_event(
                raw,
                line_no=line_no,
                expected_turn=expected,
                stage="ingest",
            )
            if error is not None:
                failures.append(error)
                continue
            assert event is not None
            key = (event["session_id"], event["turn_no"])
            if key in events:
                failures.append(failure(
                    code="E_TURN_INVALID",
                    stage="ingest",
                    message="duplicate session_id/turn_no",
                    session_id=key[0],
                    turn_no=key[1],
                    line_no=line_no,
                    injection_id=event.get("injection_id"),
                ))
                continue
            events[key] = event
    return events, sort_failures(failures)


def normalize_threshold(value: float, precision: int) -> float:
    return quantize(value, precision)
