"""Persistent batch-boundary checkpoints for crash recovery."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes
from .errors import E_CHECKPOINT, WorkbenchError


class ForcedTermination(Exception):
    """Raised by deterministic tests/hooks to emulate SIGKILL after commit."""


class BatchCheckpointStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkbenchError(E_CHECKPOINT, "Checkpoint is corrupt", field=str(self.path)) from exc
        if not isinstance(value, dict) or value.get("schema") != "dedup-workbench/checkpoint/v1":
            raise WorkbenchError(E_CHECKPOINT, "Checkpoint schema is invalid", field=str(self.path))
        return value

    def reset(self, *, run_fingerprint: str, total_batches: int, batch_size: int) -> dict[str, Any]:
        checkpoint = {
            "schema": "dedup-workbench/checkpoint/v1",
            "run_fingerprint": run_fingerprint,
            "batch_size": batch_size,
            "total_batches": total_batches,
            "next_batch_index": 0,
            "committed_batch_indexes": [],
            "batch_hashes": {},
            "status": "running",
        }
        self.write(checkpoint)
        return checkpoint

    def write(self, checkpoint: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".checkpoint.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes(checkpoint))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def ensure_for_run(self, *, run_fingerprint: str, total_batches: int, batch_size: int) -> dict[str, Any]:
        checkpoint = self.load()
        if checkpoint is None:
            return self.reset(run_fingerprint=run_fingerprint, total_batches=total_batches, batch_size=batch_size)
        if checkpoint.get("run_fingerprint") != run_fingerprint:
            return self.reset(run_fingerprint=run_fingerprint, total_batches=total_batches, batch_size=batch_size)
        if checkpoint.get("total_batches") != total_batches or checkpoint.get("batch_size") != batch_size:
            raise WorkbenchError(E_CHECKPOINT, "Checkpoint batch geometry does not match current run", field="batch_size")
        return checkpoint

    def commit_batch(self, checkpoint: dict[str, Any], batch_index: int, batch_hash: str) -> dict[str, Any]:
        committed = list(checkpoint.get("committed_batch_indexes", []))
        if batch_index not in committed:
            committed.append(batch_index)
        committed.sort()
        checkpoint["committed_batch_indexes"] = committed
        checkpoint["next_batch_index"] = max(committed) + 1 if committed else 0
        checkpoint.setdefault("batch_hashes", {})[str(batch_index)] = batch_hash
        checkpoint["status"] = "running"
        self.write(checkpoint)
        return checkpoint

    def complete(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        checkpoint["status"] = "completed"
        self.write(checkpoint)
        return checkpoint
