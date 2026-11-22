from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Transaction:
    txn_id: int
    commit_ms: int
    byte_size: int
    segment_id: int


@dataclass(frozen=True)
class ReplayEvent:
    sequence: int
    txn_id: int
    instance_id: str
    replay_ms: int
    result: str
    tombstone: bool


@dataclass(frozen=True)
class Policy:
    policy_id: str
    source_version: str
    instance_id: str
    segment_id: int | None
    retention_generation: int
    effective_at_ms: int
    relative_seconds: int | None
    absolute_cutoff_ms: int | None
    required_min_catchup_margin: int
    parent_policy_id: str | None
    tombstone: bool
    retention_window: str | None
    source_ref: str
    source_priority: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source_version}:{self.policy_id}"

    @property
    def signature(self) -> tuple[str, int | None, int, int]:
        return (
            self.instance_id,
            self.segment_id,
            self.retention_generation,
            self.effective_at_ms,
        )


@dataclass(frozen=True)
class InstanceReplayState:
    instance_id: str
    latest_txn_id: int
    latest_replay_ms: int
    result: str
    tombstone: bool
    event_count: int


@dataclass
class NormalizedInput:
    transactions: list[Transaction]
    replay_events: list[ReplayEvent]
    replay_states: dict[str, InstanceReplayState]
    policies: list[Policy]
    segment_ids: list[int]
    fingerprint: str

    def lag_bytes_by_instance(self) -> dict[str, int]:
        by_txn = {txn.txn_id: txn for txn in self.transactions}
        max_txn = max(by_txn)
        result: dict[str, int] = {}
        for instance_id, state in self.replay_states.items():
            result[instance_id] = sum(
                txn.byte_size
                for txn_id, txn in by_txn.items()
                if state.latest_txn_id < txn_id <= max_txn
            )
        return result
