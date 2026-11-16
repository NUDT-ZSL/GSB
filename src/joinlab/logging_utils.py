"""Structured JSONL business logging."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import JoinLabError


class BusinessLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def write(self, event: str, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def error(self, operation: str, error: JoinLabError, **context: Any) -> None:
        self.write(
            "business_error",
            {
                "operation": operation,
                "code": error.code,
                "message": error.message,
                "path": error.path,
                "details": error.details,
                **context,
            },
        )
