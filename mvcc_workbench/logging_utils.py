"""Append-only structured JSONL event log."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class StructuredLogger:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def event(self, event: str, level: str = "info", **fields: Any) -> dict[str, Any]:
        record = {
            "ts_ns": time.time_ns(),
            "monotonic_ns": time.perf_counter_ns(),
            "level": level,
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
        return record

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-max(1, limit) :]
        result: list[dict[str, Any]] = []
        for line in lines:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
