"""SQLite plus atomic JSON snapshot checkpoints."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .codes import (
    E_CHECKPOINT_CORRUPT,
    E_CHECKPOINT_NOT_FOUND,
    E_CHECKPOINT_VERSION,
    W_CHECKPOINT_RESIDUE,
)
from .fixedpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    FIXED_SCALE,
    FIXED_SCALE_VERSION,
    FIXED_SCALE_VERSION_NUMBER,
)


class CheckpointVersionError(RuntimeError):
    code = E_CHECKPOINT_VERSION


class CheckpointCorruptError(RuntimeError):
    code = E_CHECKPOINT_CORRUPT


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def state_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def atomic_write_json(path: str | Path, value: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


@dataclass
class Checkpoint:
    checkpoint_id: int
    round_watermark: int
    logical_clock_q: int
    state: dict[str, Any]
    state_sha256: str
    snapshot_path: Path


class CheckpointManager:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = self.workspace / "snapshots"
        self.snapshot_dir.mkdir(exist_ok=True)
        self.db_path = self.workspace / "checkpoints.sqlite3"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CheckpointManager":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def initialize(self) -> dict[str, Any]:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_watermark INTEGER NOT NULL,
                logical_clock_q INTEGER NOT NULL,
                state_sha256 TEXT NOT NULL,
                state_json TEXT NOT NULL,
                snapshot_path TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        self.conn.commit()
        existing = {
            row["key"]: row["value"]
            for row in self.conn.execute("SELECT key, value FROM meta")
        }
        expected = {
            "checkpoint_schema_version": str(CHECKPOINT_SCHEMA_VERSION),
            "fixed_scale_version": FIXED_SCALE_VERSION,
            "fixed_scale_version_number": str(FIXED_SCALE_VERSION_NUMBER),
            "fixed_scale": str(FIXED_SCALE),
        }
        if existing:
            for key, value in expected.items():
                if existing.get(key) != value:
                    raise CheckpointVersionError(
                        f"checkpoint version mismatch for {key}: "
                        f"stored={existing.get(key)!r}, current={value!r}"
                    )
        else:
            for key, value in expected.items():
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)", (key, value)
                )
            self.conn.commit()
        return expected

    @classmethod
    def prepare(
        cls, workspace: str | Path, rebuild_on_mismatch: bool = True
    ) -> tuple["CheckpointManager", dict[str, Any]]:
        workspace = Path(workspace)
        manager = cls(workspace)
        rebuild_info: dict[str, Any] = {"rebuilt": False}
        try:
            manager.initialize()
            return manager, rebuild_info
        except CheckpointVersionError as exc:
            manager.close()
            if not rebuild_on_mismatch:
                raise
            suffix = "version-mismatch"
            db_backup = workspace / f"checkpoints.sqlite3.{suffix}.bak"
            snapshot_backup = workspace / f"snapshots.{suffix}.bak"
            counter = 1
            while db_backup.exists() or snapshot_backup.exists():
                db_backup = workspace / f"checkpoints.sqlite3.{suffix}-{counter}.bak"
                snapshot_backup = workspace / f"snapshots.{suffix}-{counter}.bak"
                counter += 1
            if (workspace / "checkpoints.sqlite3").exists():
                shutil.move(str(workspace / "checkpoints.sqlite3"), str(db_backup))
            if (workspace / "snapshots").exists():
                shutil.move(str(workspace / "snapshots"), str(snapshot_backup))
            manager = cls(workspace)
            manager.initialize()
            rebuild_info = {
                "rebuilt": True,
                "code": E_CHECKPOINT_VERSION,
                "reason": str(exc),
                "database_backup": str(db_backup),
                "snapshot_backup": str(snapshot_backup),
            }
            return manager, rebuild_info

    def save_checkpoint(self, state: dict[str, Any]) -> Checkpoint:
        watermark = int(state["round_watermark"])
        clock_q = int(state["logical_clock_q"])
        digest = state_hash(state)
        cursor = self.conn.execute(
            """
            INSERT INTO checkpoints(round_watermark, logical_clock_q, state_sha256,
                                    state_json, snapshot_path, status)
            VALUES (?, ?, ?, ?, ?, 'writing')
            """,
            (watermark, clock_q, digest, canonical_json(state), ""),
        )
        checkpoint_id = int(cursor.lastrowid)
        relative_path = Path("snapshots") / f"checkpoint-{watermark:06d}-{checkpoint_id:06d}.json"
        absolute_path = self.workspace / relative_path
        snapshot_document = {
            "checkpoint_id": checkpoint_id,
            "round_watermark": watermark,
            "logical_clock_q": clock_q,
            "state_sha256": digest,
            "state": state,
        }
        atomic_write_json(absolute_path, snapshot_document)
        self.conn.execute(
            """
            UPDATE checkpoints
               SET snapshot_path = ?, status = 'complete', state_sha256 = ?, state_json = ?
             WHERE checkpoint_id = ?
            """,
            (str(relative_path), digest, canonical_json(state), checkpoint_id),
        )
        self.conn.commit()
        return Checkpoint(
            checkpoint_id=checkpoint_id,
            round_watermark=watermark,
            logical_clock_q=clock_q,
            state=state,
            state_sha256=digest,
            snapshot_path=absolute_path,
        )

    def _row_to_checkpoint(self, row: sqlite3.Row) -> Checkpoint:
        state = json.loads(row["state_json"])
        snapshot_path = Path(row["snapshot_path"])
        if not snapshot_path.is_absolute():
            snapshot_path = self.workspace / snapshot_path
        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            round_watermark=row["round_watermark"],
            logical_clock_q=row["logical_clock_q"],
            state=state,
            state_sha256=row["state_sha256"],
            snapshot_path=snapshot_path,
        )

    def list_checkpoints(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM checkpoints ORDER BY round_watermark DESC, checkpoint_id DESC"
        ))

    def verify(self) -> dict[str, Any]:
        meta = {row["key"]: row["value"] for row in self.conn.execute("SELECT key, value FROM meta")}
        result: dict[str, Any] = {
            "valid": True,
            "code": None,
            "checkpoint_schema_version": meta.get("checkpoint_schema_version"),
            "fixed_scale_version": meta.get("fixed_scale_version"),
            "current_checkpoint_schema_version": str(CHECKPOINT_SCHEMA_VERSION),
            "current_fixed_scale_version": FIXED_SCALE_VERSION,
            "checkpoints": [],
            "residue": [],
            "issues": [],
            "latest_watermark": None,
            "latest_intact_watermark": None,
        }
        version_ok = (
            meta.get("checkpoint_schema_version") == str(CHECKPOINT_SCHEMA_VERSION)
            and meta.get("fixed_scale_version") == FIXED_SCALE_VERSION
            and meta.get("fixed_scale") == str(FIXED_SCALE)
        )
        if not version_ok:
            result["valid"] = False
            result["code"] = E_CHECKPOINT_VERSION
            result["issues"].append("checkpoint schema or fixed-point scale version changed")

        rows = self.list_checkpoints()
        if rows:
            result["latest_watermark"] = rows[0]["round_watermark"]

        latest_intact: Optional[sqlite3.Row] = None
        for row in rows:
            issues: list[str] = []
            row_id = row["checkpoint_id"]
            if row["status"] != "complete":
                issues.append(f"checkpoint {row_id} has status {row['status']!r}")
            snapshot_path = Path(row["snapshot_path"]) if row["snapshot_path"] else None
            if snapshot_path is None:
                issues.append(f"checkpoint {row_id} has no snapshot path")
            else:
                if not snapshot_path.is_absolute():
                    snapshot_path = self.workspace / snapshot_path
                if not snapshot_path.exists():
                    issues.append(f"missing snapshot: {snapshot_path}")
                else:
                    try:
                        document = json.loads(snapshot_path.read_text(encoding="utf-8"))
                        snapshot_state = document["state"]
                        if state_hash(snapshot_state) != row["state_sha256"]:
                            issues.append(f"snapshot hash mismatch for checkpoint {row_id}")
                        if state_hash(json.loads(row["state_json"])) != row["state_sha256"]:
                            issues.append(f"database state hash mismatch for checkpoint {row_id}")
                        if document.get("state_sha256") != row["state_sha256"]:
                            issues.append(f"snapshot envelope hash mismatch for checkpoint {row_id}")
                    except (OSError, json.JSONDecodeError, KeyError) as exc:
                        issues.append(f"unreadable snapshot for checkpoint {row_id}: {exc}")
            intact = not issues and row["status"] == "complete"
            result["checkpoints"].append({
                "checkpoint_id": row_id,
                "watermark": row["round_watermark"],
                "status": row["status"],
                "intact": intact,
                "issues": issues,
            })
            if intact and latest_intact is None:
                latest_intact = row
            elif issues:
                result["issues"].extend(issues)

        for pattern_root in (self.workspace, self.snapshot_dir):
            if not pattern_root.exists():
                continue
            for temporary in pattern_root.glob("**/*.tmp"):
                result["residue"].append(str(temporary))
            for temporary in pattern_root.glob("**/.*.tmp"):
                if str(temporary) not in result["residue"]:
                    result["residue"].append(str(temporary))
        if result["residue"]:
            result["warning_code"] = W_CHECKPOINT_RESIDUE

        if latest_intact is not None:
            result["latest_intact_watermark"] = latest_intact["round_watermark"]
            result["latest_intact_checkpoint_id"] = latest_intact["checkpoint_id"]
        elif not rows and version_ok:
            result["valid"] = False
            result["code"] = E_CHECKPOINT_NOT_FOUND
            result["issues"].append("no checkpoint rows found")
        elif rows and version_ok:
            result["valid"] = False
            result["code"] = E_CHECKPOINT_CORRUPT
            result["issues"].append("no intact complete checkpoint is available")
        return result

    def load_latest_intact(self) -> Optional[tuple[Checkpoint, dict[str, Any]]]:
        verification = self.verify()
        watermark = verification.get("latest_intact_watermark")
        checkpoint_id = verification.get("latest_intact_checkpoint_id")
        if watermark is None or checkpoint_id is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_checkpoint(row), verification

    def save_named_json(self, key: str, value: Any) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, canonical_json(value)),
        )
        self.conn.commit()

    def load_named_json(self, key: str) -> Optional[Any]:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None


def remove_residue_files(verification: dict[str, Any]) -> list[str]:
    removed = []
    for name in verification.get("residue", []):
        path = Path(name)
        try:
            path.unlink(missing_ok=True)
            removed.append(str(path))
        except OSError:
            pass
    return removed
