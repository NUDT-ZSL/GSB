"""跨任务迁移与适配的本地确定性调度组件。

包内所有模块仅依赖 Python 标准库与 NumPy；随机过程全部由显式种子驱动，
不读取系统时间，不依赖字典遍历顺序，进程池统一使用 ``spawn`` 启动方式。
"""

from .errors import (
    E_BUSINESS_CODES,
    BusinessError,
    EComputeBudget,
    EConfigInvalid,
    EInconsistent,
    ESlotBudget,
    EMemoryBudget,
    ESnapshotCorrupt,
    ESnapshotRollback,
    EStageBusy,
    EWorkerFailure,
)

__all__ = [
    "E_BUSINESS_CODES",
    "BusinessError",
    "EComputeBudget",
    "EConfigInvalid",
    "EInconsistent",
    "ESlotBudget",
    "EMemoryBudget",
    "ESnapshotCorrupt",
    "ESnapshotRollback",
    "EStageBusy",
    "EWorkerFailure",
]

__version__ = "1.0.0"
