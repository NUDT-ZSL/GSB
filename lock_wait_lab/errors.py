"""Business errors with stable codes and first-invalid-field reporting."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


class ErrorCode:
    CLI = "E_CLI"
    INTERNAL = "E_INTERNAL"
    SAMPLE_MISSING = "E_SAMPLE_MISSING"
    JSON_INVALID = "E_JSON_INVALID"
    SCHEMA_INVALID = "E_SCHEMA_INVALID"
    DB_DUPLICATE_TABLE = "E_DB_DUPLICATE_TABLE"
    DB_DUPLICATE_COLUMN = "E_DB_DUPLICATE_COLUMN"
    DB_COLUMN_INVALID = "E_DB_COLUMN_INVALID"
    DB_INDEX_INVALID = "E_DB_INDEX_INVALID"
    DB_KEY_INVALID = "E_DB_KEY_INVALID"
    DB_FOREIGN_KEY_INVALID = "E_DB_FOREIGN_KEY_INVALID"
    DB_DATA_INVALID = "E_DB_DATA_INVALID"
    DB_SQLITE_MISMATCH = "E_DB_SQLITE_MISMATCH"
    TX_DUPLICATE_ID = "E_TX_DUPLICATE_ID"
    TX_FIELD_INVALID = "E_TX_FIELD_INVALID"
    TX_ISOLATION_INVALID = "E_TX_ISOLATION_INVALID"
    TX_OPERATION_INVALID = "E_TX_OPERATION_INVALID"
    TX_LOCK_INVALID = "E_TX_LOCK_INVALID"
    POLICY_V1_INVALID = "E_POLICY_V1_INVALID"
    POLICY_V2_INVALID = "E_POLICY_V2_INVALID"
    POLICY_MONOTONIC = "E_POLICY_MONOTONIC"
    POLICY_SUMMARY = "E_POLICY_SUMMARY"
    POLICY_TOMBSTONE = "E_POLICY_TOMBSTONE"
    POLICY_VERSION_ROLLBACK = "E_POLICY_VERSION_ROLLBACK"
    POLICY_NOT_EFFECTIVE = "E_POLICY_NOT_EFFECTIVE"
    STATE_MISSING = "E_STATE_MISSING"
    STATE_INVALID = "E_STATE_INVALID"
    RUN_INCOMPLETE = "E_RUN_INCOMPLETE"
    VERIFY_FAILED = "E_VERIFY_FAILED"


@dataclass
class LabError(Exception):
    code: str
    message: str
    field: str | None = None
    path: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __str__(self) -> str:
        location = self.path or ""
        if self.field:
            location = f"{location}.{self.field}" if location else self.field
        suffix = f" [{location}]" if location else ""
        return f"{self.code}: {self.message}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            error["field"] = self.field
        if self.path is not None:
            error["path"] = self.path
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


def require(condition: bool, code: str, message: str, field: str | None = None,
            path: str | None = None, details: dict[str, Any] | None = None) -> None:
    if not condition:
        raise LabError(
            code=code,
            message=message,
            field=field,
            path=path,
            details=details or {},
        )
