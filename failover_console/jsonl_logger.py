"""Small dependency-free structured JSON-lines logger."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import BusinessError


class JsonlLogger:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        echo: bool = True,
    ) -> None:
        self.path = Path(path) if path else None
        self.echo = echo
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, level: str, code: str, message: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "epoch_ms": int(time.time() * 1000),
            "level": level,
            "code": code,
            "message": message,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            if self.path:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            if self.echo:
                print(line, flush=True)
        return record

    def info(self, code: str, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("INFO", code, message, **fields)

    def error(self, code: str, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("ERROR", code, message, **fields)

    def business_error(self, error: BusinessError, **fields: Any) -> dict[str, Any]:
        payload = error.as_dict()
        return self.error(
            payload["code"],
            payload["message"],
            details=payload.get("details"),
            issues=payload.get("issues"),
            **fields,
        )
