from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ErrorCode:
    LATENCY_BUDGET = "E_LATENCY_BUDGET"
    BLOCK_MISMATCH = "E_BLOCK_MISMATCH"
    ANCHOR_STRENGTH = "E_ANCHOR_STRENGTH"
    REFERENCE_SHAPE = "E_REFERENCE_SHAPE"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    STAGE_BUSY = "E_STAGE_BUSY"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    INVALID_REQUEST = "E_INVALID_REQUEST"
    SNAPSHOT_CHECKSUM = "E_SNAPSHOT_CHECKSUM"
    TASK_NOT_FOUND = "E_TASK_NOT_FOUND"
    PLAN_NOT_READY = "E_PLAN_NOT_READY"


@dataclass
class AppError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_record(self) -> dict[str, Any]:
        return {"error_code": self.code, "message": self.message, "details": self.details}


class ErrorLedger:
    """Thread-safe business error ledger.

    The ledger is intentionally append-only.  It may be mirrored to JSONL so a
    demo/acceptance run can show the most recent errors and their context.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, error: AppError | str, message: str | None = None,
               details: dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(error, AppError):
            item = error.to_record()
        else:
            item = {"error_code": error, "message": message or error, "details": details or {}}
        with self._lock:
            self._records.append(item)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        return item

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._records[-1]) if self._records else None

    def codes(self) -> list[str]:
        with self._lock:
            return [item["error_code"] for item in self._records]


def error_response(error: AppError | Exception) -> dict[str, Any]:
    if isinstance(error, AppError):
        return {"ok": False, **error.to_record()}
    return {
        "ok": False,
        "error_code": "E_INTERNAL",
        "message": str(error),
        "details": {},
    }
