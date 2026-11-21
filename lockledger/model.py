"""Parsed, immutable sample model."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    primary_key: str
    rows: tuple[dict[str, Any], ...]

    def column(self, name: str) -> Column | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)


@dataclass(frozen=True, slots=True)
class DatabaseSpec:
    tables: tuple[Table, ...]

    def table(self, name: str) -> Table | None:
        lowered = name.lower()
        return next((t for t in self.tables if t.name.lower() == lowered), None)


@dataclass(frozen=True, slots=True)
class Config:
    clock_step_ms: int = 1
    scale: Fraction = Fraction(1, 1)
    tolerance: Fraction = Fraction(0, 1)
    error_budget: Fraction = Fraction(1, 1)
    max_concurrency: int = 4
    deadlock_check_period_ms: int = 1
    max_rounds: int = 10000
    logical_horizon_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Operation:
    index: int
    op_id: str
    kind: str
    table: str
    mode: str
    row_value: Any = None
    range_lo: Any = None
    range_hi: Any = None
    columns: tuple[str, ...] = ()
    values: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Transaction:
    tx_key: str
    tx_no: int
    isolation: str
    wait_budget_ms: int
    max_retries: int
    operations: tuple[Operation, ...]
    rollback_after_ops: int | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    database: DatabaseSpec
    transactions: tuple[Transaction, ...]
    config: Config
    source_path: str
    source_sha256: str

    def transaction(self, tx_key: str) -> Transaction:
        return next(tx for tx in self.transactions if tx.tx_key == tx_key)

