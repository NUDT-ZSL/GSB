from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, TextIO

from .cache import ContentAddressedCache
from .config import ConfigBundle
from .errors import (
    E_CHECKPOINT_CORRUPT,
    E_STAGE_BUSY,
    WorkbenchError,
)
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    content_hash,
    read_json_file,
    sentence_sort_key,
)


_PROCESS_GUARDS: dict[str, threading.Lock] = {}
_PROCESS_GUARDS_LOCK = threading.Lock()


class CommitGuard:
    def __init__(self, state_dir: str | Path, owner: str):
        self.state_dir = Path(state_dir)
        self.owner = owner
        self.lock_path = self.state_dir / "commit.lock"
        self._descriptor: TextIO | None = None
        self._local_lock: threading.Lock | None = None

    def __enter__(self) -> "CommitGuard":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        key = str(self.lock_path.resolve())
        with _PROCESS_GUARDS_LOCK:
            lock = _PROCESS_GUARDS.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise WorkbenchError(
                E_STAGE_BUSY,
                "another commit is already active",
                {"owner": self.owner, "lock": str(self.lock_path)},
            )
        self._local_lock = lock
        descriptor = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(
                descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError as exc:
            descriptor.close()
            lock.release()
            self._local_lock = None
            raise WorkbenchError(
                E_STAGE_BUSY,
                "another process is committing a checkpoint or snapshot",
                {"owner": self.owner, "lock": str(self.lock_path)},
            ) from exc
        self._descriptor = descriptor
        descriptor.seek(0)
        descriptor.truncate()
        descriptor.write(self.owner)
        descriptor.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._descriptor is not None:
            try:
                holder = self.lock_path.open("r+", encoding="utf-8")
                try:
                    holder.seek(0)
                    holder.truncate()
                    holder.flush()
                finally:
                    holder.close()
                fcntl.flock(self._descriptor.fileno(), fcntl.LOCK_UN)
            finally:
                try:
                    self._descriptor.close()
                except OSError:
                    pass
                self._descriptor = None
        if self._local_lock is not None:
            self._local_lock.release()
            self._local_lock = None


class Ledger:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "ledger.sqlite3"
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    logical_clock_ms INTEGER NOT NULL,
                    checkpoint_version INTEGER,
                    payload_json TEXT NOT NULL,
                    checksum TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=0.2)
        connection.row_factory = sqlite3.Row
        return connection

    def append(
        self,
        kind: str,
        logical_clock_ms: int,
        payload: dict[str, Any],
        *,
        checkpoint_version: int | None = None,
    ) -> dict[str, Any]:
        payload_text = canonical_json_bytes(payload).decode("utf-8")
        checksum = content_hash(
            {
                "kind": kind,
                "logical_clock_ms": logical_clock_ms,
                "checkpoint_version": checkpoint_version,
                "payload": payload,
            }
        )
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                INSERT INTO events(
                    kind, logical_clock_ms, checkpoint_version,
                    payload_json, checksum
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    logical_clock_ms,
                    checkpoint_version,
                    payload_text,
                    checksum,
                ),
            )
            connection.commit()
            seq = int(cursor.lastrowid)
        finally:
            connection.close()
        return {
            "seq": seq,
            "kind": kind,
            "logical_clock_ms": logical_clock_ms,
            "checkpoint_version": checkpoint_version,
            "payload": payload,
            "checksum": checksum,
        }

    def events(self, limit: int | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            sql = "SELECT * FROM events ORDER BY seq"
            if limit is not None:
                sql += " DESC LIMIT ?"
            rows = connection.execute(
                sql, () if limit is None else (int(limit),)
            ).fetchall()
        finally:
            connection.close()
        events = [
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "logical_clock_ms": row["logical_clock_ms"],
                "checkpoint_version": row["checkpoint_version"],
                "payload": json.loads(row["payload_json"]),
                "checksum": row["checksum"],
            }
            for row in rows
        ]
        if limit is not None:
            events.reverse()
        return events


class StageMachine:
    STAGES = ("normalize", "candidates", "disambiguation", "tagging")

    def __init__(self) -> None:
        self.index = 0

    def enter(self, stage: str) -> None:
        from .errors import E_STATE_TRANSITION

        if self.index >= len(self.STAGES) or self.STAGES[self.index] != stage:
            expected = (
                self.STAGES[self.index]
                if self.index < len(self.STAGES)
                else "<complete>"
            )
            raise WorkbenchError(
                E_STATE_TRANSITION,
                "invalid stage transition",
                {"stage": stage, "expected": expected},
            )

    def complete(self) -> None:
        self.index += 1

    @property
    def is_complete(self) -> bool:
        return self.index == len(self.STAGES)


class StateStore:
    def __init__(self, state_dir: str | Path, cache: ContentAddressedCache):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = cache
        self.checkpoints_dir = self.root / "checkpoints"
        self.pending_dir = self.root / "pending"
        self.snapshots_dir = self.root / "config_snapshots"
        self.manifest_path = self.root / "manifest.json"
        self.last_run_path = self.root / "last_run.json"
        self.version_path = self.root / "checkpoint_version.json"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.root)

    def _checkpoint_path(self, stage: str) -> Path:
        return self.checkpoints_dir / f"{stage}.json"

    def _pending_path(self, stage: str) -> Path:
        return self.pending_dir / f"{stage}.json"

    def discard_pending(self, *, ledger_clock_ms: int) -> list[str]:
        discarded: list[str] = []
        dropped_keys = 0
        if self.pending_dir.exists():
            for path in sorted(self.pending_dir.glob("*.json")):
                discarded.append(path.name)
                try:
                    pending = read_json_file(path)
                except WorkbenchError:
                    pending = {}
                if isinstance(pending, dict):
                    for key_hash in pending.get("key_hashes", []):
                        if isinstance(key_hash, str) and self.cache.drop_key_hash(
                            key_hash
                        ):
                            dropped_keys += 1
                path.unlink()
        if discarded:
            self.ledger.append(
                "pending_discarded",
                ledger_clock_ms,
                {"files": discarded, "dropped_key_entries": dropped_keys},
            )
        return discarded

    def write_pending_batch(
        self,
        stage: str,
        *,
        batch_index: int,
        sentence_ids: list[str],
        payload_hash: str,
        key_hashes: list[str],
        logical_clock_ms: int,
    ) -> None:
        atomic_write_json(
            self._pending_path(stage),
            {
                "stage": stage,
                "batch_index": batch_index,
                "sentence_ids": sorted(sentence_ids, key=sentence_sort_key),
                "payload_hash": payload_hash,
                "key_hashes": key_hashes,
                "logical_clock_ms": logical_clock_ms,
                "committed": False,
            },
        )

    def load_checkpoint(
        self,
        stage: str,
        *,
        task_digest: str,
        config_fingerprint: str,
    ) -> dict[str, Any] | None:
        path = self._checkpoint_path(stage)
        if not path.exists():
            return None
        try:
            checkpoint = read_json_file(path)
        except Exception as exc:
            if isinstance(exc, WorkbenchError):
                raise
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint cannot be read",
                {"stage": stage, "path": str(path), "error": str(exc)},
            ) from exc
        if not isinstance(checkpoint, dict):
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint root is not an object",
                {"stage": stage, "path": str(path)},
            )
        if checkpoint.get("stage") != stage:
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint stage mismatch",
                {"stage": stage, "found": checkpoint.get("stage")},
            )
        if checkpoint.get("task_digest") != task_digest:
            return None
        if checkpoint.get("config_fingerprint") != config_fingerprint:
            return None
        payload_hash = checkpoint.get("payload_hash")
        checksum = checkpoint.get("content_checksum")
        if not isinstance(payload_hash, str) or checksum != payload_hash:
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint checksum metadata is invalid",
                {"stage": stage, "path": str(path)},
            )
        payload = self.cache.get_object(payload_hash)
        completed = checkpoint.get("completed_sentence_ids")
        if not isinstance(completed, list) or len(completed) != len(set(completed)):
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint completed sentence set is invalid",
                {"stage": stage, "path": str(path)},
            )
        return checkpoint

    def _next_checkpoint_version_locked(self) -> int:
        current = 0
        if self.version_path.exists():
            value = read_json_file(self.version_path)
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("version"), int)
                or value["version"] < 0
            ):
                raise WorkbenchError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint version counter is corrupt",
                    {"path": str(self.version_path)},
                )
            current = value["version"]
        next_version = current + 1
        atomic_write_json(self.version_path, {"version": next_version})
        return next_version

    def commit_checkpoint(
        self,
        stage: str,
        *,
        completed_sentence_ids: list[str],
        expected_sentence_ids: set[str],
        payload: dict[str, Any],
        task_digest: str,
        config_fingerprint: str,
        logical_clock_ms: int,
    ) -> dict[str, Any]:
        completed = set(completed_sentence_ids)
        if completed != expected_sentence_ids:
            missing = sorted(expected_sentence_ids - completed, key=sentence_sort_key)
            extra = sorted(completed - expected_sentence_ids, key=sentence_sort_key)
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "stage checkpoint cannot commit before every sentence succeeds",
                {"stage": stage, "missing": missing, "extra": extra},
            )
        payload_hash = self.cache.put_object(payload)
        with CommitGuard(self.root, f"checkpoint:{stage}"):
            version = self._next_checkpoint_version_locked()
            checkpoint = {
                "schema": "stage-checkpoint-v1",
                "stage": stage,
                "version": version,
                "completed_sentence_ids": sorted(
                    completed_sentence_ids, key=sentence_sort_key
                ),
                "content_checksum": payload_hash,
                "payload_hash": payload_hash,
                "task_digest": task_digest,
                "config_fingerprint": config_fingerprint,
                "logical_clock_ms": logical_clock_ms,
                "atomic": True,
            }
            atomic_write_json(self._checkpoint_path(stage), checkpoint)
            self.ledger.append(
                "stage_commit",
                logical_clock_ms,
                {
                    "stage": stage,
                    "completed_count": len(completed),
                    "content_checksum": payload_hash,
                },
                checkpoint_version=version,
            )
            pending = self._pending_path(stage)
            if pending.exists():
                pending.unlink()
        return checkpoint

    def read_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        value = read_json_file(self.manifest_path)
        if not isinstance(value, dict):
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "manifest root is not an object",
                {"path": str(self.manifest_path)},
            )
        return value

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        atomic_write_json(self.manifest_path, manifest)

    def write_last_run(self, report: dict[str, Any]) -> None:
        atomic_write_json(self.last_run_path, report)

    def commit_config_snapshot(
        self,
        bundle: ConfigBundle,
        *,
        logical_clock_ms: int,
    ) -> dict[str, Any]:
        with CommitGuard(self.root, "config-snapshot"):
            version = self._next_checkpoint_version_locked()
            effective_hash = bundle.effective_hash
            snapshot_id = f"{version:08d}-{effective_hash[:16]}"
            snapshot = {
                "schema": "config-snapshot-v1",
                "version": version,
                "snapshot_id": snapshot_id,
                "generation": bundle.data["generation"],
                "effective_hash": effective_hash,
                "logical_clock_ms": logical_clock_ms,
                "config": bundle.data,
            }
            snapshot_path = self.snapshots_dir / f"{snapshot_id}.json"
            atomic_write_json(snapshot_path, snapshot)
            atomic_write_json(
                self.snapshots_dir / "current.json",
                {
                    "snapshot_id": snapshot_id,
                    "path": snapshot_path.name,
                    "version": version,
                },
            )
            self.ledger.append(
                "config_snapshot",
                logical_clock_ms,
                {
                    "snapshot_id": snapshot_id,
                    "effective_hash": effective_hash,
                    "generation": bundle.data["generation"],
                },
                checkpoint_version=version,
            )
        return snapshot

