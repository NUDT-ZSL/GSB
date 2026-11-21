from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


STAGE_LABELS = {
    "model_validation": "模型包校验",
    "quantization": "逐层量化",
    "calibration": "校准统计",
    "inference": "逐层推理",
}

STAGES = tuple(STAGE_LABELS)


@dataclass
class DeployError(Exception):
    code: str
    stage: str
    message: str
    layer_id: str | None = None
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    injected: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        location = []
        if self.layer_id:
            location.append(f"layer={self.layer_id}")
        if self.field:
            location.append(f"field={self.field}")
        suffix = f" ({', '.join(location)})" if location else ""
        return f"{self.code}@[{self.stage}] {self.message}{suffix}"

    def to_record(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "layer_id": self.layer_id,
            "field": self.field,
            "message": self.message,
            "details": self.details,
            "injected": self.injected,
            "attributed": True,
        }


E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"
E_QUANT_SCHEME = "E_QUANT_SCHEME"
E_PACKAGE_MANIFEST_TRUNCATED = "E_PACKAGE_MANIFEST_TRUNCATED"
E_PACKAGE_SCHEMA = "E_PACKAGE_SCHEMA"
E_PACKAGE_WEIGHT_TRUNCATED = "E_PACKAGE_WEIGHT_TRUNCATED"
E_PACKAGE_WEIGHT_SHAPE = "E_PACKAGE_WEIGHT_SHAPE"
E_PACKAGE_CALIBRATION_MISSING = "E_PACKAGE_CALIBRATION_MISSING"
E_CALIB_SAMPLE_MISMATCH = "E_CALIB_SAMPLE_MISMATCH"
E_CALIBRATION_TRUNCATED = "E_CALIBRATION_TRUNCATED"
E_INFER_DIM_MISMATCH = "E_INFER_DIM_MISMATCH"
E_STAGE_SEQUENCE = "E_STAGE_SEQUENCE"
E_PARTIAL_STAGE = "E_PARTIAL_STAGE"
E_MANIFEST_CHANGED_MID_RUN = "E_MANIFEST_CHANGED_MID_RUN"
E_UNATTRIBUTED = "E_UNATTRIBUTED"


def deploy_error_from_record(record: dict[str, Any]) -> DeployError:
    return DeployError(
        code=str(record.get("code", E_UNATTRIBUTED)),
        stage=str(record.get("stage", "unknown")),
        message=str(record.get("message", "")),
        layer_id=record.get("layer_id"),
        field=record.get("field"),
        details=record.get("details") or {},
        injected=bool(record.get("injected", False)),
    )

