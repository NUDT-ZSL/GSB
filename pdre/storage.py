"""Atomic snapshots, file locking, hash ledger, and SQLite checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import CHECKPOINT_STRUCTURE_VERSION, MERGE_SEMANTICS_VERSION
from .canonical import canonical_json, stable_hash
from .codes import BusinessCode as Codes


class StorageError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


class LockConflict(StorageError):
    def __init__(self, lock_path: Path):
        super().__init__(Codes.LOCK, f"checkpoint lock is held: {lock_path}")
        self.lock_path = lock_path


class FileLock:
    def __init__(self, path: str | Path, timeout_ms: int = 0) -> None:
        self.path = Path(path)
        self.timeout_ms = timeout_ms
        self._file = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        if self.path.stat().st_size == 0:
            self._file.write(b"\0")
            self._file.flush()

        deadline = time.perf_counter() + self.timeout_ms / 1000.0
        while True:
            try:
                if os.name == "nt":  # pragma: no cover - exercised on Windows
                    import msvcrt

                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.perf_counter() >= deadline:
                    self._file.close()
                    self._file = None
                    raise LockConflict(self.path)
                time.sleep(0.01)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        with temporary.open("r", encoding="utf-8") as handle:
            json.load(handle)

        try:
            os.chmod(temporary, 0o644)
        except OSError:
            pass
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return target
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass
class LedgerAppendResult:
    entry_count: int
    byte_offset: int
    digest: str
    tail_hash: str


class Ledger:
    GENESIS_HASH = "0" * 64

    def __init__(
        self,
        path: str | Path,
        *,
        entry_count: int = 0,
        tail_hash: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entry_count = entry_count
        self.tail_hash = tail_hash or self.GENESIS_HASH

    def append_unit_judgments(
        self,
        units: list[dict[str, Any]],
        *,
        batch_id: str | None,
        unit_versions: dict[str, int] | None = None,
    ) -> LedgerAppendResult:
        with self.path.open("a", encoding="utf-8") as handle:
            for unit in sorted(units, key=lambda item: item["unit_id"]):
                self.entry_count += 1
                body = {
                    "ledger_seq": self.entry_count,
                    "entry_type": "unit_judgment",
                    "batch_id": batch_id,
                    "unit_id": unit["unit_id"],
                    "unit_version": (unit_versions or {}).get(unit["unit_id"], 1),
                    "semantic": unit,
                    "prev_hash": self.tail_hash,
                }
                entry_hash = stable_hash(body)
                entry = {**body, "entry_hash": entry_hash}
                handle.write(canonical_json(entry))
                handle.write("\n")
                self.tail_hash = entry_hash
            handle.flush()
            os.fsync(handle.fileno())
        return LedgerAppendResult(
            entry_count=self.entry_count,
            byte_offset=self.path.stat().st_size,
            digest=sha256_file(self.path),
            tail_hash=self.tail_hash,
        )


@dataclass
class RecoveryResult:
    checkpoint: dict[str, Any]
    state: dict[str, Any]
    ledger_entries: list[dict[str, Any]]
    residue: dict[str, Any]


class CheckpointManager:
    def __init__(
        self,
        checkpoint_dir: str | Path,
        *,
        snapshot_dir: str | Path | None = None,
        ledger_path: str | Path | None = None,
        structure_version: int = CHECKPOINT_STRUCTURE_VERSION,
        semantics_version: int = MERGE_SEMANTICS_VERSION,
        lock_timeout_ms: int = 0,
    ) -> None:
        self.checkpoint_dir = Path(checkpoint_dir)
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else self.checkpoint_dir / "snapshots"
        self.ledger_path = Path(ledger_path) if ledger_path else self.checkpoint_dir / "ledger.jsonl"
        self.db_path = self.checkpoint_dir / "checkpoints.sqlite3"
        self.lock_path = self.checkpoint_dir / "checkpoint.lock"
        self.structure_version = structure_version
        self.semantics_version = semantics_version
        self.lock_timeout_ms = lock_timeout_ms
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=1.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    structure_version INTEGER NOT NULL,
                    semantics_version INTEGER NOT NULL,
                    watermark TEXT NOT NULL,
                    batch_seq INTEGER NOT NULL,
                    ledger_seq INTEGER NOT NULL,
                    ledger_digest TEXT NOT NULL,
                    ledger_byte_offset INTEGER NOT NULL,
                    ledger_tail_hash TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    state_json TEXT NOT NULL
                )
                """
            )

    def save_checkpoint(
        self,
        *,
        watermark: str,
        batch_seq: int,
        state: dict[str, Any],
        ledger: LedgerAppendResult,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path, timeout_ms=self.lock_timeout_ms):
            payload = {
                "status": "complete",
                "structure_version": self.structure_version,
                "semantics_version": self.semantics_version,
                "watermark": watermark,
                "batch_seq": batch_seq,
                "ledger": {
                    "entry_count": ledger.entry_count,
                    "byte_offset": ledger.byte_offset,
                    "digest": ledger.digest,
                    "tail_hash": ledger.tail_hash,
                    "path": str(self.ledger_path),
                },
                "state": state,
            }
            snapshot_path = self.snapshot_dir / f"checkpoint-{batch_seq:06d}.json"
            atomic_write_json(snapshot_path, payload)
            snapshot_digest = sha256_file(snapshot_path)
            state_json = canonical_json(payload)

            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO checkpoints (
                        status, structure_version, semantics_version, watermark,
                        batch_seq, ledger_seq, ledger_digest, ledger_byte_offset,
                        ledger_tail_hash, snapshot_path, snapshot_sha256, state_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "complete",
                        self.structure_version,
                        self.semantics_version,
                        watermark,
                        batch_seq,
                        ledger.entry_count,
                        ledger.digest,
                        ledger.byte_offset,
                        ledger.tail_hash,
                        str(snapshot_path),
                        snapshot_digest,
                        state_json,
                    ),
                )
                checkpoint_id = int(cursor.lastrowid)

            pointer = {
                "checkpoint_id": checkpoint_id,
                "snapshot_path": str(snapshot_path),
                "watermark": watermark,
                "batch_seq": batch_seq,
                "ledger_digest": ledger.digest,
            }
            atomic_write_json(self.snapshot_dir / "latest.json", pointer)
            return {"checkpoint_id": checkpoint_id, **pointer}

    def latest_row(self) -> Optional[sqlite3.Row]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE status = 'complete' ORDER BY checkpoint_id DESC LIMIT 1"
            ).fetchone()
        return row

    def recover(self) -> Optional[RecoveryResult]:
        row = self.latest_row()
        if row is None:
            return None
        if (
            row["structure_version"] != self.structure_version
            or row["semantics_version"] != self.semantics_version
        ):
            raise StorageError(
                Codes.VERSION_MISMATCH,
                "checkpoint versions do not match current merge semantics",
                {
                    "checkpoint_structure_version": row["structure_version"],
                    "checkpoint_semantics_version": row["semantics_version"],
                    "current_structure_version": self.structure_version,
                    "current_semantics_version": self.semantics_version,
                    "checkpoint_id": row["checkpoint_id"],
                },
            )

        snapshot_path = Path(row["snapshot_path"])
        if not snapshot_path.exists():
            raise StorageError(
                Codes.LEDGER_REPLAY,
                f"checkpoint snapshot is missing: {snapshot_path}",
                {"checkpoint_id": row["checkpoint_id"]},
            )
        if sha256_file(snapshot_path) != row["snapshot_sha256"]:
            raise StorageError(
                Codes.LEDGER_REPLAY,
                "checkpoint snapshot digest mismatch",
                {"snapshot_path": str(snapshot_path)},
            )

        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        ledger_info, entries, residue = self._replay_ledger(row)
        return RecoveryResult(
            checkpoint={
                "checkpoint_id": row["checkpoint_id"],
                "watermark": row["watermark"],
                "batch_seq": row["batch_seq"],
                **ledger_info,
            },
            state=payload["state"],
            ledger_entries=entries,
            residue=residue,
        )

    def _replay_ledger(self, row: sqlite3.Row) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        count = int(row["ledger_seq"])
        offset = int(row["ledger_byte_offset"])
        expected_digest = row["ledger_digest"]
        expected_tail = row["ledger_tail_hash"]

        residue: dict[str, Any] = {
            "half_write_detected": False,
            "extra_bytes": 0,
            "discarded_entries": 0,
            "temporary_files": [],
        }
        for directory in (self.checkpoint_dir, self.snapshot_dir):
            residue["temporary_files"].extend(
                sorted(str(path) for path in directory.glob("*.tmp"))
            )
        if residue["temporary_files"]:
            residue["half_write_detected"] = True

        if not self.ledger_path.exists():
            raise StorageError(
                Codes.LEDGER_REPLAY,
                "ledger file is missing despite complete checkpoint",
                {"ledger_path": str(self.ledger_path)},
            )

        raw = self.ledger_path.read_bytes()
        if len(raw) < offset or sha256_bytes(raw[:offset]) != expected_digest:
            raise StorageError(
                Codes.LEDGER_REPLAY,
                "committed ledger prefix digest mismatch; refusing incremental continuation",
                {"ledger_path": str(self.ledger_path), "expected_digest": expected_digest},
            )

        prefix = raw[:offset]
        text = prefix.decode("utf-8")
        entries = [json.loads(line) for line in text.splitlines() if line]
        if len(entries) != count:
            raise StorageError(
                Codes.LEDGER_REPLAY,
                "ledger entry count does not match checkpoint",
                {"expected": count, "actual": len(entries)},
            )

        previous_hash = Ledger.GENESIS_HASH
        for entry in entries:
            expected_prev = previous_hash
            entry_hash = entry.pop("entry_hash", None)
            if entry.get("prev_hash") != expected_prev:
                raise StorageError(Codes.LEDGER_REPLAY, "ledger hash chain is broken", {"seq": entry.get("ledger_seq")})
            if stable_hash(entry) != entry_hash:
                raise StorageError(Codes.LEDGER_REPLAY, "ledger entry hash mismatch", {"seq": entry.get("ledger_seq")})
            entry["entry_hash"] = entry_hash
            previous_hash = entry_hash

        if previous_hash != expected_tail:
            raise StorageError(Codes.LEDGER_REPLAY, "ledger tail hash mismatch", {})

        extra = raw[offset:]
        if extra:
            residue["extra_bytes"] = len(extra)
            extra_lines = [line for line in extra.splitlines() if line.strip()]
            residue["discarded_entries"] = max(0, len(extra_lines))
            try:
                for line in extra_lines:
                    json.loads(line)
            except json.JSONDecodeError:
                residue["half_write_detected"] = True

            with self.ledger_path.open("r+b") as handle:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())

        ledger_info = {
            "entry_count": count,
            "byte_offset": offset,
            "digest": expected_digest,
            "tail_hash": expected_tail,
        }
        return ledger_info, entries, residue

    def reset_for_full_rebuild(self) -> None:
        """Move incompatible state aside conceptually by clearing ledger pointer state."""
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        if self.ledger_path.exists():
            backup = self.ledger_path.with_suffix(".rebuild.jsonl")
            os.replace(self.ledger_path, backup)
