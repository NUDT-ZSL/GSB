"""结构化业务错误码。

所有可预期的业务/资源/一致性问题都抛出 :class:`BusinessError`，
携带稳定字符串错误码、人读消息、出错字段路径与可选上下文，
便于上层做分支裁决与日志归因，而不是依赖异常类型字符串匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    """业务错误码常量。命名即契约，禁止随意改名。"""

    DUPLICATE_QUERY_ID = "E_DUP_QUERY_ID"
    DOC_TERM_OUT_OF_RANGE = "E_DOC_TERM_RANGE"
    INVALID_WEIGHT = "E_WEIGHT_NON_POSITIVE"
    INVALID_BUDGET = "E_BUDGET_NON_POSITIVE"
    INVALID_CONFIG = "E_CONFIG_INVALID"
    MISSING_FIELD = "E_MISSING_FIELD"
    RECALL_BUDGET_EXCEEDED = "E_RECALL_BUDGET"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    PRECISION_DRIFT = "E_PRECISION_DRIFT"
    CACHE_KEY_MISMATCH = "E_CACHE_KEY"
    CHECKSUM_MISMATCH = "E_CHECKSUM"
    VERSION_CONFLICT = "E_VERSION_CONFLICT"
    SNAPSHOT_BUSY = "E_SNAPSHOT_BUSY"
    SELF_CHECK_FAILED = "E_SELF_CHECK"


@dataclass
class BusinessError(Exception):
    """带错误码与字段路径的业务异常。"""

    code: str
    message: str
    field_path: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        head = f"[{self.code}] {self.message}"
        if self.field_path:
            head += f" (field={self.field_path})"
        return head

    def to_dict(self) -> dict[str, Any]:
        """序列化为结构化字典，可直接写入 JSON 日志。"""
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.field_path is not None:
            payload["field"] = self.field_path
        if self.context:
            payload["context"] = self.context
        return payload
