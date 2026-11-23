"""业务错误码与统一异常类型。

所有跨模块传播的业务异常都使用 :class:`BenchError`，它携带稳定的错误码、
请求标识、所处阶段与可读原因，便于诊断工作台沿异常链路归因。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


E_DUP_REQUEST = "E_DUP_REQUEST"
E_SAMPLING_INVALID = "E_SAMPLING_INVALID"
E_LENGTH_INVALID = "E_LENGTH_INVALID"
E_SEED_INVALID = "E_SEED_INVALID"
E_VOCAB_VERSION_MISSING = "E_VOCAB_VERSION_MISSING"

E_STEP_BUDGET = "E_STEP_BUDGET"
E_CACHE_BUDGET = "E_CACHE_BUDGET"
E_SLOT_BUDGET = "E_SLOT_BUDGET"

E_BATCH_MISMATCH = "E_BATCH_MISMATCH"
E_INCONSISTENT = "E_INCONSISTENT"

E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"


@dataclass
class BenchError(Exception):
    """携带结构化归因信息的业务异常。"""

    code: str
    reason: str
    request_id: str | None = None
    stage: str | None = None
    field: str | None = None
    detail: Any = None

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        parts = [self.code, self.reason]
        if self.request_id:
            parts.append(f"request={self.request_id}")
        if self.stage:
            parts.append(f"stage={self.stage}")
        if self.field:
            parts.append(f"field={self.field}")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.reason,
            "request_id": self.request_id,
            "stage": self.stage,
            "field": self.field,
            "detail": self.detail,
        }


def raise_error(
    code: str,
    reason: str,
    *,
    request_id: str | None = None,
    stage: str | None = None,
    field: str | None = None,
    detail: Any = None,
) -> "BenchError":
    """构造一个 :class:`BenchError`（不抛出），便于调用方记录或抛出。"""
    return BenchError(code, reason, request_id, stage, field, detail)
