"""业务错误码定义.

所有业务校验失败均抛出 GenError, 携带稳定错误码与结构化细节,
禁止以放宽比较或补丁式覆盖的方式掩盖差异.
"""

from __future__ import annotations

from typing import Any, Dict


E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_BLOCK_MISMATCH = "E_BLOCK_MISMATCH"
E_ANCHOR_STRENGTH = "E_ANCHOR_STRENGTH"
E_ANCHOR_SHAPE = "E_ANCHOR_SHAPE"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_STALE = "E_SNAPSHOT_STALE"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"


class GenError(Exception):
    """携带业务错误码与结构化细节的异常."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: Dict[str, Any] = details

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}
