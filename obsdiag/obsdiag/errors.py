"""业务错误码与强校验异常。

所有加载/注入阶段的确定性失败均抛出 :class:`ObsDiagError`，
错误码以 ``E_`` 前缀开头，错误信息固定可复现，不包含运行时随机内容。
"""
from __future__ import annotations

from dataclasses import dataclass


E_INPUT = "E_INPUT"
E_ORDER = "E_ORDER"
E_TOPO = "E_TOPO"
E_GAP = "E_GAP"
E_STATE = "E_STATE"
E_VERSION = "E_VERSION"
E_INTERNAL = "E_INTERNAL"


@dataclass(frozen=True)
class ValidationFault:
    """单条校验失败记录（首个出错记录）。"""

    code: str
    line_number: int
    field_path: str
    expected: str
    actual: str

    def format(self) -> str:
        return (
            f"{self.code} line={self.line_number} field={self.field_path} "
            f"expected={self.expected} actual={self.actual}"
        )


class ObsDiagError(Exception):
    """工作台统一异常，携带业务码与结构化故障信息。"""

    def __init__(self, code: str, message: str, fault: ValidationFault | None = None):
        super().__init__(message)
        self.code = code
        self.fault = fault

    def render(self) -> str:
        if self.fault is None:
            return f"{self.code}: {self.args[0]}"
        return f"{self.fault.format()} message={self.args[0]}"
