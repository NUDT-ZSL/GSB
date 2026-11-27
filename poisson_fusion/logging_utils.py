"""Persistent JSON-lines run history."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io_utils import append_jsonl


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_record(log_path: str | Path | None, record: dict[str, Any]) -> None:
    if log_path is None:
        return
    enriched = {"timestamp_utc": now_utc(), **record}
    append_jsonl(log_path, enriched)
