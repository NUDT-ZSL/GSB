"""SQLite boundary ledger, transactional batches, and checkpoints."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .codes import (
    E_CHECKPOINT_INVALID,
    E_LEDGER_REPLAY_MISMATCH,
    E_SNAPSHOT_INVALID,
    E_SNAPSHOT_NEWER,
    E_SNAPSHOT_STALE,
    LEDGER_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
)
from .models import LedgerBatch, Rejection
from .util import (
    GuardError,
    atomic_write_json,
    canonical_digest,
    canonical_json_bytes,
)


@dataclass
class RecoveryInfo:
    rebuilt: bool = False
    skipped_batches: int = 0
    warnings: list[Rejection] | None = None
    snapshot_status: str = "absent"
    stale_temp_files: list[str] | None = None
    last_batch_id: Optional[str] = None
    watermark_seq: int = -1

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []
        if self.stale_temp_files is None:
            self.stale_temp_files = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "rebuilt": self.rebuilt,
            "skipped_batches": self.skipped_batches,
            "warnings": [item.to_dict() for item in self.warnings or []],
            "snapshot_status": self.snapshot_status,
            "stale_temp_files": list(self.stale_temp_files or []),
            "last_batch_id": self.last_batch_id,
            "watermark_seq": self.watermark_seq,
        }


class Ledger:
    def __init__(self, database_path: Path, snapshot_path: Path):
        self.database_path = Path(database_path)
        self.snapshot_path = Path(snapshot_path)
        self.connection: Optional[sqlite3.Connection] = None
        self.layout_key: Optional[str] = None
        self.snapshot_version = 0
        self.recovery = RecoveryInfo()

    def open(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        self.connection = connection
        self._create_schema()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "Ledger":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("ledger is not open")
        return self.connection

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS region_ledger (
                region_id TEXT NOT NULL,
                access_kind TEXT NOT NULL,
                access_count INTEGER NOT NULL,
                oob_count INTEGER NOT NULL,
                oob_upper_bound TEXT,
                processed_access_seq TEXT NOT NULL,
                PRIMARY KEY (region_id, access_kind)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                layout_key TEXT NOT NULL,
                first_seq TEXT NOT NULL,
                last_seq TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                delta_json TEXT NOT NULL,
                delta_digest TEXT NOT NULL,
                ledger_digest TEXT NOT NULL,
                applied_at_ms INTEGER NOT NULL,
                complete INTEGER NOT NULL CHECK (complete = 1)
            );
            CREATE TABLE IF NOT EXISTS checkpoint (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                checkpoint_version INTEGER NOT NULL,
                batch_id TEXT,
                watermark_seq TEXT,
                ledger_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL
            );
            """
        )

    def _meta(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def prepare(self, layout_key: str) -> RecoveryInfo:
        recovery = RecoveryInfo()
        recovery.stale_temp_files = self._stale_snapshot_temps()
        for stale in recovery.stale_temp_files:
            recovery.warnings.append(
                Rejection(
                    source_file=stale,
                    line_number=None,
                    business_code=E_SNAPSHOT_STALE,
                    message="incomplete atomic snapshot temp file was ignored",
                    detail={"snapshot_temp": stale},
                )
            )
            try:
                Path(stale).unlink()
            except OSError:
                pass

        snapshot_document: Optional[dict[str, Any]] = None
        snapshot_invalid = False
        if self.snapshot_path.exists():
            try:
                loaded = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("snapshot root is not an object")
                if loaded.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
                    raise ValueError("unsupported snapshot schema_version")
                snapshot_version = loaded.get("snapshot_version")
                if type(snapshot_version) is not int or snapshot_version < 0:
                    raise ValueError("invalid snapshot_version")
                snapshot_document = loaded
                self.snapshot_version = snapshot_version
                recovery.snapshot_status = "valid"
            except (OSError, ValueError, TypeError) as exc:
                recovery.snapshot_status = "invalid"
                snapshot_invalid = True
                self.snapshot_version = 0
                recovery.warnings.append(
                    Rejection(
                        source_file=str(self.snapshot_path),
                        line_number=None,
                        business_code=E_SNAPSHOT_INVALID,
                        message="snapshot file cannot be parsed; it was not trusted",
                        detail={"error": str(exc)},
                    )
                )
                # SQLite is the authoritative complete checkpoint. A corrupt
                # final snapshot is reported and rebuilt if SQLite is healthy.

        self.layout_key = layout_key
        db_layout = self._meta("layout_key")
        previous_snapshot_version = self.snapshot_version
        had_snapshot_file = self.snapshot_path.exists()
        if db_layout != layout_key:
            self._rebuild(layout_key)
            recovery.rebuilt = True
            recovery.snapshot_status = "rebuilt"
            if snapshot_document is not None:
                self.snapshot_version = max(
                    self.snapshot_version,
                    int(snapshot_document.get("snapshot_version", 0)),
                )
            # The new run may use the next snapshot version, but only the
            # compare-before-replace check in atomic_write_json can advance it.
            self.snapshot_version += 1
            self._ensure_empty_checkpoint(
                expected_version=(
                    previous_snapshot_version
                    if had_snapshot_file and not snapshot_invalid
                    else None
                )
            )
        else:
            self._verify_recovery_consistency(
                recovery, snapshot_document, snapshot_invalid=snapshot_invalid
            )

        recovery.last_batch_id = self._meta("last_batch_id")
        watermark = self._meta("watermark_seq")
        recovery.watermark_seq = -1 if watermark is None else int(watermark)
        self.recovery = recovery
        return recovery

    def _stale_snapshot_temps(self) -> list[str]:
        if not self.snapshot_path.parent.exists():
            return []
        prefix = ".%s." % self.snapshot_path.name
        return sorted(
            str(path)
            for path in self.snapshot_path.parent.iterdir()
            if path.name.startswith(prefix) and path.name.endswith(".tmp")
        )

    def _rebuild(self, layout_key: str) -> None:
        self._begin()
        try:
            self.db.execute("DELETE FROM region_ledger")
            self.db.execute("DELETE FROM batches")
            self.db.execute("DELETE FROM checkpoint")
            self._set_meta("layout_key", layout_key)
            self._set_meta("ledger_schema_version", LEDGER_SCHEMA_VERSION)
            self._set_meta("last_batch_id", "")
            self._set_meta("watermark_seq", "-1")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _begin(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def _rows(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT region_id, access_kind, access_count, oob_count, "
            "oob_upper_bound, processed_access_seq FROM region_ledger "
            "ORDER BY region_id, access_kind"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            upper = row["oob_upper_bound"]
            result.append(
                {
                    "region_id": str(row["region_id"]),
                    "access_kind": str(row["access_kind"]),
                    "access_count": int(row["access_count"]),
                    "oob_count": int(row["oob_count"]),
                    "oob_upper_bound": None if upper is None else int(upper),
                    "processed_access_seq": int(row["processed_access_seq"]),
                }
            )
        return result

    def _ledger_digest(self) -> str:
        return canonical_digest(self._rows())

    def _checkpoint_payload(
        self,
        checkpoint_version: int,
        batch_id: Optional[str],
        watermark_seq: int,
    ) -> dict[str, Any]:
        rows = self._rows()
        ledger_digest = canonical_digest(rows)
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_version": checkpoint_version,
            "layout_key": self.layout_key or self._meta("layout_key"),
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "batch_id": batch_id,
            "watermark_seq": watermark_seq,
            "ledger_digest": ledger_digest,
            "ledger_rows": rows,
        }
        payload["payload_digest"] = canonical_digest(payload)
        return payload

    def _write_checkpoint_row(self, payload: Mapping[str, Any]) -> None:
        batch_id = payload.get("batch_id")
        watermark = payload.get("watermark_seq")
        watermark_text = None if watermark is None else str(int(watermark))
        self.db.execute(
            "INSERT INTO checkpoint("
            "id, checkpoint_version, batch_id, watermark_seq, ledger_digest, "
            "payload_json, payload_digest"
            ") VALUES(1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "checkpoint_version=excluded.checkpoint_version, "
            "batch_id=excluded.batch_id, "
            "watermark_seq=excluded.watermark_seq, "
            "ledger_digest=excluded.ledger_digest, "
            "payload_json=excluded.payload_json, "
            "payload_digest=excluded.payload_digest",
            (
                int(payload["snapshot_version"]),
                batch_id,
                watermark_text,
                str(payload["ledger_digest"]),
                canonical_json_bytes(payload).decode("utf-8"),
                str(payload["payload_digest"]),
            ),
        )

    def _ensure_empty_checkpoint(self, *, expected_version: Optional[int] = None) -> None:
        row = self.db.execute("SELECT 1 FROM checkpoint WHERE id = 1").fetchone()
        if row is not None:
            return
        self._begin()
        try:
            payload = self._checkpoint_payload(
                self.snapshot_version, None, -1
            )
            self._write_checkpoint_row(payload)
            self._set_meta("last_batch_id", "")
            self._set_meta("watermark_seq", "-1")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self._write_snapshot(payload, expected_version=expected_version)

    def _verify_recovery_consistency(
        self,
        recovery: RecoveryInfo,
        snapshot_document: Optional[dict[str, Any]],
        *,
        snapshot_invalid: bool,
    ) -> None:
        file_snapshot_version = self.snapshot_version
        checkpoint = self.db.execute(
            "SELECT * FROM checkpoint WHERE id = 1"
        ).fetchone()
        if checkpoint is None:
            if self.db.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]:
                raise GuardError(
                    E_LEDGER_REPLAY_MISMATCH,
                    "ledger has batches but no complete checkpoint",
                )
            self._ensure_empty_checkpoint(
                expected_version=(
                    None
                    if snapshot_invalid or not self.snapshot_path.exists()
                    else self.snapshot_version
                )
            )
            recovery.snapshot_status = "initialized"
            return

        try:
            payload = json.loads(str(checkpoint["payload_json"]))
        except (ValueError, TypeError) as exc:
            raise GuardError(
                E_CHECKPOINT_INVALID,
                "checkpoint payload is not valid JSON",
                {"error": str(exc)},
            ) from exc
        stored_payload_digest = payload.pop("payload_digest", None)
        if stored_payload_digest != canonical_digest(payload):
            raise GuardError(
                E_CHECKPOINT_INVALID,
                "checkpoint payload digest does not verify",
            )
        if stored_payload_digest != checkpoint["payload_digest"]:
            raise GuardError(
                E_CHECKPOINT_INVALID,
                "checkpoint row digest does not verify",
            )
        ledger_rows = payload.get("ledger_rows")
        if not isinstance(ledger_rows, list):
            raise GuardError(E_CHECKPOINT_INVALID, "checkpoint ledger_rows is invalid")
        if canonical_digest(ledger_rows) != payload.get("ledger_digest"):
            raise GuardError(E_CHECKPOINT_INVALID, "checkpoint ledger digest is invalid")

        replay: dict[tuple[str, str], dict[str, Any]] = {}
        batch_rows = sorted(
            self.db.execute("SELECT * FROM batches").fetchall(),
            key=lambda row: (int(row["first_seq"]), int(row["last_seq"]), row["batch_id"]),
        )
        for row in batch_rows:
            delta_text = str(row["delta_json"])
            if canonical_digest(json.loads(delta_text)) != row["delta_digest"]:
                raise GuardError(
                    E_LEDGER_REPLAY_MISMATCH,
                    "batch delta digest does not verify",
                    {"batch_id": row["batch_id"]},
                )
            entries = json.loads(delta_text)
            for entry in entries:
                key = (str(entry["region_id"]), str(entry["access_kind"]))
                current = replay.setdefault(
                    key,
                    {
                        "region_id": key[0],
                        "access_kind": key[1],
                        "access_count": 0,
                        "oob_count": 0,
                        "oob_upper_bound": None,
                        "processed_access_seq": -1,
                    },
                )
                current["access_count"] += 1
                seq = int(entry["access_seq"])
                current["processed_access_seq"] = max(
                    int(current["processed_access_seq"]), seq
                )
                if bool(entry["oob"]):
                    current["oob_count"] += 1
                    access_end = int(entry["access_end"])
                    upper = current["oob_upper_bound"]
                    current["oob_upper_bound"] = (
                        access_end if upper is None else max(int(upper), access_end)
                    )
            replay_digest = canonical_digest(
                [replay[key] for key in sorted(replay)]
            )
            if replay_digest != row["ledger_digest"]:
                raise GuardError(
                    E_LEDGER_REPLAY_MISMATCH,
                    "replayed ledger does not match the batch checkpoint",
                    {"batch_id": row["batch_id"]},
                )

        replayed_rows = [replay[key] for key in sorted(replay)]
        actual_rows = self._rows()
        if canonical_digest(replayed_rows) != canonical_digest(actual_rows):
            raise GuardError(
                E_LEDGER_REPLAY_MISMATCH,
                "replayed complete batches do not match the current ledger",
            )
        if canonical_digest(actual_rows) != checkpoint["ledger_digest"]:
            raise GuardError(
                E_LEDGER_REPLAY_MISMATCH,
                "current ledger does not match the last complete checkpoint",
            )

        db_version = int(checkpoint["checkpoint_version"])
        if self.snapshot_path.exists() and not snapshot_invalid:
            if self.snapshot_version > db_version:
                recovery.snapshot_status = "newer"
                recovery.warnings.append(
                    Rejection(
                        source_file=str(self.snapshot_path),
                        line_number=None,
                        business_code=E_SNAPSHOT_NEWER,
                        message="snapshot is newer than the SQLite checkpoint; it was not trusted",
                        detail={
                            "snapshot_version": self.snapshot_version,
                            "database_version": db_version,
                        },
                    )
                )
                raise GuardError(
                    E_SNAPSHOT_NEWER,
                    "snapshot is newer than the SQLite checkpoint",
                    {
                        "snapshot_version": self.snapshot_version,
                        "database_version": db_version,
                    },
                )
            elif self.snapshot_version < db_version:
                recovery.snapshot_status = "stale"
                recovery.warnings.append(
                    Rejection(
                        source_file=str(self.snapshot_path),
                        line_number=None,
                        business_code=E_SNAPSHOT_STALE,
                        message="snapshot is behind the SQLite checkpoint and will be repaired",
                        detail={
                            "snapshot_version": self.snapshot_version,
                            "database_version": db_version,
                        },
                    )
                )
                self.snapshot_version = db_version
            else:
                if snapshot_document is None:
                    raise GuardError(
                        E_LEDGER_REPLAY_MISMATCH,
                        "snapshot content is unavailable for consistency checking",
                    )
                snapshot_payload = dict(snapshot_document)
                snapshot_payload_digest = snapshot_payload.pop("payload_digest", None)
                if snapshot_payload_digest != canonical_digest(snapshot_payload):
                    raise GuardError(
                        E_LEDGER_REPLAY_MISMATCH,
                        "snapshot payload digest does not verify",
                    )
                if snapshot_document.get("layout_key") != self.layout_key:
                    raise GuardError(
                        E_LEDGER_REPLAY_MISMATCH,
                        "snapshot layout does not match the SQLite layout",
                    )
                if (
                    snapshot_document.get("batch_id") != checkpoint["batch_id"]
                    or int(snapshot_document.get("watermark_seq", -1))
                    != int(checkpoint["watermark_seq"] if checkpoint["watermark_seq"] is not None else -1)
                    or snapshot_document.get("ledger_digest") != checkpoint["ledger_digest"]
                ):
                    raise GuardError(
                        E_LEDGER_REPLAY_MISMATCH,
                        "snapshot does not match the last complete SQLite checkpoint",
                    )
        elif snapshot_invalid:
            recovery.snapshot_status = "invalid"
            self.snapshot_version = db_version
        else:
            recovery.snapshot_status = "missing"
            recovery.warnings.append(
                Rejection(
                    source_file=str(self.snapshot_path),
                    line_number=None,
                    business_code=E_SNAPSHOT_STALE,
                    message="snapshot file is missing and will be rebuilt from SQLite",
                )
            )
            self.snapshot_version = db_version

        recovery.last_batch_id = payload.get("batch_id")
        recovery.watermark_seq = int(payload.get("watermark_seq", -1))
        if recovery.snapshot_status in {"stale", "initialized", "missing", "invalid"}:
            repaired = self._checkpoint_payload(
                self.snapshot_version,
                payload.get("batch_id"),
                int(payload.get("watermark_seq", -1)),
            )
            expected = (
                file_snapshot_version
                if recovery.snapshot_status == "stale" and not snapshot_invalid
                else None
            )
            self._write_snapshot(repaired, expected_version=expected)
            recovery.snapshot_status = "repaired"

    def _write_snapshot(
        self,
        payload: Mapping[str, Any],
        *,
        expected_version: Optional[int],
    ) -> None:
        atomic_write_json(
            self.snapshot_path,
            payload,
            expected_snapshot_version=expected_version,
            require_parse=True,
        )

    def batch_processed(self, batch_id: str, delta_digest: str) -> bool:
        row = self.db.execute(
            "SELECT delta_digest FROM batches WHERE batch_id = ? AND complete = 1",
            (batch_id,),
        ).fetchone()
        if row is None:
            return False
        if str(row["delta_digest"]) != delta_digest:
            raise GuardError(
                E_CHECKPOINT_INVALID,
                "existing batch id has a different delta",
                {"batch_id": batch_id},
            )
        return True

    def apply_batch(
        self,
        batch: LedgerBatch,
        *,
        after_commit_before_snapshot: Optional[Callable[[LedgerBatch], None]] = None,
    ) -> bool:
        delta = [
            {
                "region_id": entry.region_id,
                "access_kind": entry.access_kind,
                "access_seq": entry.access_seq,
                "oob": entry.oob,
                "access_end": entry.access_end,
            }
            for entry in batch.entries
        ]
        delta_text = canonical_json_bytes(delta).decode("utf-8")
        delta_digest = canonical_digest(delta)
        if self.batch_processed(batch.batch_id, delta_digest):
            return False

        for entry in batch.entries:
            if entry.access_kind not in {"read", "write"}:
                raise GuardError(E_CHECKPOINT_INVALID, "invalid access_kind in batch")
            if entry.access_seq < 0 or entry.access_end < 0:
                raise GuardError(E_CHECKPOINT_INVALID, "negative sequence or access end")

        self._begin()
        try:
            exists = self.db.execute(
                "SELECT delta_digest FROM batches WHERE batch_id = ?",
                (batch.batch_id,),
            ).fetchone()
            if exists is not None:
                if str(exists["delta_digest"]) != delta_digest:
                    raise GuardError(
                        E_CHECKPOINT_INVALID,
                        "existing batch id has a different delta",
                        {"batch_id": batch.batch_id},
                    )
                self.db.execute("ROLLBACK")
                return False

            for entry in batch.entries:
                row = self.db.execute(
                    "SELECT access_count, oob_count, oob_upper_bound, processed_access_seq "
                    "FROM region_ledger WHERE region_id = ? AND access_kind = ?",
                    (entry.region_id, entry.access_kind),
                ).fetchone()
                if row is None:
                    access_count = 1
                    oob_count = 1 if entry.oob else 0
                    upper = entry.access_end if entry.oob else None
                    processed = entry.access_seq
                else:
                    access_count = int(row["access_count"]) + 1
                    oob_count = int(row["oob_count"]) + (1 if entry.oob else 0)
                    upper = (
                        None if row["oob_upper_bound"] is None else int(row["oob_upper_bound"])
                    )
                    if entry.oob:
                        upper = entry.access_end if upper is None else max(upper, entry.access_end)
                    processed = max(int(row["processed_access_seq"]), entry.access_seq)
                self.db.execute(
                    "INSERT INTO region_ledger("
                    "region_id, access_kind, access_count, oob_count, "
                    "oob_upper_bound, processed_access_seq"
                    ") VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(region_id, access_kind) DO UPDATE SET "
                    "access_count=excluded.access_count, "
                    "oob_count=excluded.oob_count, "
                    "oob_upper_bound=excluded.oob_upper_bound, "
                    "processed_access_seq=excluded.processed_access_seq",
                    (
                        entry.region_id,
                        entry.access_kind,
                        access_count,
                        oob_count,
                        None if upper is None else str(upper),
                        str(processed),
                    ),
                )

            ledger_digest = self._ledger_digest()
            self.db.execute(
                "INSERT INTO batches("
                "batch_id, layout_key, first_seq, last_seq, record_count, "
                "delta_json, delta_digest, ledger_digest, applied_at_ms, complete"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    batch.batch_id,
                    self.layout_key or "",
                    str(batch.first_seq),
                    str(batch.last_seq),
                    batch.record_count,
                    delta_text,
                    delta_digest,
                    ledger_digest,
                    int(time.time() * 1000),
                ),
            )
            next_version = self.snapshot_version + 1
            payload = self._checkpoint_payload(
                next_version, batch.batch_id, batch.last_seq
            )
            self._write_checkpoint_row(payload)
            self._set_meta("last_batch_id", batch.batch_id)
            self._set_meta("watermark_seq", str(batch.last_seq))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

        if after_commit_before_snapshot is not None:
            after_commit_before_snapshot(batch)

        previous_version = self.snapshot_version
        self.snapshot_version = next_version
        try:
            self._write_snapshot(payload, expected_version=previous_version)
        except Exception:
            self.snapshot_version = next_version
            raise
        return True

    def rows(self) -> list[dict[str, Any]]:
        return self._rows()

    def checkpoint_metadata(self) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM checkpoint WHERE id = 1").fetchone()
        if row is None:
            return {}
        return {
            "checkpoint_version": int(row["checkpoint_version"]),
            "batch_id": row["batch_id"],
            "watermark_seq": -1 if row["watermark_seq"] is None else int(row["watermark_seq"]),
            "ledger_digest": str(row["ledger_digest"]),
        }

    def committed_batch_ids(self) -> set[str]:
        return {
            str(row["batch_id"])
            for row in self.db.execute("SELECT batch_id FROM batches WHERE complete = 1")
        }
