from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "context": self.context,
            },
        }


def fail(code: str, message: str, **context: Any) -> "None":
    raise WorkbenchError(code=code, message=message, context=context)


# Stable, externally documented business error codes.
E_INPUT_IO = "E_INPUT_IO"
E_INPUT_JSON = "E_INPUT_JSON"
E_INPUT_SCHEMA = "E_INPUT_SCHEMA"
E_DUP_JSON_KEY = "E_DUP_JSON_KEY"
E_DUP_EXPERIMENT_ID = "E_DUP_EXPERIMENT_ID"
E_PARENT_MISSING = "E_PARENT_MISSING"
E_LINEAGE_CYCLE = "E_LINEAGE_CYCLE"
E_HYPERPARAM_TYPE = "E_HYPERPARAM_TYPE"
E_METRIC_REFERENCE = "E_METRIC_REFERENCE"
E_METRIC_SEQ = "E_METRIC_SEQ"
E_METRIC_ID_DUP = "E_METRIC_ID_DUP"
E_METRIC_STEP = "E_METRIC_STEP"
E_METRIC_VALUE = "E_METRIC_VALUE"
E_METRIC_CLOCK = "E_METRIC_CLOCK"
E_METRIC_DIGEST = "E_METRIC_DIGEST"
E_V1_ALIAS_DUP = "E_V1_ALIAS_DUP"
E_V1_FINGERPRINT = "E_V1_FINGERPRINT"
E_V1_REFRESH = "E_V1_REFRESH"
E_V2_ALIAS_DUP = "E_V2_ALIAS_DUP"
E_V2_HASH = "E_V2_HASH"
E_V2_GENERATION = "E_V2_GENERATION"
E_V2_METADATA = "E_V2_METADATA"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CONFLICT = "E_SNAPSHOT_CONFLICT"
E_SNAPSHOT_INCOMPLETE = "E_SNAPSHOT_INCOMPLETE"
E_CLOCK_REGRESSION = "E_CLOCK_REGRESSION"
E_WINDOW_ARGUMENT = "E_WINDOW_ARGUMENT"
E_WINDOW_ASSIGNMENT = "E_WINDOW_ASSIGNMENT"
E_PATCH_INVALID = "E_PATCH_INVALID"
E_PATCH_CONFLICT = "E_PATCH_CONFLICT"
E_NOT_LOADED = "E_NOT_LOADED"
E_STATE_CORRUPT = "E_STATE_CORRUPT"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"
E_DB_BUSY = "E_DB_BUSY"
E_PENDING_RECOVERY = "E_PENDING_RECOVERY"
E_VERIFY_MISMATCH = "E_VERIFY_MISMATCH"

