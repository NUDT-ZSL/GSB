"""Business errors with stable codes and machine-readable diagnostics."""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """An expected validation or pipeline failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        view: str | None = None,
        node_id: str | None = None,
        edit_round: int | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.view = view
        self.node_id = node_id
        self.edit_round = edit_round
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.view is not None:
            payload["view"] = self.view
        if self.node_id is not None:
            payload["node_id"] = self.node_id
        if self.edit_round is not None:
            payload["edit_round"] = self.edit_round
        for key in (
            "patch_id",
            "missing_column",
            "reference_path",
            "logical_clock_ms",
            "watermark_ms",
            "tolerance_ms",
            "source",
            "column",
            "dependency_path",
        ):
            if key in self.details:
                payload[key] = self.details[key]
        if self.details:
            payload["details"] = self.details
        return payload


E_INPUT_JSON = "E_INPUT_JSON"
E_INPUT_SCHEMA = "E_INPUT_SCHEMA"
E_VIEW_NAME_EMPTY = "E_VIEW_NAME_EMPTY"
E_VIEW_NAME_INVALID = "E_VIEW_NAME_INVALID"
E_VIEW_NAME_DUPLICATE = "E_VIEW_NAME_DUPLICATE"
E_VIEW_MISSING = "E_VIEW_MISSING"
E_SQL_SYNTAX = "E_SQL_SYNTAX"
E_SQL_UNSUPPORTED = "E_SQL_UNSUPPORTED"
E_SQL_DUPLICATE_COLUMN = "E_SQL_DUPLICATE_COLUMN"
E_SQL_AMBIGUOUS_COLUMN = "E_SQL_AMBIGUOUS_COLUMN"
E_COLUMN_UNKNOWN = "E_COLUMN_UNKNOWN"
E_DEPENDENCY_MISSING = "E_DEPENDENCY_MISSING"
E_DEPENDENCY_SELF = "E_DEPENDENCY_SELF"
E_DEPENDENCY_CYCLE = "E_DEPENDENCY_CYCLE"
E_DEPENDENCY_DECLARED_MISMATCH = "E_DEPENDENCY_DECLARED_MISMATCH"
E_LOGIC_CLOCK_INVALID = "E_LOGIC_CLOCK_INVALID"
E_LOGIC_CLOCK_REGRESSION = "E_LOGIC_CLOCK_REGRESSION"
E_V1_TIME_INVALID = "E_V1_TIME_INVALID"
E_V1_COLUMN_UNKNOWN = "E_V1_COLUMN_UNKNOWN"
E_V2_META_MISMATCH = "E_V2_META_MISMATCH"
E_SHARD_HASH_MISMATCH = "E_SHARD_HASH_MISMATCH"
E_PLAN_GENERATION_REGRESSION = "E_PLAN_GENERATION_REGRESSION"
E_PLAN_INTEGRITY = "E_PLAN_INTEGRITY"
E_DOWNSTREAM_COLUMN_MISSING = "E_DOWNSTREAM_COLUMN_MISSING"
E_PATCH_INVALID = "E_PATCH_INVALID"
E_PATCH_PAYLOAD_CONFLICT = "E_PATCH_PAYLOAD_CONFLICT"
E_PATCH_SEQUENCE_CONFLICT = "E_PATCH_SEQUENCE_CONFLICT"
E_PATCH_TARGET_MISSING = "E_PATCH_TARGET_MISSING"
E_PATCH_TARGET_EXISTS = "E_PATCH_TARGET_EXISTS"
E_PATCH_IDEMPOTENT = "E_PATCH_IDEMPOTENT"
E_STATE_EXISTS = "E_STATE_EXISTS"
E_STATE_NOT_INITIALIZED = "E_STATE_NOT_INITIALIZED"
E_STATE_CONFLICT = "E_STATE_CONFLICT"
E_RESUME_CONFLICT = "E_RESUME_CONFLICT"
E_VERIFY_FAILED = "E_VERIFY_FAILED"
E_IO = "E_IO"

