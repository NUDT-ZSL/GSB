from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    DUPLICATE_CASE_ID = "E_DUPLICATE_CASE_ID"
    SEQ_LEN_MISMATCH = "E_SEQ_LEN_MISMATCH"
    INVALID_HEAD_DIM = "E_INVALID_HEAD_DIM"
    INVALID_BLOCK_SIZE = "E_INVALID_BLOCK_SIZE"
    INVALID_HEAD_COUNT = "E_INVALID_HEAD_COUNT"
    INVALID_SEQ_LEN = "E_INVALID_SEQ_LEN"
    INVALID_REVISION = "E_INVALID_REVISION"
    CAUSAL_MASK_CONFLICT = "E_CAUSAL_MASK_CONFLICT"
    UNSUPPORTED_DTYPE = "E_UNSUPPORTED_DTYPE"
    DTYPE_MISMATCH = "E_DTYPE_MISMATCH"
    NONFINITE_INPUT = "E_NONFINITE_INPUT"
    INVALID_CONFIG = "E_INVALID_CONFIG"
    INVALID_INPUT = "E_INVALID_INPUT"
    SOFTMAX_NAN = "E_SOFTMAX_NAN"
    NORM_ZERO = "E_NORM_ZERO"
    CACHE_CORRUPT = "E_CACHE_CORRUPT"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    INCONSISTENT = "E_INCONSISTENT"
    GRAPH_CYCLE = "E_GRAPH_CYCLE"
    NODE_FAILURE = "E_NODE_FAILURE"
    UPSTREAM_FAILED = "E_UPSTREAM_FAILED"
    OUTPUT_MISSING = "E_OUTPUT_MISSING"


@dataclass
class Issue:
    code: str
    message: str
    case_id: str | None = None
    field: str | None = None
    node_id: str | None = None
    block_index: int | tuple[int, ...] | None = None
    category: str | None = None
    recovered: bool = False
    details: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        self.code = (
            self.code.value if isinstance(self.code, Enum) else str(self.code)
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "recovered": self.recovered,
        }
        for key in (
            "case_id",
            "field",
            "node_id",
            "block_index",
            "category",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.details:
            payload["details"] = self.details
        return payload


class BusinessError(Exception):
    def __init__(self, issue: Issue):
        super().__init__(issue.message)
        self.issue = issue


class CacheCorruptError(BusinessError):
    pass


class ResourceLimitError(BusinessError):
    pass
