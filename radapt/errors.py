from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    SCHEMA_INVALID = "E_SCHEMA_INVALID"
    JSON_INVALID = "E_JSON_INVALID"

    TABLE_DUPLICATE = "E_TABLE_NAME_DUPLICATE"
    TABLE_NOT_FOUND = "E_TABLE_NOT_FOUND"
    COLUMN_DUPLICATE = "E_COLUMN_NAME_DUPLICATE"
    COLUMN_TYPE_INVALID = "E_COLUMN_TYPE_INVALID"
    COLUMN_NOT_FOUND = "E_COLUMN_NOT_FOUND"
    NDV_INVALID = "E_NDV_INVALID"
    SKEW_RATIO_INVALID = "E_SKEW_RATIO_INVALID"
    SKEW_COLUMN_INVALID = "E_SKEW_COLUMN_INVALID"
    WINDOW_PARAM_INVALID = "E_WINDOW_PARAM_INVALID"
    GLOBAL_SEED_INVALID = "E_GLOBAL_SEED_INVALID"

    PLAN_INVALID = "E_PLAN_INVALID"
    WINDOW_DUPLICATE = "E_WINDOW_ID_DUPLICATE"
    WINDOW_NOT_FOUND = "E_WINDOW_NOT_FOUND"
    WINDOW_ORDER_INVALID = "E_WINDOW_ORDER_INVALID"
    OPERATOR_INVALID = "E_OPERATOR_INVALID"
    AGGREGATION_INVALID = "E_AGGREGATION_INVALID"
    COST_ESTIMATE_INVALID = "E_COST_ESTIMATE_INVALID"
    CANDIDATE_INVALID = "E_CANDIDATE_INVALID"

    BATCH_SEQ_NOT_INCREASING = "E_BATCH_SEQ_NOT_STRICTLY_INCREASING"
    BATCH_SEQ_INVALID = "E_BATCH_SEQ_INVALID"
    CLOCK_INVALID = "E_LOGIC_CLOCK_INVALID"
    BATCH_ROW_COUNT_INVALID = "E_BATCH_ROW_COUNT_INVALID"
    BATCH_WINDOW_MISMATCH = "E_BATCH_WINDOW_MISMATCH"
    BATCH_DIGEST_MISMATCH = "E_BATCH_DIGEST_MISMATCH"

    DRIFT_THRESHOLD_INVALID = "E_DRIFT_THRESHOLD_INVALID"
    COOLDOWN_INVALID = "E_COOLDOWN_INVALID"
    SWITCH_BUDGET_INVALID = "E_SWITCH_BUDGET_INVALID"
    PARALLEL_LIMIT_INVALID = "E_PARALLEL_TASK_LIMIT_INVALID"
    LOGICAL_BUDGET_INVALID = "E_LOGICAL_TIME_BUDGET_INVALID"
    MEMORY_BUDGET_INVALID = "E_MEMORY_BUDGET_INVALID"
    FIXED_SCALE_INVALID = "E_FIXED_POINT_SCALE_INVALID"
    TOLERANCE_INVALID = "E_COST_TOLERANCE_INVALID"

    DRIFT_NOT_TRIGGERED = "E_DRIFT_NOT_TRIGGERED"
    SWITCH_BUDGET_INSUFFICIENT = "E_SWITCH_BUDGET_INSUFFICIENT"
    SWITCH_NO_IMPROVEMENT = "E_SWITCH_NO_ESTIMATED_IMPROVEMENT"
    FORM_VERSION_ROLLBACK = "E_FORM_VERSION_ROLLBACK"
    FORM_VERSION_CONFLICT = "E_FORM_VERSION_CONFLICT"
    TASK_FAILED_AFTER_START = "E_TASK_FAILED_AFTER_START"
    ROUND_BUDGET_EXHAUSTED = "E_ROUND_BUDGET_EXHAUSTED"
    AGGREGATE_MISMATCH = "E_AGGREGATE_VALUE_MISMATCH"
    PERSISTENCE_INVALID = "E_PERSISTENCE_INVALID"
    CRASH_INJECTED = "E_CRASH_INJECTED"


@dataclass
class RadaptError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": _jsonable(self.details),
            },
        }


class ValidationError(RadaptError):
    pass


class BusinessRuleError(RadaptError):
    pass


class AggregateMismatchError(RadaptError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value
