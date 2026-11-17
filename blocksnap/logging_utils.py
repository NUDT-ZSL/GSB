"""Structured JSONL business logging.

All business-code events are appended as one JSON object per line to
``<store>/logs/events.log.jsonl``. Logging never raises into the caller: a
failed log write is reported on stderr but does not mask the original error.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()


def _log_path(store_dir: str) -> str:
    return os.path.join(store_dir, "logs", "events.log.jsonl")


def log_event(store_dir: str, event: str, code: str | None = None, **fields) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event": event,
        "code": code,
    }
    record.update(fields)
    path = _log_path(store_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LOCK, open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - defensive
        import sys

        print(f"blocksnap: failed to write log {path}: {exc}", file=sys.stderr)
    return record


def log_error(store_dir: str, event: str, err: "BaseException", **fields) -> dict:
    if hasattr(err, "to_record"):
        rec = err.to_record()
        fields.update(rec.get("context", {}))
        code = rec["code"]
        message = rec["message"]
    else:
        code, message = "E_UNKNOWN", str(err)
    return log_event(store_dir, event, code=code, message=message, **fields)


def query_logs(
    store_dir: str,
    code: str | None = None,
    event: str | None = None,
) -> list[dict]:
    """Return structured log records, optionally filtered by code or event."""
    path = _log_path(store_dir)
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if code is not None and record.get("code") != code:
                continue
            if event is not None and record.get("event") != event:
                continue
            records.append(record)
    return records
