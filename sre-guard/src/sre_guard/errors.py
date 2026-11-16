"""业务错误码与异常定义。

所有可预期的治理组件错误均以 :class:`GovernanceError` 抛出，
``code`` 为稳定的业务码字符串，``issues`` 携带可定位的字段路径、
文件行号与上下文，便于结构化日志与上层集成。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 策略文件非法（结构、取值、唯一性等校验失败）
E_POLICY = "E_POLICY"
#: 事件流非法（时间戳回退、结果枚举非法、耗时为负等）
E_EVENT = "E_EVENT"
#: 定点网格 / 缩放因子非法或缺失
E_GRID = "E_GRID"
#: 降级链不可用（权重和为零、目标全部不可用、链条耗尽）
E_DEGRADE = "E_DEGRADE"
#: 历史决策重放不可复现
E_REPLAY = "E_REPLAY"


@dataclass
class Issue:
    """单条校验问题。"""

    path: str
    message: str
    line: int | None = None
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "message": self.message}
        if self.line is not None:
            data["line"] = self.line
        if self.value is not None:
            data["value"] = self.value
        return data


class GovernanceError(Exception):
    """治理组件统一业务异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        issues: list[Issue] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues: list[Issue] = list(issues or [])
        self.context: dict[str, Any] = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "issues": [issue.to_dict() for issue in self.issues],
            "context": self.context,
        }

    def __str__(self) -> str:  # pragma: no cover - 调试便利
        detail = ""
        if self.issues:
            detail = "; ".join(
                f"[line {i.line} {i.path}] {i.message}" if i.line is not None
                else f"[{i.path}] {i.message}"
                for i in self.issues
            )
        return f"{self.code}: {self.message} {detail}".strip()
