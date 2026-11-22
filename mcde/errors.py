from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """A business or infrastructure error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


class SchemaError(EngineError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_SCHEMA", message, details=details)


class FileError(EngineError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_FILE", message, details=details)


class TypeMismatchError(EngineError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_TYPE", message, details=details)


class LimitError(EngineError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_LIMIT", message, details=details)


class ReferenceError(EngineError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(code, message, details=details)


class StateError(EngineError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(code, message, details=details, exit_code=3)


# Stable business codes used across validation, planning, dispatch and recovery.
E_USAGE = "E_USAGE"
E_FILE = "E_FILE"
E_PARSE = "E_PARSE"
E_SCHEMA = "E_SCHEMA"
E_TYPE = "E_TYPE"
E_LIMIT = "E_LIMIT"
E_DUP_APP = "E_DUP_APP"
E_DUP_REGION = "E_DUP_REGION"
E_DUP_CLUSTER = "E_DUP_CLUSTER"
E_DUP_APPROVAL = "E_DUP_APPROVAL"
E_REF_REGION = "E_REF_REGION"
E_REF_CLUSTER = "E_REF_CLUSTER"
E_REF_CLUSTER_REGION = "E_REF_CLUSTER_REGION"
E_REF_APP = "E_REF_APP"
E_CLOCK = "E_CLOCK"

E_OVERRIDE_CONFLICT = "E_OVERRIDE_CONFLICT"
E_APPROVAL_REQUIRED = "E_APPROVAL_REQUIRED"
E_APPROVAL_EXPIRED = "E_APPROVAL_EXPIRED"
E_APPROVAL_NOT_YET_VALID = "E_APPROVAL_NOT_YET_VALID"
E_APPROVAL_SCOPE_MISMATCH = "E_APPROVAL_SCOPE_MISMATCH"
E_APPROVAL_ALREADY_CONSUMED = "E_APPROVAL_ALREADY_CONSUMED"
E_QUOTA_REPLICAS = "E_QUOTA_REPLICAS"
E_QUOTA_RESOURCES = "E_QUOTA_RESOURCES"

E_WINDOW_CLOSED = "E_WINDOW_CLOSED"
E_VERSION_ROLLBACK = "E_VERSION_ROLLBACK"
E_VERSION_CONFLICT = "E_VERSION_CONFLICT"
E_NOT_LOADED = "E_NOT_LOADED"
E_NO_ACTIVE_VERSION = "E_NO_ACTIVE_VERSION"
E_STATE_CONFLICT = "E_STATE_CONFLICT"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"
E_UNEXPLAINED_DRIFT = "E_UNEXPLAINED_DRIFT"

