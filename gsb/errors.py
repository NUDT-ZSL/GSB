"""业务错误码与统一异常。

所有可预期的输入/状态问题都抛出 :class:`GsbError`，CLI 映射为稳定的
JSON 错误体与非零退出码。错误码即跨模块契约，测试按码断言。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class Code:
    OK = "E_OK"
    BAD_STREAM = "E_BAD_STREAM"
    BAD_POLICY = "E_BAD_POLICY"
    BAD_CONFIG = "E_BAD_CONFIG"
    E_CHECKPOINT = "E_CHECKPOINT_WATERMARK"
    E_CONFLICT_POLICY = "E_CONFLICT_POLICY"
    E_VERSION_ROLLBACK = "E_VERSION_ROLLBACK"
    E_SNAPSHOT_CONFLICT = "E_SNAPSHOT_CONFLICT"
    E_ALREADY_DECIDED = "E_ALREADY_DECIDED"
    E_NOT_FOUND = "E_NOT_FOUND"
    E_STATE = "E_STATE"
    E_LOCK_BUSY = "E_LOCK_BUSY"
    E_RECOVERY = "E_RECOVERY"
    E_CRASH_INJECTED = "E_CRASH_INJECTED"
    E_INTERNAL = "E_INTERNAL"


# CLI 退出码：0 成功；2 业务校验/冲突；3 状态或锁；4 崩溃注入（模拟 SIGKILL 语义）
EXIT_BY_CODE = {
    Code.OK: 0,
    Code.BAD_STREAM: 2,
    Code.BAD_POLICY: 2,
    Code.BAD_CONFIG: 2,
    Code.E_CHECKPOINT: 2,
    Code.E_CONFLICT_POLICY: 2,
    Code.E_VERSION_ROLLBACK: 2,
    Code.E_SNAPSHOT_CONFLICT: 2,
    Code.E_NOT_FOUND: 3,
    Code.E_ALREADY_DECIDED: 3,
    Code.E_STATE: 3,
    Code.E_LOCK_BUSY: 3,
    Code.E_RECOVERY: 3,
    Code.E_CRASH_INJECTED: 137,
    Code.E_INTERNAL: 1,
}


class GsbError(Exception):
    """带业务错误码的异常。``details`` 必须可 JSON 序列化。"""

    def __init__(
        self,
        code: str = Code.E_INTERNAL,
        message: str = "internal error",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    @property
    def exit_code(self) -> int:
        return EXIT_BY_CODE.get(self.code, 1)


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise GsbError(code, message, details or None)
