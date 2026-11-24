"""SQLite plus snapshot checkpoints with replay and file-lock isolation."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .engine import RenewalState, build_summary, replay_events
from .errors import BusinessError
from .io_utils import payload_checksum, read_json, write_json_atomic


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self._handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise BusinessError(
                "E_LOCK",
                "checkpoint directory is locked by another writer",
                details={"lock_path": str(self.path)},
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


@dataclass
class CheckpointView:
    status: str
    sequence: int
    checkpoint_id: str | None
    watermark: int
    state: RenewalState
    summary: dict[str, Any]
    events: list[dict[str, Any]]
    diagnostics: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sequence": self.sequence,
            "checkpoint_id": self.checkpoint_id,
            "watermark": self.watermark,
            "summary": self.summary,
            "events_count": len(self.events),
            "diagnostics": self.diagnostics,
        }


class _CheckpointSession:
    def __init__(self, manager: "CheckpointManager") -> None:
        self.manager = manager
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "_CheckpointSession":
        self.manager._ensure_layout()
        self.manager._lock.__enter__()
        try:
            self.connection = sqlite3.connect(
                self.manager.database_path,
                timeout=0,
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    sequence INTEGER PRIMARY KEY,
                    checkpoint_id TEXT NOT NULL UNIQUE,
                    schema_version INTEGER NOT NULL,
                    quota_version TEXT NOT NULL,
                    watermark INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    checksum TEXT NOT NULL
                )
                """
            )
            self.connection.commit()
        except BaseException:
            self.manager._lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        finally:
            self.manager._lock.__exit__(exc_type, exc, traceback)

    def load(self) -> CheckpointView:
        assert self.connection is not None
        return self.manager._load_unlocked(self.connection)

    def save(
        self,
        *,
        watermark: int,
        events: list[dict[str, Any]],
        state: RenewalState,
    ) -> CheckpointView:
        assert self.connection is not None
        return self.manager._save_unlocked(
            self.connection,
            watermark=watermark,
            events=events,
            state=state,
        )

    def reset(self) -> None:
        assert self.connection is not None
        self.manager._rebuild_unlocked(
            self.connection,
            reason="explicit reset",
        )


class CheckpointManager:
    def __init__(
        self,
        workdir: Path,
        *,
        schema_version: int,
        quota_version: str,
    ) -> None:
        self.workdir = Path(workdir)
        self.schema_version = schema_version
        self.quota_version = quota_version
        self.database_path = self.workdir / "checkpoint.sqlite3"
        self.snapshot_dir = self.workdir / "checkpoints"
        self.lock_path = self.workdir / "checkpoint.lock"
        self._lock = _FileLock(self.lock_path)

    def _ensure_layout(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def session(self) -> Iterator[_CheckpointSession]:
        session = _CheckpointSession(self)
        with session:
            yield session

    def _snapshot_path(self, sequence: int, checkpoint_id: str) -> Path:
        return self.snapshot_dir / f"ckpt-{sequence:020d}-{checkpoint_id}.json"

    def _row_ids(self, connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            "SELECT checkpoint_id FROM checkpoints"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _snapshot_ids(self) -> dict[str, Path]:
        files: dict[str, Path] = {}
        if not self.snapshot_dir.exists():
            return files
        for path in sorted(self.snapshot_dir.glob("ckpt-*.json")):
            parts = path.stem.split("-")
            if len(parts) >= 3:
                files[parts[-1]] = path
        return files

    def _rebuild_unlocked(
        self,
        connection: sqlite3.Connection,
        *,
        reason: str,
    ) -> None:
        connection.execute("DELETE FROM checkpoints")
        connection.commit()
        if self.snapshot_dir.exists():
            for path in self.snapshot_dir.glob("ckpt-*.json"):
                path.unlink(missing_ok=True)
        if self.database_path.exists():
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass

    def _validate_payload(
        self,
        payload: Any,
        *,
        expected_sequence: int,
        expected_checksum: str,
    ) -> tuple[dict[str, Any], str | None]:
        if not isinstance(payload, dict):
            return {}, "payload is not an object"
        if payload_checksum(payload) != expected_checksum:
            return {}, "payload checksum mismatch"
        if payload.get("sequence") != expected_sequence:
            return {}, "sequence mismatch"
        if not isinstance(payload.get("watermark"), int):
            return {}, "watermark is not an integer"
        if not isinstance(payload.get("events"), list):
            return {}, "events is not a list"
        if not isinstance(payload.get("state"), dict):
            return {}, "state is not an object"
        if not isinstance(payload.get("summary"), dict):
            return {}, "summary is not an object"
        return payload, None

    def _row_complete(
        self,
        connection: sqlite3.Connection,
        row: tuple[Any, ...],
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        sequence, checkpoint_id, schema_version, quota_version, watermark, payload_json, checksum = row
        snapshot_path = self._snapshot_path(int(sequence), str(checkpoint_id))
        if not snapshot_path.exists():
            return None, "missing_snapshot", str(snapshot_path)
        try:
            wrapper = read_json(snapshot_path)
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"unreadable_snapshot:{exc}", str(snapshot_path)
        if not isinstance(wrapper, dict) or wrapper.get("checksum") != checkpoint_id:
            return None, "snapshot_checksum_mismatch", str(snapshot_path)
        payload = wrapper.get("payload")
        validated, error = self._validate_payload(
            payload,
            expected_sequence=int(sequence),
            expected_checksum=str(checkpoint_id),
        )
        if error:
            return None, f"invalid_snapshot:{error}", str(snapshot_path)
        if payload_checksum(validated) != checksum:
            return None, "database_checksum_mismatch", str(snapshot_path)
        try:
            db_payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return None, "invalid_database_payload", str(snapshot_path)
        if db_payload != validated:
            return None, "snapshot_database_payload_mismatch", str(snapshot_path)
        if validated.get("schema_version") != schema_version:
            return None, "database_schema_metadata_mismatch", str(snapshot_path)
        if validated.get("quota_version") != quota_version:
            return None, "database_quota_metadata_mismatch", str(snapshot_path)
        if validated.get("watermark") != watermark:
            return None, "database_watermark_metadata_mismatch", str(snapshot_path)

        replay_state = replay_events(validated["events"]).as_dict()
        if replay_state != validated["state"]:
            return None, "event_replay_state_mismatch", str(snapshot_path)
        rebuilt_summary = build_summary(
            watermark=int(validated["watermark"]),
            events=validated["events"],
            state=replay_events(validated["events"]),
        )
        if rebuilt_summary != validated["summary"]:
            return None, "event_replay_summary_mismatch", str(snapshot_path)
        return validated, None, str(snapshot_path)

    def _load_unlocked(self, connection: sqlite3.Connection) -> CheckpointView:
        rows = connection.execute(
            """
            SELECT sequence, checkpoint_id, schema_version, quota_version,
                   watermark, payload_json, checksum
            FROM checkpoints
            ORDER BY sequence DESC
            """
        ).fetchall()
        if rows and any(
            row[2] != self.schema_version or row[3] != self.quota_version
            for row in rows
        ):
            diagnostics = {
                "orphan_snapshots": [],
                "damaged_checkpoints": [],
                "replay_verified": False,
                "rebuild_reason": (
                    "checkpoint structure or quota profile version changed"
                ),
            }
            self._rebuild_unlocked(
                connection,
                reason="schema or quota profile version changed",
            )
            return CheckpointView(
                status="rebuilt",
                sequence=0,
                checkpoint_id=None,
                watermark=0,
                state=RenewalState(),
                summary=build_summary(watermark=0, events=[], state=RenewalState()),
                events=[],
                diagnostics=diagnostics,
            )
        db_ids = self._row_ids(connection)
        snapshot_ids = self._snapshot_ids()
        orphans = sorted(
            {
                checkpoint_id: str(path)
                for checkpoint_id, path in snapshot_ids.items()
                if checkpoint_id not in db_ids
            }.items()
        )
        damaged: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        selected_sequence = 0
        selected_checkpoint_id: str | None = None
        for row in rows:
            payload, error, location = self._row_complete(connection, row)
            if error is None and payload is not None:
                selected = payload
                selected_sequence = int(row[0])
                selected_checkpoint_id = str(row[1])
                break
            damaged.append(
                {
                    "sequence": int(row[0]),
                    "checkpoint_id": str(row[1]),
                    "reason": error,
                    "location": location,
                }
            )

        diagnostics = {
            "orphan_snapshots": [
                {"checkpoint_id": checkpoint_id, "location": location}
                for checkpoint_id, location in orphans
            ],
            "damaged_checkpoints": damaged,
            "replay_verified": selected is not None,
            "rebuild_reason": None,
        }
        if selected is None:
            return CheckpointView(
                status="empty" if not rows and not orphans else "recovered_empty",
                sequence=0,
                checkpoint_id=None,
                watermark=0,
                state=RenewalState(),
                summary=build_summary(watermark=0, events=[], state=RenewalState()),
                events=[],
                diagnostics=diagnostics,
            )

        events = list(selected["events"])
        state = RenewalState.from_dict(selected["state"])
        return CheckpointView(
            status="resumed",
            sequence=selected_sequence,
            checkpoint_id=selected_checkpoint_id,
            watermark=int(selected["watermark"]),
            state=state,
            summary=selected["summary"],
            events=events,
            diagnostics=diagnostics,
        )

    def _save_unlocked(
        self,
        connection: sqlite3.Connection,
        *,
        watermark: int,
        events: list[dict[str, Any]],
        state: RenewalState,
    ) -> CheckpointView:
        if not isinstance(watermark, int) or watermark < 0:
            raise BusinessError("E_CHECKPOINT_CORRUPT", "watermark must be non-negative")
        replayed = replay_events(events)
        if replayed.as_dict() != state.as_dict():
            raise BusinessError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint state does not match event replay",
            )
        mismatched = connection.execute(
            """
            SELECT COUNT(*) FROM checkpoints
            WHERE schema_version <> ? OR quota_version <> ?
            """,
            (self.schema_version, self.quota_version),
        ).fetchone()[0]
        if mismatched:
            self._rebuild_unlocked(
                connection,
                reason="schema or quota profile version changed before save",
            )
        summary = build_summary(watermark=watermark, events=events, state=replayed)
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM checkpoints"
            ).fetchone()[0]
        )
        payload = {
            "schema_version": self.schema_version,
            "quota_version": self.quota_version,
            "sequence": sequence,
            "watermark": watermark,
            "state": replayed.as_dict(),
            "summary": summary,
            "events": events,
        }
        checkpoint_id = payload_checksum(payload)
        snapshot_path = self._snapshot_path(sequence, checkpoint_id)
        write_json_atomic(
            snapshot_path,
            {"checksum": checkpoint_id, "payload": payload},
        )
        try:
            connection.execute(
                """
                INSERT INTO checkpoints (
                    sequence, checkpoint_id, schema_version, quota_version,
                    watermark, payload_json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    checkpoint_id,
                    self.schema_version,
                    self.quota_version,
                    watermark,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    checkpoint_id,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            # The already-written snapshot is deliberately left in place. On
            # recovery it is reported as half-write residue and ignored.
            raise
        view = self._load_unlocked(connection)
        view.diagnostics["replay_verified"] = True
        return view

    def load_for_resume(self) -> CheckpointView:
        with self.session() as session:
            return session.load()

    def save(
        self,
        *,
        watermark: int,
        events: list[dict[str, Any]],
        state: RenewalState,
    ) -> CheckpointView:
        with self.session() as session:
            return session.save(watermark=watermark, events=events, state=state)

    def reset(self) -> None:
        with self.session() as session:
            session.reset()

