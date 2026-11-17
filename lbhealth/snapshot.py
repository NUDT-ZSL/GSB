"""Versioned snapshots and whole-state replacement restoration."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .engine import HealthEngine
from .errors import SnapshotError
from .ledger import Ledger
from .models import MemberState, RoundResult, RuntimeState


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Snapshot:
    snapshot_id: int
    created_utc: str
    label: str
    config_version: str
    member_digest: str
    config_digest: str
    round_no: int
    state: RuntimeState
    available_members: tuple[str, ...]
    effective_weights: dict[str, int]
    probe_sequence: list[dict[str, Any]]
    recent_rounds: list[dict[str, Any]]
    replay_baseline: RuntimeState | None
    config_json: dict[str, Any]
    replay_context: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "created_utc": self.created_utc,
            "label": self.label,
            "config_version": self.config_version,
            "member_digest": self.member_digest,
            "round": self.round_no,
            "available_members": list(self.available_members),
            "effective_weights": {
                key: self.effective_weights[key] for key in sorted(self.effective_weights)
            },
            "replay_rounds": [int(item["round"]) for item in self.recent_rounds],
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Snapshot":
        return cls(
            snapshot_id=int(row["snapshot_id"]),
            created_utc=str(row["created_utc"]),
            label=str(row["label"]),
            config_version=str(row["config_version"]),
            member_digest=str(row["member_digest"]),
            config_digest=str(row["config_digest"]),
            round_no=int(row["round_no"]),
            state=RuntimeState.from_dict(json.loads(row["state_json"])),
            available_members=tuple(json.loads(row["available_json"])),
            effective_weights={
                str(key): int(value) for key, value in json.loads(row["weights_json"]).items()
            },
            probe_sequence=json.loads(row["probe_sequence_json"]),
            recent_rounds=json.loads(row["recent_rounds_json"]),
            replay_baseline=(
                RuntimeState.from_dict(json.loads(row["replay_baseline_json"]))
                if row["replay_baseline_json"]
                else None
            ),
            config_json=json.loads(row["config_json"]),
            replay_context=json.loads(row["replay_context_json"]),
        )


@dataclass
class SnapshotRestoreResult:
    status: str
    snapshot_id: int
    mode: str
    config_version_match: bool
    member_digest_match: bool
    config_digest_match: bool
    current_config_version: str
    snapshot_config_version: str
    current_member_digest: str
    snapshot_member_digest: str
    current_config_digest: str
    snapshot_config_digest: str
    state: RuntimeState
    replaced_members: list[dict[str, Any]]
    added_members: list[str]
    removed_members: list[str]
    reset_counters: list[dict[str, Any]]
    note: str = (
        "All previous member states and effective weights were discarded. Restoration "
        "used the snapshot as a complete replacement, never an incremental merge."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "snapshot_id": self.snapshot_id,
            "mode": self.mode,
            "config_version_match": self.config_version_match,
            "member_digest_match": self.member_digest_match,
            "config_digest_match": self.config_digest_match,
            "current_config_version": self.current_config_version,
            "snapshot_config_version": self.snapshot_config_version,
            "current_member_digest": self.current_member_digest,
            "snapshot_member_digest": self.snapshot_member_digest,
            "current_config_digest": self.current_config_digest,
            "snapshot_config_digest": self.snapshot_config_digest,
            "restored_round": self.state.current_round,
            "available_members": list(self.state.available_members),
            "effective_weights": {
                key: self.state.effective_weights[key]
                for key in sorted(self.state.effective_weights)
            },
            "replaced_members": self.replaced_members,
            "added_members": self.added_members,
            "removed_members": self.removed_members,
            "reset_counters": self.reset_counters,
            "note": self.note,
        }


class SnapshotService:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def create(
        self,
        config: Config,
        engine: HealthEngine,
        *,
        label: str,
        window: int = 4,
        replay_context: dict[str, Any] | None = None,
    ) -> Snapshot:
        if window <= 0:
            raise SnapshotError("E_SNAPSHOT", "window must be a positive integer")
        state = engine.snapshot_state()
        if state.current_round <= 0:
            raise SnapshotError("E_SNAPSHOT", "cannot snapshot before the first complete round")
        rows = self.ledger.complete_rounds_upto(state.current_round, limit=window)
        if not rows:
            raise SnapshotError("E_SNAPSHOT", "no complete rounds are available for the snapshot")
        recent_rounds: list[dict[str, Any]] = []
        probe_sequence: list[dict[str, Any]] = []
        for row in rows:
            result = RoundResult.from_dict(json.loads(row["result_json"]))
            deterministic = result.deterministic()
            recent_rounds.append(deterministic)
            probe_sequence.append(
                {"round": result.round_no, "probes": deterministic["probes"]}
            )
        first_round = int(recent_rounds[0]["round"])
        baseline = self.ledger.latest_state_before(first_round)
        snapshot_id = self.ledger.insert_snapshot(
            created_utc=_utc_now_iso(),
            label=label,
            config=config,
            round_no=state.current_round,
            state=state,
            probe_sequence=probe_sequence,
            recent_rounds=recent_rounds,
            replay_baseline=baseline,
            replay_context=replay_context,
        )
        row = self.ledger.get_snapshot_row(snapshot_id)
        if row is None:
            raise SnapshotError("E_SNAPSHOT", "snapshot insert succeeded but could not be read back")
        return Snapshot.from_row(row)

    def get(self, snapshot_id: int) -> Snapshot:
        row = self.ledger.get_snapshot_row(snapshot_id)
        if row is None:
            raise SnapshotError("E_SNAPSHOT_NOT_FOUND", f"snapshot {snapshot_id} does not exist")
        return Snapshot.from_row(row)

    def list(self) -> list[dict[str, Any]]:
        return [Snapshot.from_row(row).summary() for row in self.ledger.list_snapshot_rows()]

    def restore(
        self,
        snapshot_id: int,
        current_config: Config,
        previous_state: RuntimeState | None = None,
    ) -> SnapshotRestoreResult:
        snapshot = self.get(snapshot_id)
        if previous_state is None:
            previous_state = RuntimeState(
                current_round=0,
                member_states={
                    member.id: MemberState(member_id=member.id)
                    for member in current_config.members
                },
                available_members=(),
                effective_weights={member.id: 0 for member in current_config.members},
                full_weight_sum=current_config.pool.full_weight_sum,
            )

        restored_states: dict[str, MemberState] = {}
        replaced_members: list[dict[str, Any]] = []
        added_members: list[str] = []
        reset_counters: list[dict[str, Any]] = []
        for member in current_config.members:
            previous = previous_state.member_states.get(member.id)
            snapshot_state = snapshot.state.member_states.get(member.id)
            if snapshot_state is None:
                restored = MemberState(member_id=member.id)
                added_members.append(member.id)
                reset = {
                    "member": member.id,
                    "success_count_before": previous.success_count if previous else 0,
                    "success_count_after": 0,
                    "failure_count_before": previous.failure_count if previous else 0,
                    "failure_count_after": 0,
                    "reason": "member absent from snapshot; complete replacement reset it to unknown",
                }
                reset_counters.append(reset)
            else:
                restored = MemberState.from_dict(snapshot_state.to_dict())
            restored_states[member.id] = restored
            if previous is not None:
                replaced_members.append(
                    {
                        "member": member.id,
                        "before": previous.to_dict(),
                        "after": restored.to_dict(),
                    }
                )
            else:
                replaced_members.append(
                    {
                        "member": member.id,
                        "before": None,
                        "after": restored.to_dict(),
                    }
                )

        if previous_state.current_round != snapshot.state.current_round:
            for member_id, previous in previous_state.member_states.items():
                if member_id in {member.id for member in current_config.members}:
                    continue
                if previous.success_count or previous.failure_count:
                    reset_counters.append(
                        {
                            "member": member_id,
                            "success_count_before": previous.success_count,
                            "success_count_after": 0,
                            "failure_count_before": previous.failure_count,
                            "failure_count_after": 0,
                            "reason": "member removed by complete snapshot replacement",
                        }
                    )

        current_ids = {member.id for member in current_config.members}
        snapshot_ids = set(snapshot.state.member_states)
        removed_members = sorted(snapshot_ids - current_ids)
        ordered_available = tuple(
            member.id
            for member in current_config.members
            if member.id in set(snapshot.available_members)
            and restored_states[member.id].state.value == "healthy"
        )
        weights = {
            member.id: int(snapshot.effective_weights.get(member.id, 0))
            for member in current_config.members
        }
        state = RuntimeState(
            current_round=snapshot.round_no,
            member_states=restored_states,
            available_members=ordered_available,
            effective_weights=weights,
            full_weight_sum=current_config.pool.full_weight_sum,
        )
        return SnapshotRestoreResult(
            status="ok",
            snapshot_id=snapshot.snapshot_id,
            mode="full_replace",
            config_version_match=snapshot.config_version == current_config.config_version,
            member_digest_match=snapshot.member_digest == current_config.member_digest,
            config_digest_match=snapshot.config_digest == current_config.config_digest,
            current_config_version=current_config.config_version,
            snapshot_config_version=snapshot.config_version,
            current_member_digest=current_config.member_digest,
            snapshot_member_digest=snapshot.member_digest,
            current_config_digest=current_config.config_digest,
            snapshot_config_digest=snapshot.config_digest,
            state=state,
            replaced_members=replaced_members,
            added_members=added_members,
            removed_members=removed_members,
            reset_counters=reset_counters,
        )
