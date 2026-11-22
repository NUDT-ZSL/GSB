from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_QUANT_DTYPE = "E_QUANT_DTYPE"
E_SCALE_INVALID = "E_SCALE_INVALID"
E_ZERO_POINT_RANGE = "E_ZERO_POINT_RANGE"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_NO_CHECKPOINT = "E_NO_CHECKPOINT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_UNATTRIBUTED = "E_UNATTRIBUTED"
E_MANIFEST_INVALID = "E_MANIFEST_INVALID"
E_MANIFEST_MISMATCH = "E_MANIFEST_MISMATCH"
E_REVISION_ROLLBACK = "E_REVISION_ROLLBACK"
E_RECOVERY_MISMATCH = "E_RECOVERY_MISMATCH"
E_INVALID_CONFIG = "E_INVALID_CONFIG"
E_IO = "E_IO"


@dataclass
class BusinessError(Exception):
    """A stable business error that can cross CLI, API and UI boundaries."""

    code: str
    message: str
    stage: str | None = None
    layer_id: str | None = None
    layer_index: int | None = None
    version: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    @property
    def business_code(self) -> str:
        return self.code

    def with_context(
        self,
        *,
        stage: str | None = None,
        layer_id: str | None = None,
        layer_index: int | None = None,
        version: int | None = None,
    ) -> "BusinessError":
        if self.stage is None:
            self.stage = stage
        if self.layer_id is None:
            self.layer_id = layer_id
        if self.layer_index is None:
            self.layer_index = layer_index
        if self.version is None:
            self.version = version
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "layer_id": self.layer_id,
            "layer_index": self.layer_index,
            "version": self.version,
            "details": self.details,
        }


def attribute_stage(stage: str, exc: Exception) -> BusinessError:
    """Attach a pipeline stage to an exception without swallowing it."""
    if isinstance(exc, BusinessError):
        return exc.with_context(stage=stage)
    return BusinessError(
        code=E_UNATTRIBUTED,
        message=f"未归因异常: {type(exc).__name__}: {exc}",
        stage=stage,
        details={"exception_type": type(exc).__name__, "exception": str(exc)},
    )
