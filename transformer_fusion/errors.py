"""Business error codes and lightweight diagnostic records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


E_OP_UNKNOWN = "E_OP_UNKNOWN"
E_AXIS_INVALID = "E_AXIS_INVALID"
E_MANIFEST_INVALID = "E_MANIFEST_INVALID"
E_INPUT_SHAPE = "E_INPUT_SHAPE"
E_INPUT_MISSING = "E_INPUT_MISSING"
E_BLOCK_SIZE_INVALID = "E_BLOCK_SIZE_INVALID"
E_EPS_INVALID = "E_EPS_INVALID"
E_DTYPE_UNSUPPORTED = "E_DTYPE_UNSUPPORTED"
E_POLICY_INVALID = "E_POLICY_INVALID"
E_FUSION_INFEASIBLE = "E_FUSION_INFEASIBLE"
E_REDUCTION_DEPENDENCY = "E_REDUCTION_DEPENDENCY"
E_NOT_ELEMENTWISE = "E_NOT_ELEMENTWISE"
E_NO_ADJACENT_PAIR = "E_NO_ADJACENT_PAIR"
E_NON_FINITE_INPUT = "E_NON_FINITE_INPUT"
E_TOLERANCE_CONFIG = "E_TOLERANCE_CONFIG"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_WORKSPACE_BUSY = "E_WORKSPACE_BUSY"
E_WORKSPACE_NOT_WRITABLE = "E_WORKSPACE_NOT_WRITABLE"
E_INCONSISTENT = "E_INCONSISTENT"
E_INJECTION_INVALID = "E_INJECTION_INVALID"
E_POLICY_MEMORY = "E_POLICY_MEMORY"
E_POLICY_LOCALITY = "E_POLICY_LOCALITY"


@dataclass
class Diagnostic:
    """A serializable stage-scoped business record."""

    code: str
    case_id: str
    stage: str
    message: str
    severity: str = "error"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "case_id": self.case_id,
            "stage": self.stage,
            "message": self.message,
            "severity": self.severity,
            "details": self.details,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Diagnostic":
        return cls(
            code=str(value["code"]),
            case_id=str(value["case_id"]),
            stage=str(value["stage"]),
            message=str(value.get("message", "")),
            severity=str(value.get("severity", "error")),
            details=dict(value.get("details", {})),
        )
