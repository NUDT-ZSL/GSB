"""Business error codes for the recovery pipeline.

Every validation / execution failure surfaces as :class:`BizError` carrying a
stable machine-readable ``code`` plus diagnostic fields.  Illegal input must
never leave partial persistent state behind: loaders are pure, and the
executor validates before opening a transaction.
"""

from __future__ import annotations

from typing import Any


class Err:
    # v1 parsing / validation
    V1_DUPLICATE_SERVICE = "E_V1_DUPLICATE_SERVICE"
    V1_INVALID_ROLLBACK_SECONDS = "E_V1_INVALID_ROLLBACK_SECONDS"
    V1_NON_MONOTONIC_STEPS = "E_V1_NON_MONOTONIC_STEPS"
    V1_MAPPING_INCONSISTENT = "E_V1_MAPPING_INCONSISTENT"
    # v2 parsing / validation
    V2_SHARD_HASH_MISMATCH = "E_V2_SHARD_HASH_MISMATCH"
    V2_GENERATION_REGRESSION = "E_V2_GENERATION_REGRESSION"
    V2_INVALID_EFFECTIVE_MS = "E_V2_INVALID_EFFECTIVE_MS"
    V2_DANGLING_DEPENDENCY = "E_V2_DANGLING_DEPENDENCY"
    # logical clock
    CLOCK_ROLLBACK_EXCEEDED = "E_CLOCK_ROLLBACK_EXCEEDED"
    # action dispatch target
    TARGET_CONN_REFUSED = "E_TARGET_CONN_REFUSED"
    TARGET_TIMEOUT = "E_TARGET_TIMEOUT"
    TARGET_VERSION_ECHO_MISMATCH = "E_TARGET_VERSION_ECHO_MISMATCH"
    # integrity / topology
    DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
    DEPENDENCY_CYCLE = "E_DEPENDENCY_CYCLE"
    # config package management
    PACKAGE_VERSION_REGRESSION = "E_PACKAGE_VERSION_REGRESSION"


class BizError(Exception):
    """Business error with a stable code and diagnostic fields."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.fields = fields

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "fields": self.fields}
