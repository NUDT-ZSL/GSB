from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import PolicyTable, TrainingConfig
from .errors import SnapshotVersionMismatch
from .io import atomic_write_json, atomic_write_jsonl, read_json, read_jsonl


SNAPSHOT_SCHEMA_VERSION = 1


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = root / "snapshot.json"
        self.run_state_path = root / "run_state.json"
        self.journal_path = root / "round_journal.jsonl"
        self.progress_path = root / "progress.json"

    def load_snapshot(
        self, config: TrainingConfig, policy: PolicyTable
    ) -> dict[str, Any] | None:
        if not self.snapshot_path.exists():
            return None
        try:
            snapshot = read_json(self.snapshot_path)
        except Exception as exc:
            raise SnapshotVersionMismatch(f"cannot read snapshot: {exc}") from exc
        if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotVersionMismatch(
                "snapshot version mismatch",
                details={
                    "snapshot_schema_version": snapshot.get("schema_version"),
                    "expected_schema_version": SNAPSHOT_SCHEMA_VERSION,
                },
            )
        required = {
            "snapshot_version",
            "phase",
            "config_hash",
            "policy_hash",
            "round_cursor",
            "active_round",
            "manifest_digest",
            "params_digest",
        }
        missing = sorted(required - set(snapshot))
        if missing:
            raise SnapshotVersionMismatch(f"snapshot is missing required fields: {missing}")
        if not isinstance(snapshot.get("snapshot_version"), int) or snapshot["snapshot_version"] < 1:
            raise SnapshotVersionMismatch("snapshot version number is invalid")
        if snapshot.get("config_hash") != config.config_hash:
            raise SnapshotVersionMismatch(
                "snapshot config hash mismatch",
                details={
                    "snapshot_config_hash": snapshot.get("config_hash"),
                    "active_config_hash": config.config_hash,
                },
            )
        if snapshot.get("policy_hash") != policy.policy_hash:
            raise SnapshotVersionMismatch(
                "snapshot policy hash mismatch",
                details={
                    "snapshot_policy_hash": snapshot.get("policy_hash"),
                    "active_policy_hash": policy.policy_hash,
                },
            )
        return snapshot

    def write_snapshot(
        self,
        *,
        config: TrainingConfig,
        policy: PolicyTable,
        phase: str,
        round_cursor: int,
        active_round: int,
        manifest_digest: str,
        params_digest: str,
        checkpoint_generation: int,
        pending_retention: bool = False,
        round_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous_version = 0
        if self.snapshot_path.exists():
            try:
                previous_version = int(read_json(self.snapshot_path).get("snapshot_version", 0))
            except Exception:
                previous_version = 0
        snapshot = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_version": previous_version + 1,
            "phase": phase,
            "config_hash": config.config_hash,
            "policy_hash": policy.policy_hash,
            "round_cursor": int(round_cursor),
            "active_round": int(active_round),
            "manifest_digest": manifest_digest,
            "params_digest": params_digest,
            "checkpoint_generation": int(checkpoint_generation),
            "pending_retention": bool(pending_retention),
        }
        if round_record is not None:
            snapshot["round_record"] = round_record
        atomic_write_json(self.snapshot_path, snapshot)
        return snapshot

    def read_run_state(self) -> dict[str, Any] | None:
        if not self.run_state_path.exists():
            return None
        try:
            return read_json(self.run_state_path)
        except Exception:
            return None

    def write_run_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.run_state_path, state)

    def read_journal(self) -> list[dict[str, Any]]:
        return read_jsonl(self.journal_path)

    def write_journal(self, records: list[dict[str, Any]]) -> None:
        ordered = sorted(records, key=lambda record: int(record["round"]))
        atomic_write_jsonl(self.journal_path, ordered)

    def upsert_journal_record(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        records = [item for item in self.read_journal() if int(item["round"]) != int(record["round"])]
        records.append(record)
        self.write_journal(records)
        return sorted(records, key=lambda item: int(item["round"]))

    def write_progress(self, progress: dict[str, Any]) -> None:
        atomic_write_json(self.progress_path, progress)

    def read_progress(self) -> dict[str, Any] | None:
        if not self.progress_path.exists():
            return None
        try:
            return read_json(self.progress_path)
        except Exception:
            return None
