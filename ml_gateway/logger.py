from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .storage import SessionStore


class StructuredLogger:
    """Append-only JSONL logger mirrored into SQLite with logical ordering."""

    def __init__(self, store: SessionStore, path: str | Path, fresh: bool = False) -> None:
        self.store = store
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if fresh:
            self.path.write_text("", encoding="utf-8")
        else:
            self.path.touch(exist_ok=True)

    def log(
        self,
        level: str,
        code: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            event_seq = self.store.append_log_event_auto(
                level=level,
                code=code,
                message=message,
                payload=payload,
            )
            record = {
                "code": code,
                "event_seq": event_seq,
                "level": level,
                "message": message,
                "payload": payload,
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical_json(record) + "\n")
            return record

    def info(self, code: str, message: str, **payload: Any) -> dict[str, Any]:
        return self.log("INFO", code, message, payload)

    def warning(self, code: str, message: str, **payload: Any) -> dict[str, Any]:
        return self.log("WARN", code, message, payload)

    def error(self, code: str, message: str, **payload: Any) -> dict[str, Any]:
        return self.log("ERROR", code, message, payload)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records
