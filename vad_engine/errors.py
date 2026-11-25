"""Business error codes used across the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


OK = "OK"
E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_PARSE_ERROR = "E_PARSE_ERROR"
E_TIMELINE_OVERLAP = "E_TIMELINE_OVERLAP"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STATE_INCONSISTENT = "E_STATE_INCONSISTENT"
E_PRECISION_MISMATCH = "E_PRECISION_MISMATCH"
E_REPLAY_MISMATCH = "E_REPLAY_MISMATCH"

D_ENDPOINT_SHIFT = "D_ENDPOINT_SHIFT"
D_ENERGY_DRIFT = "D_ENERGY_DRIFT"
D_RULE_CURVE_DRIFT = "D_RULE_CURVE_DRIFT"
D_NONE = "D_NONE"

S_STAGE_COMMITTED = "S_STAGE_COMMITTED"
S_STAGE_REUSED = "S_STAGE_REUSED"
M_V1_TO_V2_OK = "M_V1_TO_V2_OK"
M_V2_TO_V1_OK = "M_V2_TO_V1_OK"
M_DROP_GENERATION = "M_DROP_GENERATION_UNSUPPORTED"
M_DROP_TOMBSTONE = "M_DROP_SCOPE_TOMBSTONE_UNSUPPORTED"
M_DROP_SCOPE = "M_DROP_SCOPE_UNSUPPORTED"
CACHE_REUSED = "CACHE_REUSED"
CACHE_REBUILT_WHOLE = "CACHE_REBUILT_WHOLE"
R_DEGRADED_SERIAL = "R_DEGRADED_PARALLEL_TO_SERIAL"


@dataclass
class EngineError(RuntimeError):
    """An expected, machine-readable engine failure."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        if self.details:
            return f"{self.code}: {self.message} ({self.details})"
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.code,
            "message": self.message,
            "details": self.details,
        }


class SimulatedCrash(RuntimeError):
    """Test-only injected termination, intentionally not an EngineError."""

