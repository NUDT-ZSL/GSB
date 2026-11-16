"""Deterministic structured JSONL logging for local experiment sessions."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


class StructuredLogger:
    """Append-only JSONL logger.

    Every event is serialized with ``sort_keys=True`` so the output ordering is
    stable across runs; only the monotonic wall timestamp varies.
    """

    def __init__(self, path: Optional[str]):
        self.path = path
        self._lock = threading.Lock()
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)

    def event(self, name: str, payload: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": name,
        }
        if payload:
            record.update(dict(payload))
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if self.path:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        return record
