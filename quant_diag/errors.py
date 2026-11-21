from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_INVALID_MANIFEST = "E_INVALID_MANIFEST"
E_REVISION_ROLLBACK = "E_REVISION_ROLLBACK"
E_WEIGHT_TRUNCATED = "E_WEIGHT_TRUNCATED"
E_INVALID_QUANT_DTYPE = "E_INVALID_QUANT_DTYPE"
E_LAYER_INCOMPLETE = "E_LAYER_INCOMPLETE"
E_CALIBRATION_SHAPE_MISMATCH = "E_CALIBRATION_SHAPE_MISMATCH"
E_INPUT_DIM_MISMATCH = "E_INPUT_DIM_MISMATCH"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_PARTIAL = "E_SNAPSHOT_PARTIAL"
E_INCONSISTENT = "E_INCONSISTENT"
E_INVALID_CONFIG = "E_INVALID_CONFIG"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    stage: str | None = None
    layer_id: str | None = None
    layer_index: int | None = None
    version: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def push_context(self, frame: str) -> "BusinessError":
        if not self.trace or self.trace[-1] != frame:
            self.trace.append(frame)
        return self

    def with_context(
        self,
        frame: str,
        *,
        stage: str | None = None,
        layer_id: str | None = None,
        layer_index: int | None = None,
        version: int | None = None,
    ) -> "BusinessError":
        if self.stage is None and stage is not None:
            self.stage = stage
        if self.layer_id is None and layer_id is not None:
            self.layer_id = layer_id
        if self.layer_index is None and layer_index is not None:
            self.layer_index = layer_index
        if self.version is None and version is not None:
            self.version = version
        self.push_context(frame)
        return self

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "layer_id": self.layer_id,
            "layer_index": self.layer_index,
            "version": self.version,
            "details": self.details,
            "trace": list(self.trace),
        }
        return payload

    def __str__(self) -> str:
        suffix = []
        if self.stage:
            suffix.append(f"stage={self.stage}")
        if self.layer_id:
            suffix.append(f"layer={self.layer_id}")
        if self.version is not None:
            suffix.append(f"version={self.version}")
        loc = " ".join(suffix)
        return f"{self.code}: {self.message}" + (f" [{loc}]" if loc else "")


def as_business_error(exc: BaseException) -> BusinessError:
    if isinstance(exc, BusinessError):
        return exc
    return BusinessError(
        "E_INTERNAL",
        str(exc),
        details={"exception_type": type(exc).__name__},
        trace=["internal"],
    )
