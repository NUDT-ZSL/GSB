from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_CODES: dict[str, str] = {
    "E_BAD_BATCH": "Columnar batch input is invalid.",
    "E_BAD_PLAN": "Logical plan input is invalid.",
    "E_BAD_CONFIG": "Execution configuration is invalid.",
    "E_PLAN_CYCLE": "Logical plan contains a cycle.",
    "E_PLAN_REF": "Logical plan references a missing node or column.",
    "E_DUPLICATE_NODE": "Logical plan contains duplicate node identifiers.",
    "E_REWRITE_MISMATCH": "Rewritten plan is not bitwise equivalent to the original plan.",
    "E_DIGEST_MISMATCH": "A shard or operator-state digest does not match persisted content.",
    "E_FIXED_OVERFLOW": "Fixed-point conversion overflowed int64.",
    "E_BUFFER_BUDGET": "An operator buffer exceeds its configured memory budget.",
    "E_CLOCK_REGRESSION": "The logical clock moved backwards.",
    "E_RUN_CONFLICT": "Run identifier is already bound to different inputs.",
    "E_SHARD_FAILURE": "A started shard failed with an explicit execution error.",
    "E_WINDOW_ORDER": "Window input order regressed across committed shards.",
    "E_COMMIT_CONFLICT": "The same shard state version was committed with different content.",
    "E_BAD_JSON": "JSON input is malformed or contains duplicate object keys.",
    "E_IO": "A local filesystem operation failed.",
    "E_STATE": "Persisted execution state is inconsistent.",
}


@dataclass
class EngineError(Exception):
    code: str
    message: str
    node_id: str | None = None
    shard_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown engine error code: {self.code}")
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.node_id is not None:
            result["node_id"] = self.node_id
        if self.shard_id is not None:
            result["shard_id"] = self.shard_id
        if self.details:
            result["details"] = self.details
        return result


def fail(
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    shard_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    raise EngineError(
        code,
        message,
        node_id=node_id,
        shard_id=shard_id,
        details=details or {},
    )


def require(condition: bool, code: str, message: str, **kwargs: Any) -> None:
    if not condition:
        fail(code, message, **kwargs)

