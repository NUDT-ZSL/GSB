from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WalRecord:
    segment_no: int
    logical_tx_id: int
    prev_tx_id: int
    prev_tx_hash: str
    checkpoint_gen: int
    table: str
    row_key: str
    operation: str
    before: Any
    after: Any
    duration_ms: int
    content_hash: str
    tx_hash: str
    raw: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class TableRow:
    table: str
    key: str
    payload: Any
    version: int
    source_tx: int
    tombstone: bool
    row_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "payload": self.payload,
            "row_hash": self.row_hash,
            "source_tx": self.source_tx,
            "tombstone": self.tombstone,
            "version": self.version,
        }


@dataclass(frozen=True)
class V1Conversion:
    base_logical_ms: int
    relative_ms: int
    scale: str
    rounding: str
    absolute_ms: int


@dataclass(frozen=True)
class Checkpoint:
    format_version: str
    checkpoint_gen: int
    applied_watermark: int
    chain_hash: str
    logical_clock_ms: int
    manifest_version: int
    replay_generation: int
    commit_ts_ms: int
    table_summaries: dict[str, dict[str, Any]]
    rows_by_table: dict[str, list[TableRow]]
    path: Path
    manifest_hash: str
    parent_checkpoint_gen: int | None = None
    min_replay_generation: int = 0
    v1_conversion: V1Conversion | None = None
    dirty_pages: dict[str, Any] = field(default_factory=dict)
    update_kind: str = "full"
    raw_manifest: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_v2(self) -> bool:
        return self.format_version == "v2"


@dataclass(frozen=True)
class Policy:
    max_concurrent_txs: int = 3
    round_budget_ms: int = 24
    checkpoint_interval_ms: int = 30
    clock_step_ms: int = 8
    clock_tolerance_ms: int = 4
    inject_failure_tx_ids: tuple[int, ...] = ()
    fail_round_on_error: bool = True

    def overridden(self, **changes: Any) -> "Policy":
        values = {
            "max_concurrent_txs": self.max_concurrent_txs,
            "round_budget_ms": self.round_budget_ms,
            "checkpoint_interval_ms": self.checkpoint_interval_ms,
            "clock_step_ms": self.clock_step_ms,
            "clock_tolerance_ms": self.clock_tolerance_ms,
            "inject_failure_tx_ids": self.inject_failure_tx_ids,
            "fail_round_on_error": self.fail_round_on_error,
        }
        values.update({key: value for key, value in changes.items() if value is not None})
        if isinstance(values["inject_failure_tx_ids"], list):
            values["inject_failure_tx_ids"] = tuple(values["inject_failure_tx_ids"])
        return Policy(**values)


@dataclass(frozen=True)
class CheckpointSet:
    by_gen: dict[int, Checkpoint]
    current: Checkpoint

    @property
    def generations(self) -> set[int]:
        return set(self.by_gen)

    @property
    def max_generation(self) -> int:
        return max(self.by_gen, default=0)

    def get(self, generation: int) -> Checkpoint:
        try:
            return self.by_gen[generation]
        except KeyError as exc:
            raise KeyError(f"checkpoint generation {generation} is absent") from exc


@dataclass(frozen=True)
class Sample:
    root: Path
    wal: list[WalRecord]
    checkpoints: CheckpointSet
    policy: Policy

    @property
    def tx_by_id(self) -> dict[int, WalRecord]:
        return {record.logical_tx_id: record for record in self.wal}

    @property
    def max_tx_id(self) -> int:
        return max((record.logical_tx_id for record in self.wal), default=0)
