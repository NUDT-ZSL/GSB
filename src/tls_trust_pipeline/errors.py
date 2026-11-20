from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PipelineError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": {"code": self.code, "message": self.message}}
        if self.details:
            result["error"]["details"] = self.details
        return result


E_BAD_SNAPSHOT = "E_BAD_SNAPSHOT"
E_FP_MISMATCH = "E_FP_MISMATCH"
E_BAD_PATCH = "E_BAD_PATCH"
E_PATCH_CONFLICT = "E_PATCH_CONFLICT"
E_SNAPSHOT_CONFLICT = "E_SNAPSHOT_CONFLICT"
E_VERSION_ROLLBACK = "E_VERSION_ROLLBACK"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_CERT_INVALID = "E_CERT_INVALID"
E_CHAIN_NOT_FOUND = "E_CHAIN_NOT_FOUND"
E_STATE = "E_STATE"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"
E_BAD_REQUEST = "E_BAD_REQUEST"
E_AUDIT_FAILED = "E_AUDIT_FAILED"

