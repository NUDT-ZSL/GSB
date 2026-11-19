"""Structured business errors used across process and snapshot boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_MANIFEST_INVALID = "E_MANIFEST_INVALID"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_STALE_VERSION = "E_STALE_VERSION"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_MISSING = "E_SNAPSHOT_MISSING"
E_SNAPSHOT_VERSION_CONFLICT = "E_SNAPSHOT_VERSION_CONFLICT"
E_COMPUTE_BUDGET = "E_COMPUTE_BUDGET"
E_MEMORY_BUDGET = "E_MEMORY_BUDGET"
E_SLOT_BUDGET = "E_SLOT_BUDGET"
E_INJECTED_FAULT = "E_INJECTED_FAULT"
E_WORKER_INTERNAL = "E_WORKER_INTERNAL"
E_WORKER_CRASH = "E_WORKER_CRASH"
E_EXECUTOR_UNAVAILABLE = "E_EXECUTOR_UNAVAILABLE"
E_INCONSISTENT = "E_INCONSISTENT"
E_VALIDATION_FAILED = "E_VALIDATION_FAILED"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    stage: str = "system"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.code,
            "message": self.message,
            "stage": self.stage,
            "details": self.details,
        }


class SnapshotCorruptError(BusinessError):
    def __init__(
        self,
        category: str,
        version: int | None,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "snapshot_category": category,
            "snapshot_version": version,
        }
        if details:
            payload.update(details)
        super().__init__(
            E_SNAPSHOT_CORRUPT,
            message,
            stage="snapshot_load",
            details=payload,
        )
