"""结构化业务错误码与异常定义。

业务码全部为字符串常量，便于跨进程（pickle）与 JSON 快照持久化；
任何业务失败都可以还原为 ``(code, message, context)`` 三元组。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_COMPUTE_BUDGET = "E_COMPUTE_BUDGET"
E_MEMORY_BUDGET = "E_MEMORY_BUDGET"
E_SLOT_BUDGET = "E_SLOT_BUDGET"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_WORKER_FAILURE = "E_WORKER_FAILURE"
E_INCONSISTENT = "E_INCONSISTENT"

E_BUSINESS_CODES = frozenset(
    [
        E_CONFIG_INVALID,
        E_COMPUTE_BUDGET,
        E_MEMORY_BUDGET,
        E_SLOT_BUDGET,
        E_STAGE_BUSY,
        E_SNAPSHOT_CORRUPT,
        E_SNAPSHOT_ROLLBACK,
        E_WORKER_FAILURE,
        E_INCONSISTENT,
    ]
)


@dataclass(frozen=True)
class BusinessError(Exception):
    """带业务码与上下文的异常，可安全跨进程传递。"""

    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志展示
        return f"{self.code}: {self.message}"


class EConfigInvalid(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_CONFIG_INVALID, message, dict(context))


class EComputeBudget(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_COMPUTE_BUDGET, message, dict(context))


class EMemoryBudget(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_MEMORY_BUDGET, message, dict(context))


class ESlotBudget(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_SLOT_BUDGET, message, dict(context))


class EStageBusy(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_STAGE_BUSY, message, dict(context))


class ESnapshotCorrupt(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_SNAPSHOT_CORRUPT, message, dict(context))


class ESnapshotRollback(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_SNAPSHOT_ROLLBACK, message, dict(context))


class EWorkerFailure(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_WORKER_FAILURE, message, dict(context))


class EInconsistent(BusinessError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_INCONSISTENT, message, dict(context))
