"""One shared stage lock for configuration snapshots and checkpoint commits."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .checkpoint import CheckpointCommit, CheckpointStore
from .ledger import CommitLedger, LedgerCommit, StageLock


class CommitController:
    """A small facade proving both commit streams contend on the same stage lock."""

    def __init__(self, root: str | Path):
        base = Path(root)
        base.mkdir(parents=True, exist_ok=True)
        self.lock = StageLock(base / "stage.lock")
        self.ledger = CommitLedger(base / "ledger", lock=self.lock)
        self.checkpoints = CheckpointStore(base / "checkpoints", lock=self.lock)

    def commit_config_snapshot(
        self,
        *,
        snapshot: Mapping[str, Any],
        version: int,
        expected_parent_version: int | None = None,
        expected_parent_checksum: str | None = None,
    ) -> LedgerCommit:
        return self.ledger.commit(
            kind="config",
            snapshot=snapshot,
            version=version,
            expected_parent_version=expected_parent_version,
            expected_parent_checksum=expected_parent_checksum,
        )

    def commit_checkpoint_batch(
        self,
        *,
        schedule_key: str,
        completed_request_ids: Sequence[str],
        decision_records: Mapping[str, Mapping[str, Any]],
        batch_index: int,
        expected_parent_version: int | None = None,
    ) -> CheckpointCommit:
        return self.checkpoints.commit_batch(
            schedule_key=schedule_key,
            completed_request_ids=completed_request_ids,
            decision_records=decision_records,
            batch_index=batch_index,
            expected_parent_version=expected_parent_version,
        )

