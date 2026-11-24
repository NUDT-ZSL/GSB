"""Durable, versioned checkpoint storage with atomic snapshots and locking."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .constants import CHECKPOINT_SCHEMA_VERSION

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class CheckpointError(RuntimeError):
    code = "E_CHECKPOINT"


class CheckpointLocked(CheckpointError):
    code = "E_LOCK"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sqlite_readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


@dataclass(frozen=True)
class CheckpointLoadResult:
    payload: dict[str, Any] | None
    generation: int | None
    checkpoint_id: str | None
    recovered: bool
    residue: tuple[str, ...]
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "checkpoint_id": self.checkpoint_id,
            "recovered": self.recovered,
            "residue": list(self.residue),
            "reason": self.reason,
        }


class CheckpointManager:
    """A lock-serialized checkpoint writer.

    Each checkpoint is first staged as a complete SQLite database and complete
    JSON snapshots, then installed with ``os.replace``.  The database commit is
    the final rename.  A crash before it leaves only a higher-generation orphan
    snapshot, which readers explicitly discard as a half-write.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        schema_version: int = CHECKPOINT_SCHEMA_VERSION,
        history_limit: int = 8,
    ):
        if schema_version < 1:
            raise ValueError("checkpoint schema_version must be >= 1")
        if history_limit < 1:
            raise ValueError("history_limit must be >= 1")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.schema_version = schema_version
        self.history_limit = history_limit
        self.database_path = self.directory / "checkpoints.sqlite3"
        self.latest_snapshot_path = self.directory / "checkpoint.json"
        self.lock_path = self.directory / "checkpoint.lock"

    def _thread_lock(self) -> threading.Lock:
        key = str(self.lock_path.resolve())
        with _PROCESS_LOCKS_GUARD:
            return _PROCESS_LOCKS.setdefault(key, threading.Lock())

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """Serialize writers/readers in-process and across processes."""

        local_lock = self._thread_lock()
        if not local_lock.acquire(blocking=False):
            raise CheckpointLocked("E_LOCK: checkpoint lock is already held")
        lock_file = None
        try:
            lock_file = self.lock_path.open("a+b")
            if lock_file.tell() == 0:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
            lock_file.seek(0)
            try:
                import fcntl  # type: ignore

                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise CheckpointLocked(
                        "E_LOCK: checkpoint file lock is already held"
                    ) from exc
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except ImportError:
                import msvcrt  # type: ignore

                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise CheckpointLocked(
                        "E_LOCK: checkpoint file lock is already held"
                    ) from exc
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            if lock_file is not None:
                lock_file.close()
            local_lock.release()

    def _snapshot_path(self, generation: int) -> Path:
        return self.directory / f"checkpoint-{generation:08d}.json"

    def _read_existing_rows(self) -> list[dict[str, Any]]:
        if not self.database_path.exists():
            return []
        connection = sqlite3.connect(
            _sqlite_readonly_uri(self.database_path), uri=True
        )
        try:
            cursor = connection.execute(
                "SELECT generation, checkpoint_id, schema_version, complete, "
                "payload_json, payload_sha256, config_fingerprint, rules_version, "
                "merge_policy_version, created_ms "
                "FROM checkpoints ORDER BY generation"
            )
            columns = [description[0] for description in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def _stage_database(self, rows: list[dict[str, Any]]) -> Path:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".checkpoints.",
            suffix=".sqlite3.tmp",
            dir=str(self.directory),
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        if temp_path.exists():
            temp_path.unlink()
        connection = sqlite3.connect(str(temp_path))
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE checkpoints (
                    generation INTEGER PRIMARY KEY,
                    checkpoint_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    rules_version TEXT NOT NULL,
                    merge_policy_version TEXT NOT NULL,
                    created_ms INTEGER NOT NULL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO checkpoints (
                    generation, checkpoint_id, schema_version, complete,
                    payload_json, payload_sha256, config_fingerprint,
                    rules_version, merge_policy_version, created_ms
                ) VALUES (
                    :generation, :checkpoint_id, :schema_version, :complete,
                    :payload_json, :payload_sha256, :config_fingerprint,
                    :rules_version, :merge_policy_version, :created_ms
                )
                """,
                rows,
            )
            connection.commit()
        finally:
            connection.close()
        return temp_path

    def save(
        self,
        payload: dict[str, Any],
        *,
        config_fingerprint: str,
        rules_version: str,
        merge_policy_version: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        payload_bytes = _payload_bytes(payload)
        payload_sha = _checksum(payload_bytes)
        with self.exclusive():
            rows = self._read_existing_rows()
            generation = max((int(row["generation"]) for row in rows), default=0) + 1
            checkpoint_id = checkpoint_id or _checksum(
                f"{generation}:{payload_sha}".encode("utf-8")
            )
            document = {
                "schema_version": self.schema_version,
                "generation": generation,
                "checkpoint_id": checkpoint_id,
                "complete": True,
                "payload_sha256": payload_sha,
                "payload": payload,
            }
            snapshot_bytes = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            row = {
                "generation": generation,
                "checkpoint_id": checkpoint_id,
                "schema_version": self.schema_version,
                "complete": 1,
                "payload_json": payload_bytes.decode("utf-8"),
                "payload_sha256": payload_sha,
                "config_fingerprint": config_fingerprint,
                "rules_version": rules_version,
                "merge_policy_version": merge_policy_version,
                "created_ms": int(time.time() * 1000),
            }
            rows.append(row)
            rows = sorted(rows, key=lambda item: int(item["generation"]))[
                -self.history_limit :
            ]
            staged_database = self._stage_database(rows)
            try:
                # Snapshot and latest pointer become visible first.  If the
                # process dies before the final database rename, readers still
                # select the previous complete database generation.
                _atomic_write(self._snapshot_path(generation), snapshot_bytes)
                _atomic_write(self.latest_snapshot_path, snapshot_bytes)
                os.replace(staged_database, self.database_path)
            finally:
                if staged_database.exists():
                    staged_database.unlink()
            return {
                "generation": generation,
                "checkpoint_id": checkpoint_id,
                "payload_sha256": payload_sha,
                "snapshot": str(self._snapshot_path(generation)),
                "database": str(self.database_path),
            }

    def _load_rows(self) -> list[dict[str, Any]]:
        if not self.database_path.exists():
            return []
        connection = sqlite3.connect(
            _sqlite_readonly_uri(self.database_path), uri=True
        )
        try:
            cursor = connection.execute(
                "SELECT generation, checkpoint_id, schema_version, complete, "
                "payload_json, payload_sha256, config_fingerprint, rules_version, "
                "merge_policy_version, created_ms "
                "FROM checkpoints ORDER BY generation DESC"
            )
            columns = [description[0] for description in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def _load_snapshot(self, generation: int) -> dict[str, Any] | None:
        path = self._snapshot_path(generation)
        try:
            with path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return document if isinstance(document, dict) else None

    def _residue(self, rows: list[dict[str, Any]]) -> list[str]:
        residue: list[str] = []
        max_db_generation = max(
            (int(row["generation"]) for row in rows), default=0
        )
        for path in sorted(self.directory.glob("checkpoint-*.json")):
            try:
                generation = int(path.stem.split("-")[-1])
            except ValueError:
                continue
            if generation > max_db_generation:
                residue.append(f"orphan_snapshot:{path.name}")
        for path in sorted(self.directory.glob("*.tmp")):
            residue.append(f"temp_residue:{path.name}")
        for path in sorted(self.directory.glob(".*.tmp")):
            residue.append(f"temp_residue:{path.name}")
        return residue

    def load(
        self,
        *,
        expected_config_fingerprint: str | None = None,
        expected_rules_version: str | None = None,
        expected_merge_policy_version: str | None = None,
    ) -> CheckpointLoadResult:
        with self.exclusive():
            rows = self._load_rows()
            residue = self._residue(rows)
            if not rows:
                return CheckpointLoadResult(
                    payload=None,
                    generation=None,
                    checkpoint_id=None,
                    recovered=False,
                    residue=tuple(residue),
                    reason="E_CHECKPOINT_NOT_FOUND",
                )
            reasons: list[str] = []
            for row in rows:
                generation = int(row["generation"])
                if int(row["schema_version"]) != self.schema_version:
                    reasons.append("E_CHECKPOINT_SCHEMA_CHANGED")
                    continue
                if not bool(row["complete"]):
                    residue.append(f"incomplete_checkpoint:{generation}")
                    continue
                if (
                    expected_config_fingerprint is not None
                    and row["config_fingerprint"] != expected_config_fingerprint
                ):
                    return CheckpointLoadResult(
                        None,
                        None,
                        None,
                        False,
                        tuple(residue),
                        "E_CHECKPOINT_CONFIG_CHANGED",
                    )
                if (
                    expected_rules_version is not None
                    and row["rules_version"] != expected_rules_version
                ):
                    return CheckpointLoadResult(
                        None,
                        None,
                        None,
                        False,
                        tuple(residue),
                        "E_CHECKPOINT_RULESET_CHANGED",
                    )
                if (
                    expected_merge_policy_version is not None
                    and row["merge_policy_version"]
                    != expected_merge_policy_version
                ):
                    return CheckpointLoadResult(
                        None,
                        None,
                        None,
                        False,
                        tuple(residue),
                        "E_CHECKPOINT_MERGE_POLICY_CHANGED",
                    )
                payload_bytes = str(row["payload_json"]).encode("utf-8")
                if _checksum(payload_bytes) != row["payload_sha256"]:
                    residue.append(f"payload_hash_mismatch:{generation}")
                    continue
                snapshot = self._load_snapshot(generation)
                if snapshot is None:
                    residue.append(f"snapshot_missing_or_invalid:{generation}")
                    continue
                if (
                    int(snapshot.get("schema_version", -1)) != self.schema_version
                    or int(snapshot.get("generation", -1)) != generation
                    or snapshot.get("checkpoint_id") != row["checkpoint_id"]
                    or snapshot.get("payload_sha256") != row["payload_sha256"]
                ):
                    residue.append(f"snapshot_mismatch:{generation}")
                    continue
                snapshot_payload = snapshot.get("payload")
                if not isinstance(snapshot_payload, dict) or _checksum(
                    _payload_bytes(snapshot_payload)
                ) != row["payload_sha256"]:
                    residue.append(f"snapshot_payload_mismatch:{generation}")
                    continue
                try:
                    payload = json.loads(row["payload_json"])
                except json.JSONDecodeError:
                    residue.append(f"payload_json_invalid:{generation}")
                    continue
                if not isinstance(payload, dict):
                    residue.append(f"payload_not_object:{generation}")
                    continue
                if max_db_generation := max(
                    int(candidate["generation"]) for candidate in rows
                ):
                    if generation < max_db_generation:
                        residue.append(f"superseded_by_invalid:{max_db_generation}")
                return CheckpointLoadResult(
                    payload=payload,
                    generation=generation,
                    checkpoint_id=str(row["checkpoint_id"]),
                    recovered=bool(residue),
                    residue=tuple(sorted(set(residue))),
                    reason=None,
                )
            reason = reasons[0] if reasons else "E_CHECKPOINT_NO_COMPLETE"
            return CheckpointLoadResult(
                None,
                None,
                None,
                bool(residue),
                tuple(sorted(set(residue))),
                reason,
            )

    def reset(self) -> None:
        with self.exclusive():
            for path in [
                self.database_path,
                self.latest_snapshot_path,
                *self.directory.glob("checkpoint-*.json"),
                *self.directory.glob("*.tmp"),
                *self.directory.glob(".*.tmp"),
            ]:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

