"""Business error types shared by the deterministic classification pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_CONFIG = "E_CONFIG"
E_INJECTION = "E_INJECTION"
E_INJECTION_TARGET = "E_INJECTION_TARGET"
E_INCONSISTENT = "E_INCONSISTENT"
E_LABEL_INDEX = "E_LABEL_INDEX"
E_METRIC_UNDEFINED = "E_METRIC_UNDEFINED"
E_PIXEL_RANGE = "E_PIXEL_RANGE"
E_PROJECTION_SHAPE = "E_PROJECTION_SHAPE"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SAMPLE = "E_SAMPLE"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_VERSION = "E_SNAPSHOT_VERSION"
E_THRESHOLD = "E_THRESHOLD"
E_UNATTRIBUTED = "E_UNATTRIBUTED"
E_VERIFY_FAIL = "E_VERIFY_FAIL"


@dataclass
class LabError(Exception):
    """A deterministic, serializable business error."""

    code: str
    message: str
    stage: str | None = None
    sample_id: str | None = None
    label: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "sample_id": self.sample_id,
            "label": self.label,
            "details": self.details,
        }


class SnapshotCorrupt(LabError):
    def __init__(
        self,
        message: str,
        *,
        category: str,
        version: int | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.category = category
        self.version = version
        super().__init__(
            code=E_SNAPSHOT_CORRUPT,
            message=message,
            stage="snapshot",
            details={
                "category": category,
                "version": version,
                **(details or {}),
            },
        )


def error_report(error: LabError) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "error",
        "result_code": error.code,
        "business_codes": [error.code],
        "errors": [error.to_dict()],
    }
