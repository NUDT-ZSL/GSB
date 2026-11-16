"""Structured JSON-lines logging.

Every log entry is a single JSON object on its own line with a stable, sorted
key order.  Timestamps are UTC ISO-8601; they are deliberately the only
time-dependent field.  The *audit conclusions* themselves never depend on the
logger, so bit-identical reports are unaffected by logging.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


class StructuredLogger:
    def __init__(self, path: str | Path | None = None,
                 stream: TextIO | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._stream = stream
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> dict:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "event": event,
        }
        entry.update(fields)
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self._stream is not None:
            self._stream.write(line + "\n")
            self._stream.flush()
        return entry

    def log_error(self, event: str, code: str, message: str,
                  key: str | None = None, grid: int | None = None,
                  locations: list[str] | None = None) -> dict:
        fields: dict[str, Any] = {"code": code, "message": message}
        if key is not None:
            fields["key"] = key
        if grid is not None:
            fields["grid"] = grid
        if locations is not None:
            fields["locations"] = list(locations)
        return self.log(event, **fields)
