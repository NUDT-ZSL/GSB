"""Small immutable domain models shared by the application layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


IsolationLevel = Literal["read_committed", "repeatable_read"]
OperationName = Literal["read", "write", "commit", "rollback"]

ISOLATION_LEVELS = ("read_committed", "repeatable_read")
OPERATION_NAMES = ("read", "write", "commit", "rollback")
TERMINAL_OPERATIONS = ("commit", "rollback")


@dataclass(frozen=True)
class Operation:
    index: int
    name: OperationName
    key: str | None = None
    value: Any = None
    has_value: bool = False


@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    isolation: IsolationLevel
    operations: tuple[Operation, ...]


@dataclass(frozen=True)
class EditScript:
    name: str
    sessions: tuple[SessionSpec, ...]
    raw: dict[str, Any]
    initial_sql: str | None = None


@dataclass(frozen=True)
class RowState:
    key: str
    payload: Any
    row_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "payload": self.payload,
            "row_version": self.row_version,
        }


@dataclass(frozen=True)
class SchedulerConfig:
    lock_wait_budget_ms: int = 200
    max_concurrency: int = 2
    tick_ms: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.lock_wait_budget_ms, int) or self.lock_wait_budget_ms < 0:
            raise ValueError("lock_wait_budget_ms must be a non-negative integer")
        if not isinstance(self.max_concurrency, int) or self.max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        if not isinstance(self.tick_ms, int) or self.tick_ms < 1:
            raise ValueError("tick_ms must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "lock_wait_budget_ms": self.lock_wait_budget_ms,
            "max_concurrency": self.max_concurrency,
            "tick_ms": self.tick_ms,
        }


@dataclass
class SessionRuntime:
    spec: SessionSpec
    tx_id: str
    tx_seq: int
    status: str = "pending"
    next_op_index: int = 0
    pending_lock: dict[str, Any] | None = None
    wait_started_ms: int | None = None
    last_wait_ms: int = 0
    pending_writes: dict[str, Any] = field(default_factory=dict)
    committed_writes: dict[str, Any] = field(default_factory=dict)
    read_cache: dict[str, Any] = field(default_factory=dict)
    reads: list[dict[str, Any]] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    lock_waits: list[dict[str, Any]] = field(default_factory=list)
    outcome: str | None = None
    code: str | None = None
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    committed_data_version: int | None = None

    @property
    def session_id(self) -> str:
        return self.spec.session_id
