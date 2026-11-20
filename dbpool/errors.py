from __future__ import annotations

from typing import Any


class PoolError(Exception):
    """A stable business error with machine-readable diagnostics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.operation = operation
        self.details = {key: value for key, value in details.items() if value is not None}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.operation is not None:
            result["error"]["operation"] = self.operation
        if self.details:
            result["error"]["details"] = self.details
        return result


# Parsing and validation
E_INVALID_JSON = "E_INVALID_JSON"
E_INVALID_ENDPOINT = "E_INVALID_ENDPOINT"
E_DUPLICATE_ENDPOINT = "E_DUPLICATE_ENDPOINT"
E_INVALID_LOOPBACK_ADDRESS = "E_INVALID_LOOPBACK_ADDRESS"
E_INVALID_APPLICATION = "E_INVALID_APPLICATION"
E_DUPLICATE_APPLICATION = "E_DUPLICATE_APPLICATION"
E_INVALID_CONFIG = "E_INVALID_CONFIG"
E_POOL_BOUNDARY = "E_POOL_BOUNDARY"
E_INVALID_CLOCK_STEP = "E_INVALID_CLOCK_STEP"
E_TENANT_POOL_LABEL = "E_TENANT_POOL_LABEL"
E_UNKNOWN_POOL_LABEL = "E_UNKNOWN_POOL_LABEL"

# State, versioning and integrity
E_DATABASE_NOT_FOUND = "E_DATABASE_NOT_FOUND"
E_CONFIG_VERSION_ROLLBACK = "E_CONFIG_VERSION_ROLLBACK"
E_CONFIG_VERSION_CONFLICT = "E_CONFIG_VERSION_CONFLICT"
E_SAMPLE_DIGEST_MISMATCH = "E_SAMPLE_DIGEST_MISMATCH"
E_APPLICATION_CONFLICT = "E_APPLICATION_CONFLICT"
E_CLOCK_REGRESSION = "E_CLOCK_REGRESSION"
E_LEASE_NOT_FOUND = "E_LEASE_NOT_FOUND"
E_STATE_INCONSISTENT = "E_STATE_INCONSISTENT"

# Endpoint and build failures
E_ENDPOINT_REFUSED = "E_ENDPOINT_REFUSED"
E_ENDPOINT_TIMEOUT = "E_ENDPOINT_TIMEOUT"
E_ENDPOINT_UNHEALTHY = "E_ENDPOINT_UNHEALTHY"
E_ENDPOINT_UNAVAILABLE = "E_ENDPOINT_UNAVAILABLE"
E_BUILD_BUDGET_EXCEEDED = "E_BUILD_BUDGET_EXCEEDED"
E_MALFORMED_PROBE = "E_MALFORMED_PROBE"

# Queueing is a normal outcome, but it has a diagnostic code for audit records.
Q_CAPACITY = "Q_CAPACITY"
Q_TENANT_QUOTA = "Q_TENANT_QUOTA"
Q_ENDPOINT_CAPACITY = "Q_ENDPOINT_CAPACITY"
Q_ENDPOINT_UNHEALTHY = "Q_ENDPOINT_UNHEALTHY"
Q_FAIRNESS = "Q_FAIRNESS"
