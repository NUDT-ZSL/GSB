"""状态演进与恢复一致性诊断组件。

将一次状态演进建模为有向无环计算图：

初始状态校验 -> 分块状态变换 -> 跨块归约 -> 状态快照提交 -> 恢复裁决。

全部计算由固定种子驱动，具备确定性、可复现、可中断恢复与误差账本能力。
"""

from .errors import (
    DiagnosticError,
    EToleranceExceeded,
    ESnapshotRollback,
    EStageBusy,
    EInconsistent,
    EValidation,
    EBudgetExceeded,
)
from .clock import LogicalClock
from .memory import MemoryBudget

__all__ = [
    "DiagnosticError",
    "EToleranceExceeded",
    "ESnapshotRollback",
    "EStageBusy",
    "EInconsistent",
    "EValidation",
    "EBudgetExceeded",
    "LogicalClock",
    "MemoryBudget",
]

__version__ = "1.0.0"
