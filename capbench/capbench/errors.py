"""Business error codes and structured exception types.

All user-facing, recoverable failures carry a stable business code:
  E_SCENE  - scene file malformed or fails strong validation
  E_PORT   - loopback target port unavailable / occupied
  E_CLOCK  - monotonic sampling clock moved backwards
  E_REPLAY - snapshot replay consistency check failed
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"

    def to_log_record(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class SceneError(BenchError):
    def __init__(self, message: str, field_paths: list[str] | None = None,
                 details: dict[str, Any] | None = None) -> None:
        merged = dict(details or {})
        if field_paths:
            merged["field_paths"] = list(field_paths)
        super().__init__("E_SCENE", message, merged)
        self.field_paths: list[str] = list(field_paths or [])


class PortError(BenchError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_PORT", message, details or {})


class ClockError(BenchError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_CLOCK", message, details or {})


class ReplayError(BenchError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_REPLAY", message, details or {})


class StructuredLogger:
    """Append-only JSON-lines structured logger (one record per line)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def log(self, level: str, event: str, **fields: Any) -> None:
        import time
        record = {
            "ts_wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": level,
            "event": event,
            **fields,
        }
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def error(self, err: BenchError, event: str = "business_error") -> None:
        self.log("ERROR", event, **err.to_log_record())
