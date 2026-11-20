"""Business codes, stage names, and structured diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Mapping

from .util import sha256_json


STAGE_INPUT = "input_validation"
STAGE_BLOCK_STATS = "block_statistics"
STAGE_NORMALIZATION = "normalization_accumulation"
STAGE_WRITEBACK = "output_writeback"
STAGES = (
    STAGE_INPUT,
    STAGE_BLOCK_STATS,
    STAGE_NORMALIZATION,
    STAGE_WRITEBACK,
)

STAGE_UNATTRIBUTED = "unattributed"
STAGE_COMPARISON = "tolerance_verification"
STAGE_SELF_CHECK = "cross_module_self_check"
STAGE_SNAPSHOT = "snapshot_management"

E_OP_UNKNOWN = "E_OP_UNKNOWN"
E_AXIS_INVALID = "E_AXIS_INVALID"
E_SHAPE_INVALID = "E_SHAPE_INVALID"
E_DTYPE_UNSUPPORTED = "E_DTYPE_UNSUPPORTED"
E_BLOCK_SIZE_INVALID = "E_BLOCK_SIZE_INVALID"
E_EPS_INVALID = "E_EPS_INVALID"
E_NORM_MODE_INVALID = "E_NORM_MODE_INVALID"
E_INPUT_NONFINITE = "E_INPUT_NONFINITE"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAT_OVERFLOW = "E_STAT_OVERFLOW"
E_WRITE_INDEX = "E_WRITE_INDEX"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_INCONSISTENT = "E_INCONSISTENT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_UNKNOWN = "E_SNAPSHOT_UNKNOWN"
E_UNATTRIBUTED = "E_UNATTRIBUTED"
E_CONFIG_INVALID = "E_CONFIG_INVALID"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    stage: str
    job_id: str
    field: str | None = None
    message: str = ""
    params: Mapping[str, Any] | None = None
    details: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "stage": self.stage,
            "job_id": self.job_id,
            "field": self.field,
            "message": self.message,
            "params_digest": (
                sha256_json(dict(self.params)) if self.params is not None else None
            ),
            "details": dict(self.details),
        }
        if self.params is not None:
            result["params"] = dict(self.params)
        return result


def degradation(
    category: str,
    stage: str,
    trigger_code: str,
    job_id: str,
    affected_blocks: list[str],
    strategy: str,
) -> dict[str, Any]:
    return {
        "category": category,
        "stage": stage,
        "trigger_code": trigger_code,
        "job_id": job_id,
        "affected_blocks": list(affected_blocks),
        "strategy": strategy,
    }
