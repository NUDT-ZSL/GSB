from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from . import MERGE_POLICY_VERSION, STRUCTURE_VERSION
from .codes import (
    E_CHECKPOINT_CORRUPT,
    E_LOCK,
    E_VERSION_REBUILD_REQUIRED,
    OK,
)
from .io_utils import (
    atomic_write_json,
    cleanup_temp_files,
    json_dumps,
    read_json_file,
    sha256_bytes,
    sha256_file,
)
from .locking import FileLock, LockConflict
from .merge import MergedView
from .parser import InputBatch


JsonDict = dict[str, Any]


class CheckpointLockError(RuntimeError):
    code = E_LOCK


class CheckpointManager:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        structure_version: int = STRUCTURE_VERSION,
        policy_version: str = MERGE_POLICY_VERSION,
        clock_ms: int = 0,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = self.state_dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "checkpoints.sqlite"
        self.lock_path = self.state_dir / "checkpoint.lock"
        self.structure_version = structure_version
        self.policy_version = policy_version
        self.clock_ms = clock_ms

    def file_lock(self) -> FileLock:
        return FileLock(self.lock_path)

    def recover(self) -> JsonDict:
        removed_temp = cleanup_temp_files(self.state_dir)
        removed_temp.extend(cleanup_temp_files(self.snapshot_dir))

        if not self.db_path.exists():
            return {
                "status": "missing",
                "code": OK,
                "watermark": 0,
                "checkpoint": None,
                "removed_temp_files": removed_temp,
            }

        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    return self._corrupt("SQLite integrity_check failed", removed_temp)
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "checkpoint" not in tables:
                    return {
                        "status": "missing",
                        "code": OK,
                        "watermark": 0,
                        "checkpoint": None,
                        "removed_temp_files": removed_temp,
                    }
                row = conn.execute(
                    """
                    SELECT watermark, structure_version, policy_version,
                           snapshot_rel, snapshot_sha256, state_json
                    FROM checkpoint WHERE id = 1
                    """
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            return self._corrupt(f"cannot read checkpoint database: {exc}", removed_temp)

        if row is None:
            return {
                "status": "missing",
                "code": OK,
                "watermark": 0,
                "checkpoint": None,
                "removed_temp_files": removed_temp,
            }

        watermark, structure_version, policy_version, snapshot_rel, snapshot_sha, state_json = row
        version_mismatch = (
            int(structure_version) != self.structure_version
            or str(policy_version) != self.policy_version
        )
        if version_mismatch:
            return {
                "status": "rebuild_required",
                "code": E_VERSION_REBUILD_REQUIRED,
                "watermark": int(watermark),
                "old_structure_version": int(structure_version),
                "old_policy_version": str(policy_version),
                "current_structure_version": self.structure_version,
                "current_policy_version": self.policy_version,
                "removed_temp_files": removed_temp,
            }

        try:
            view_data = json.loads(state_json)
        except (TypeError, json.JSONDecodeError) as exc:
            return self._corrupt(f"checkpoint state_json is not parseable: {exc}", removed_temp)

        removed_orphans = self._clean_orphan_snapshots(int(watermark))
        snapshot_path = self.state_dir / str(snapshot_rel)
        snapshot_ok = snapshot_path.exists()
        actual_sha = sha256_file(snapshot_path) if snapshot_ok else None
        snapshot_ok = bool(snapshot_ok and actual_sha == snapshot_sha)
        snapshot_data: Optional[JsonDict] = None
        repaired_snapshot = False
        if snapshot_ok:
            try:
                snapshot_data = read_json_file(snapshot_path)
            except (OSError, ValueError):
                snapshot_ok = False
        if not snapshot_ok:
            snapshot_payload = {
                "checkpoint_type": "auth_interop_merged_view",
                "structure_version": int(structure_version),
                "policy_version": str(policy_version),
                "watermark": int(watermark),
                "view": view_data,
            }
            try:
                atomic_write_json(snapshot_path, snapshot_payload)
                repaired_snapshot = True
                snapshot_ok = sha256_file(snapshot_path) == snapshot_sha
                snapshot_data = snapshot_payload
            except OSError:
                snapshot_ok = False

        checkpoint = {
            "watermark": int(watermark),
            "structure_version": int(structure_version),
            "policy_version": str(policy_version),
            "snapshot_rel": str(snapshot_rel).replace("\\", "/"),
            "snapshot_sha256": snapshot_sha,
            "snapshot_ok": snapshot_ok,
            "repaired_snapshot": repaired_snapshot,
            "view": view_data,
        }
        status = "ok" if snapshot_ok else "snapshot_repair_required"
        return {
            "status": status,
            "code": OK if snapshot_ok else E_CHECKPOINT_CORRUPT,
            "watermark": int(watermark),
            "checkpoint": checkpoint,
            "snapshot_data": snapshot_data,
            "removed_temp_files": removed_temp,
            "removed_orphan_snapshots": removed_orphans,
        }

    def _clean_orphan_snapshots(self, committed_watermark: int) -> list[str]:
        removed: list[str] = []
        for path in self.snapshot_dir.glob("checkpoint-*.json"):
            stem = path.stem
            digits = stem.rsplit("-", 1)[-1]
            if not digits.isdigit():
                continue
            watermark = int(digits)
            if watermark > committed_watermark:
                try:
                    path.unlink()
                    removed.append(str(path).replace("\\", "/"))
                except OSError:
                    pass
        return removed

    def _corrupt(self, message: str, removed_temp: list[str]) -> JsonDict:
        return {
            "status": "corrupt",
            "code": E_CHECKPOINT_CORRUPT,
            "message": message,
            "watermark": 0,
            "checkpoint": None,
            "removed_temp_files": removed_temp,
        }

    def commit(
        self,
        view: MergedView,
        watermark: int,
        batch: Optional[InputBatch] = None,
    ) -> JsonDict:
        try:
            with self.file_lock():
                return self._commit_locked(view, watermark, batch)
        except LockConflict as exc:
            raise CheckpointLockError(str(exc)) from exc

    def rebuild(
        self,
        view: MergedView,
        watermark: int,
        batches: Optional[list[InputBatch]] = None,
    ) -> JsonDict:
        try:
            with self.file_lock():
                if self.db_path.exists():
                    self.db_path.unlink()
                for snapshot in self.snapshot_dir.glob("checkpoint-*.json"):
                    snapshot.unlink(missing_ok=True)
                result = self._commit_locked(view, watermark, None)
                if batches:
                    with sqlite3.connect(str(self.db_path)) as conn:
                        for item in batches:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO applied_batches
                                (sequence, batch_id, record_count)
                                VALUES (?, ?, ?)
                                """,
                                (item.sequence, item.batch_id, len(item.records)),
                            )
                        conn.commit()
                result["rebuilt"] = True
                return result
        except LockConflict as exc:
            raise CheckpointLockError(str(exc)) from exc

    def _commit_locked(
        self,
        view: MergedView,
        watermark: int,
        batch: Optional[InputBatch],
    ) -> JsonDict:
        snapshot_rel = f"snapshots/checkpoint-{int(watermark):06d}.json"
        snapshot_path = self.state_dir / snapshot_rel
        view_data = view.to_dict()
        snapshot_payload = {
            "checkpoint_type": "auth_interop_merged_view",
            "structure_version": self.structure_version,
            "policy_version": self.policy_version,
            "watermark": int(watermark),
            "view": view_data,
        }
        encoded_snapshot = json_dumps(snapshot_payload).encode("utf-8")
        snapshot_sha = sha256_bytes(encoded_snapshot)
        state_json = json.dumps(view_data, ensure_ascii=False, sort_keys=True)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoint (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    watermark INTEGER NOT NULL,
                    structure_version INTEGER NOT NULL,
                    policy_version TEXT NOT NULL,
                    snapshot_rel TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoint_history (
                    watermark INTEGER PRIMARY KEY,
                    structure_version INTEGER NOT NULL,
                    policy_version TEXT NOT NULL,
                    snapshot_rel TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    committed_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applied_batches (
                    sequence INTEGER PRIMARY KEY,
                    batch_id TEXT NOT NULL UNIQUE,
                    record_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoint
                (id, watermark, structure_version, policy_version, snapshot_rel,
                 snapshot_sha256, state_json, record_count, updated_at_ms)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(watermark),
                    self.structure_version,
                    self.policy_version,
                    snapshot_rel,
                    snapshot_sha,
                    state_json,
                    int(view.stats()["entity_total"]),
                    int(self.clock_ms or 0),
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoint_history
                (watermark, structure_version, policy_version, snapshot_rel,
                 snapshot_sha256, committed_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(watermark),
                    self.structure_version,
                    self.policy_version,
                    snapshot_rel,
                    snapshot_sha,
                    int(self.clock_ms or 0),
                ),
            )
            if batch is not None:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO applied_batches
                    (sequence, batch_id, record_count)
                    VALUES (?, ?, ?)
                    """,
                    (batch.sequence, batch.batch_id, len(batch.records)),
                )
            conn.commit()

        # Publish the snapshot only after SQLite commit is durable.  If killed
        # here, recovery reconstructs the snapshot from committed state_json.
        atomic_write_json(snapshot_path, snapshot_payload)

        return {
            "status": "committed",
            "watermark": int(watermark),
            "snapshot_rel": snapshot_rel,
            "snapshot_sha256": snapshot_sha,
            "structure_version": self.structure_version,
            "policy_version": self.policy_version,
        }

    def verify(self) -> JsonDict:
        recovery = self.recover()
        if recovery["status"] == "ok":
            checkpoint = recovery["checkpoint"]
            assert checkpoint is not None
            return {
                "code": OK,
                "status": "ok",
                "state_dir": str(self.state_dir).replace("\\", "/"),
                "db_path": str(self.db_path).replace("\\", "/"),
                "watermark": checkpoint["watermark"],
                "structure_version": checkpoint["structure_version"],
                "policy_version": checkpoint["policy_version"],
                "snapshot_rel": checkpoint["snapshot_rel"],
                "snapshot_ok": checkpoint["snapshot_ok"],
                "sqlite_integrity": "ok",
            }
        return {
            "code": recovery["code"],
            "status": recovery["status"],
            "state_dir": str(self.state_dir).replace("\\", "/"),
            "recovery": recovery,
        }
