"""Validated in-memory model for one diagnostic sample."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Operation:
    tx_id: str
    op_seq: int
    op_version: int
    op_id: str
    kind: str
    data: dict[str, Any]
    resource_key: str | None
    resource_type: str | None
    table: str
    row_id: int | None
    range_start: int | None
    range_end: int | None
    mode: str | None


@dataclass(frozen=True)
class Transaction:
    tx_id: str
    start_order: int
    version: int
    isolation: str
    budget_ms: int
    depends_on: tuple[str, ...]
    operations: tuple[Operation, ...]


@dataclass(frozen=True)
class ClockSpec:
    raw_units_per_ms: int
    step_units: int
    samples: tuple[int, ...]


@dataclass(frozen=True)
class Config:
    max_concurrent_transactions: int
    worker_processes: int
    default_lock_wait_budget_ms: int
    deadlock_check_period: int
    fixed_scale: int
    wait_tolerance_ms: Decimal
    max_scheduler_steps: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class SampleBundle:
    sample_dir: Path
    db_path: Path
    schema_path: Path
    transactions_path: Path
    config_path: Path
    clock_path: Path
    manifest: dict[str, Any]
    schema: dict[str, Any]
    transactions: tuple[Transaction, ...]
    config: Config
    clock: ClockSpec
    tables: dict[str, tuple[str, ...]]
    indexes: dict[str, dict[str, Any]]
    fingerprint: str
    manifest_json: dict[str, Any] = field(default_factory=dict)
