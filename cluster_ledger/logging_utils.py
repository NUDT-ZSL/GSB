"""Structured JSONL event logging."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .errors import BusinessError


class EventLogger:
    """Append-only structured event logger.

    Wall-clock timestamps are for operator correlation only. Ordering and all
    performance calculations use monotonic clocks.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, *, code: str | None = None, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event": event,
            "wall_time_ns": time.time_ns(),
            "pid": os.getpid(),
        }
        if code is not None:
            record["code"] = code
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if self.path:
            with self._lock:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        return record

    def error(self, error: BusinessError, event: str = "business_error", **fields: Any) -> dict[str, Any]:
        return self.emit(
            event,
            code=error.code,
            message=error.message,
            field_path=error.field_path,
            details=error.details,
            **fields,
        )

