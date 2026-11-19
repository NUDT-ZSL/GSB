"""SQLite persistence, snapshot replacement, recovery, and verification."""

from __future__ import annotations

import json
import os
import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .errors import DiagnosticsError
from .lag import compute_lag
from .log_parser import parse_logs
from .merge import merge_records, selected_migrations
from .models import LogRecord, PositionRecord, SourceBundle
from .pipeline import (
    bundle_digest,
    validate_logs,
)
from .util import ensure_parent, sha256_bytes, stable_json_bytes, stable_json_text

SCHEMA_VERSION = 1


def _json_load(text: str) -> Any:
    return json.loads(text)


def _record_from_payload(payload: dict[str, Any]) -> PositionRecord:
    return PositionRecord.from_json(payload)


def _log_from_payload(payload: dict[str, Any]) -> LogRecord:
    return LogRecord.from_json(payload)


def _semantic_record(record: PositionRecord) -> dict[str, Any]:
    payload = record.to_json()
    payload.pop("location", None)
    return payload


def _semantic_log(record: LogRecord) -> dict[str, Any]:
    payload = record.to_json()
    payload.pop("location", None)
    return payload


def _version_tuple(value: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    for component in value.replace("-", ".").split("."):
        if component.isdigit():
            parts.append((0, int(component)))
        else:
            parts.append((1, component))
    return tuple(parts)


def _set_operation_state(
    connection: sqlite3.Connection,
    op_id: str,
    state: str,
    snapshot_id: int | None = None,
) -> None:
    connection.execute(
        "UPDATE operations SET state=?, snapshot_id=? WHERE op_id=?",
        (state, snapshot_id, op_id),
    )


class State:
    """Owns one SQLite connection and all snapshot mutations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        ensure_parent(self.path)
        try:
            self.connection = sqlite3.connect(str(self.path), timeout=30.0)
        except sqlite3.Error as exc:
            raise DiagnosticsError(
                "E_STATE",
                message=f"cannot open state database {self.path}",
                location=str(self.path),
                details={"reason": str(exc)},
            ) from exc
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operations (
            row_number INTEGER PRIMARY KEY AUTOINCREMENT,
            op_id TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            state TEXT NOT NULL,
            classification TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            snapshot_id INTEGER,
            manifest_json TEXT
        );
        CREATE TABLE IF NOT EXISTS raw_records (
            op_id TEXT NOT NULL,
            source_version TEXT NOT NULL,
            tx_num INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (op_id, source_version, tx_num),
            FOREIGN KEY (op_id) REFERENCES operations(op_id)
        );
        CREATE TABLE IF NOT EXISTS raw_logs (
            op_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (op_id, seq),
            FOREIGN KEY (op_id) REFERENCES operations(op_id)
        );
        CREATE TABLE IF NOT EXISTS staging_entries (
            op_id TEXT NOT NULL,
            tx_num INTEGER NOT NULL,
            tx_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (op_id, tx_num),
            UNIQUE (op_id, tx_id),
            FOREIGN KEY (op_id) REFERENCES operations(op_id)
        );
        CREATE TABLE IF NOT EXISTS staging_package (
            op_id TEXT PRIMARY KEY,
            classification TEXT NOT NULL,
            package_version TEXT NOT NULL,
            logical_clock_floor INTEGER NOT NULL,
            tx_min TEXT NOT NULL,
            tx_max TEXT NOT NULL,
            manifest_digest TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            FOREIGN KEY (op_id) REFERENCES operations(op_id)
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id INTEGER PRIMARY KEY,
            snapshot_key TEXT NOT NULL UNIQUE,
            package_version TEXT NOT NULL,
            logical_clock_floor INTEGER NOT NULL,
            tx_min TEXT NOT NULL,
            tx_max TEXT NOT NULL,
            manifest_digest TEXT NOT NULL,
            classification TEXT NOT NULL,
            committed_watermark TEXT,
            replay_watermark TEXT,
            config_path TEXT NOT NULL,
            config_json TEXT NOT NULL,
            lag_json TEXT NOT NULL,
            output_digest TEXT NOT NULL,
            migration_count INTEGER NOT NULL,
            tombstone_count INTEGER NOT NULL,
            window_count INTEGER NOT NULL,
            entry_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshot_entries (
            snapshot_id INTEGER NOT NULL,
            tx_num INTEGER NOT NULL,
            tx_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, tx_num),
            UNIQUE (snapshot_id, tx_id),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS snapshot_logs (
            snapshot_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, seq),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS snapshot_migrations (
            snapshot_id INTEGER NOT NULL,
            tx_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, tx_id),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS snapshot_windows (
            snapshot_id INTEGER NOT NULL,
            window_index INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, window_index),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS snapshot_diagnostics (
            snapshot_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, kind, ordinal),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS active_snapshot (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            snapshot_id INTEGER,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
        );
        """
        try:
            self.connection.executescript(schema)
            self.connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO active_snapshot(singleton, snapshot_id) VALUES(1, NULL)"
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise DiagnosticsError(
                "E_STATE",
                message="cannot initialize state schema",
                location=str(self.path),
                details={"reason": str(exc)},
            ) from exc

    # ------------------------------------------------------------------
    # transaction and query helpers
    # ------------------------------------------------------------------

    def _begin(self) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise DiagnosticsError(
                "E_STATE", message="cannot begin state transaction", details={"reason": str(exc)}
            ) from exc

    def _commit(self) -> None:
        try:
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise DiagnosticsError(
                "E_STATE", message="cannot commit state transaction", details={"reason": str(exc)}
            ) from exc

    def _rollback(self) -> None:
        try:
            self.connection.rollback()
        except sqlite3.Error:
            pass

    def active_snapshot_id(self) -> int | None:
        row = self.connection.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton=1"
        ).fetchone()
        return None if row is None else row["snapshot_id"]

    def active_row(self) -> sqlite3.Row | None:
        row = self.connection.execute(
            """
            SELECT s.*
            FROM active_snapshot a
            JOIN snapshots s ON s.snapshot_id=a.snapshot_id
            WHERE a.singleton=1
            """
        ).fetchone()
        return row

    def snapshot_row(self, snapshot_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise DiagnosticsError(
                "E_STATE", message="snapshot is missing", details={"snapshot_id": snapshot_id}
            )
        return row

    def _next_snapshot_id(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(snapshot_id), 0) + 1 AS next_id FROM snapshots"
        ).fetchone()
        return int(row["next_id"])

    def _operation(self, op_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM operations WHERE op_id=?", (op_id,)
        ).fetchone()

    # ------------------------------------------------------------------
    # staging
    # ------------------------------------------------------------------

    def stage_source(self, bundle: SourceBundle, config: Config) -> dict[str, Any]:
        digest = bundle_digest(bundle, config)
        op_id = "source:" + digest
        try:
            self._begin()
            existing = self._operation(op_id)
            payload = {
                "config_path": str(config.path),
                "config": config.as_dict(),
                "manifest": bundle.manifest,
                "v1_digest": bundle.v1_digest,
                "v2_digest": bundle.v2_digest,
                "logs_digest": bundle.logs_digest,
            }
            if existing is not None and existing["state"] == "committed":
                self._commit()
                return {
                    "ok": True,
                    "status": "noop",
                    "operation_id": op_id,
                    "snapshot_version": existing["snapshot_id"],
                    "reason": "identical_source_already_committed",
                }
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO operations(op_id, kind, input_digest, state, classification, payload_json, manifest_json)
                    VALUES(?, 'source', ?, 'staged', 'source_merge', ?, ?)
                    """,
                    (
                        op_id,
                        digest,
                        stable_json_text(payload),
                        None if bundle.manifest is None else stable_json_text(bundle.manifest),
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE operations
                    SET state='staged', classification='source_merge', payload_json=?, manifest_json=?, snapshot_id=NULL
                    WHERE op_id=?
                    """,
                    (
                        stable_json_text(payload),
                        None if bundle.manifest is None else stable_json_text(bundle.manifest),
                        op_id,
                    ),
                )
                self.connection.execute("DELETE FROM raw_records WHERE op_id=?", (op_id,))
                self.connection.execute("DELETE FROM raw_logs WHERE op_id=?", (op_id,))

            for record in list(bundle.v1_records) + list(bundle.v2_records):
                self.connection.execute(
                    "INSERT INTO raw_records(op_id, source_version, tx_num, payload_json) VALUES(?,?,?,?)",
                    (op_id, record.source_version, record.tx_num, stable_json_text(record.to_json())),
                )
            for log in bundle.log_records:
                self.connection.execute(
                    "INSERT INTO raw_logs(op_id, seq, payload_json) VALUES(?,?,?)",
                    (op_id, log.seq, stable_json_text(log.to_json())),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return {
            "ok": True,
            "status": "staged",
            "operation_id": op_id,
            "classification": "source_merge",
            "record_count": len(bundle.v1_records) + len(bundle.v2_records),
            "log_count": len(bundle.log_records),
            "input_digest": digest,
        }

    def stage_package(
        self,
        manifest_path: Path,
        entries: list[PositionRecord],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        semantic_entries = [_semantic_record(record) for record in entries]
        digest = sha256_bytes(
            stable_json_bytes({"manifest": manifest, "entries": semantic_entries})
        )
        op_id = "package:" + digest
        try:
            self._begin()
            existing = self._operation(op_id)
            if existing is not None and existing["state"] == "committed":
                self._commit()
                return {
                    "ok": True,
                    "status": "noop",
                    "operation_id": op_id,
                    "classification": existing["classification"],
                    "snapshot_version": existing["snapshot_id"],
                }

            active = self.active_row()
            package_version = manifest["package_version"]
            tx_min = manifest["tx_min"]
            tx_max = manifest["tx_max"]
            tx_min_num = int(tx_min)
            tx_max_num = int(tx_max)
            manifest_digest = sha256_bytes(stable_json_bytes(manifest))
            package_type = manifest.get("package_type", "source")

            if active is not None:
                active_watermark = active["committed_watermark"]
                if active_watermark is not None and tx_max_num < int(active_watermark):
                    raise DiagnosticsError(
                        "E_POSITION_ROLLBACK",
                        location=str(manifest_path),
                        details={
                            "candidate_tx_max": tx_max,
                            "committed_watermark": active_watermark,
                        },
                    )
                same_manifest = active["manifest_digest"] == manifest_digest
                same_bounds = (
                    active["package_version"] == package_version
                    and active["logical_clock_floor"] == manifest["logical_clock_floor"]
                    and active["tx_min"] == tx_min
                    and active["tx_max"] == tx_max
                )
                if same_manifest and same_bounds:
                    payload = {
                        "config_path": str(manifest_path),
                        "manifest": manifest,
                        "package_path": str(manifest_path),
                    }
                    self.connection.execute(
                        """
                        INSERT INTO operations(op_id, kind, input_digest, state, classification, payload_json, snapshot_id, manifest_json)
                        VALUES(?, 'package', ?, 'committed', 'noop', ?, ?, ?)
                        """,
                        (
                            op_id,
                            digest,
                            stable_json_text(payload),
                            active["snapshot_id"],
                            stable_json_text(manifest),
                        ),
                    )
                    self._commit()
                    return {
                        "ok": True,
                        "status": "noop",
                        "operation_id": op_id,
                        "classification": "noop",
                        "snapshot_version": active["snapshot_id"],
                    }
                if _version_tuple(package_version) < _version_tuple(active["package_version"]):
                    raise DiagnosticsError(
                        "E_VERSION_ROLLBACK",
                        location=str(manifest_path),
                        details={
                            "candidate_version": package_version,
                            "active_version": active["package_version"],
                        },
                    )

            if package_type == "incremental":
                if active is None:
                    raise DiagnosticsError(
                        "E_PACKAGE_INCOMPLETE",
                        message="incremental package requires an active snapshot",
                        location=str(manifest_path),
                    )
                required_min = int(active["tx_max"]) + 1
                if tx_min_num != required_min:
                    raise DiagnosticsError(
                        "E_PACKAGE_OVERLAP",
                        location=str(manifest_path),
                        details={"required_tx_min": f"{required_min:020d}", "actual_tx_min": tx_min},
                    )
                classification = "incremental_append"
            elif package_type == "full":
                classification = "full_replace"
            else:
                classification = "full_replace"

            merge_records([], entries, load_config_for_staging(manifest_path, self))
            if existing is None:
                payload = {
                    "config_path": str(manifest_path),
                    "manifest": manifest,
                    "package_path": str(manifest_path),
                }
                self.connection.execute(
                    """
                    INSERT INTO operations(op_id, kind, input_digest, state, classification, payload_json, manifest_json)
                    VALUES(?, 'package', ?, 'staged', ?, ?, ?)
                    """,
                    (
                        op_id,
                        digest,
                        classification,
                        stable_json_text(payload),
                        stable_json_text(manifest),
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE operations
                    SET state='staged', classification=?, payload_json=?, manifest_json=?, snapshot_id=NULL
                    WHERE op_id=?
                    """,
                    (
                        classification,
                        stable_json_text(
                            {
                                "config_path": str(manifest_path),
                                "manifest": manifest,
                                "package_path": str(manifest_path),
                            }
                        ),
                        stable_json_text(manifest),
                        op_id,
                    ),
                )
                self.connection.execute("DELETE FROM staging_entries WHERE op_id=?", (op_id,))
                self.connection.execute("DELETE FROM staging_package WHERE op_id=?", (op_id,))
            self.connection.execute(
                """
                INSERT INTO staging_package(
                    op_id, classification, package_version, logical_clock_floor,
                    tx_min, tx_max, manifest_digest, manifest_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    op_id,
                    classification,
                    package_version,
                    manifest["logical_clock_floor"],
                    tx_min,
                    tx_max,
                    manifest_digest,
                    stable_json_text(manifest),
                ),
            )
            for record in entries:
                self.connection.execute(
                    "INSERT INTO staging_entries(op_id, tx_num, tx_id, payload_json) VALUES(?,?,?,?)",
                    (op_id, record.tx_num, record.tx_id, stable_json_text(record.to_json())),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return {
            "ok": True,
            "status": "staged",
            "operation_id": op_id,
            "classification": classification,
            "package_version": package_version,
            "tx_min": tx_min,
            "tx_max": tx_max,
            "entry_count": len(entries),
            "input_digest": digest,
        }

    # ------------------------------------------------------------------
    # finalization and recovery
    # ------------------------------------------------------------------

    def merge_pending(self) -> dict[str, Any]:
        try:
            self._begin()
            operation = self.connection.execute(
                "SELECT * FROM operations WHERE state='staged' ORDER BY row_number LIMIT 1"
            ).fetchone()
            if operation is None:
                active = self.active_row()
                self._commit()
                if active is None:
                    return {"ok": True, "status": "empty", "reason": "no_staged_operation"}
                return {
                    "ok": True,
                    "status": "noop",
                    "snapshot_version": active["snapshot_id"],
                    "reason": "no_staged_operation",
                }
            result = self._finalize_operation(operation)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def resume(self) -> dict[str, Any]:
        resumed: list[dict[str, Any]] = []
        while True:
            try:
                self._begin()
                operation = self.connection.execute(
                    "SELECT * FROM operations WHERE state='staged' ORDER BY row_number LIMIT 1"
                ).fetchone()
                if operation is None:
                    active = self.active_row()
                    self._commit()
                    break
                result = self._finalize_operation(operation)
                self._commit()
                resumed.append(result)
            except Exception:
                self._rollback()
                raise
        return {
            "ok": True,
            "status": "resumed" if resumed else "noop",
            "resumed_operations": resumed,
            "snapshot_version": None if self.active_row() is None else self.active_row()["snapshot_id"],
        }

    def _finalize_operation(self, operation: sqlite3.Row) -> dict[str, Any]:
        op_id = operation["op_id"]
        payload = _json_load(operation["payload_json"])
        classification = operation["classification"]
        active = self.active_row()

        if operation["kind"] == "source":
            config_path = payload.get("config_path")
            if not config_path:
                raise DiagnosticsError("E_RECOVERY", details={"operation_id": op_id})
            config = load_config(config_path)
            if config.as_dict() != payload.get("config"):
                raise DiagnosticsError(
                    "E_RECOVERY",
                    message="configuration changed after staging",
                    details={"operation_id": op_id},
                )
            raw_records = self.connection.execute(
                "SELECT * FROM raw_records WHERE op_id=? ORDER BY source_version, tx_num",
                (op_id,),
            ).fetchall()
            raw_logs = self.connection.execute(
                "SELECT * FROM raw_logs WHERE op_id=? ORDER BY seq", (op_id,)
            ).fetchall()
            if not raw_records or not raw_logs:
                raise DiagnosticsError(
                    "E_RECOVERY",
                    message="staged source data is incomplete",
                    details={"operation_id": op_id},
                )
            v1_records = [
                _record_from_payload(_json_load(row["payload_json"]))
                for row in raw_records
                if row["source_version"] == "v1"
            ]
            v2_records = [
                _record_from_payload(_json_load(row["payload_json"]))
                for row in raw_records
                if row["source_version"] == "v2"
            ]
            logs = [_log_from_payload(_json_load(row["payload_json"])) for row in raw_logs]
            merged = merge_records(v1_records, v2_records, config)
            validate_logs(merged, logs)
            package_version = (
                payload["manifest"]["package_version"]
                if payload.get("manifest")
                else "1.0.0-v1"
            )
            logical_clock_floor = (
                payload["manifest"]["logical_clock_floor"]
                if payload.get("manifest")
                else config.v1_base_clock_ms
            )
            manifest_digest = (
                sha256_bytes(stable_json_bytes(payload["manifest"]))
                if payload.get("manifest")
                else sha256_bytes(b"v1-only")
            )
            snapshot_key = op_id
        elif operation["kind"] == "package":
            package_row = self.connection.execute(
                "SELECT * FROM staging_package WHERE op_id=?", (op_id,)
            ).fetchone()
            if package_row is None:
                raise DiagnosticsError(
                    "E_RECOVERY",
                    message="package staging metadata is missing",
                    details={"operation_id": op_id},
                )
            staged_rows = self.connection.execute(
                "SELECT * FROM staging_entries WHERE op_id=? ORDER BY tx_num", (op_id,)
            ).fetchall()
            if not staged_rows:
                raise DiagnosticsError(
                    "E_RECOVERY",
                    message="package staging entries are missing",
                    details={"operation_id": op_id},
                )
            package_entries = [
                _record_from_payload(_json_load(row["payload_json"])) for row in staged_rows
            ]
            manifest = _json_load(package_row["manifest_json"])
            if active is not None:
                if _version_tuple(package_row["package_version"]) < _version_tuple(
                    active["package_version"]
                ):
                    raise DiagnosticsError(
                        "E_VERSION_ROLLBACK",
                        details={
                            "candidate_version": package_row["package_version"],
                            "active_version": active["package_version"],
                            "phase": "finalize",
                        },
                    )
                if classification == "incremental_append":
                    required_min = int(active["tx_max"]) + 1
                    if int(package_row["tx_min"]) != required_min:
                        raise DiagnosticsError(
                            "E_PACKAGE_OVERLAP",
                            details={
                                "required_tx_min": f"{required_min:020d}",
                                "actual_tx_min": package_row["tx_min"],
                                "phase": "finalize",
                            },
                        )
            # A package manifest directory has no standalone diagnostic config;
            # the active snapshot's config controls delay observation settings.
            if active is None:
                raise DiagnosticsError(
                    "E_RECOVERY",
                    message="package finalization requires an active configuration",
                    details={"operation_id": op_id},
                )
            config = load_config(active["config_path"])
            if active["config_json"] != stable_json_text(config.as_dict()):
                raise DiagnosticsError(
                    "E_RECOVERY",
                    message="active configuration changed after staging",
                    details={"operation_id": op_id},
                )
            merge_records([], package_entries, config)
            if classification == "full_replace":
                merged = package_entries
            else:
                current_entries = self._snapshot_entries(active["snapshot_id"])
                merged = merge_records([], current_entries + package_entries, config)
            logs = self._snapshot_logs(active["snapshot_id"])
            validate_logs(merged, logs)
            package_version = package_row["package_version"]
            logical_clock_floor = package_row["logical_clock_floor"]
            manifest_digest = package_row["manifest_digest"]
            snapshot_key = op_id
        else:
            raise DiagnosticsError(
                "E_STATE", message="unknown operation kind", details={"operation_id": op_id}
            )

        if active is not None and active["committed_watermark"] is not None:
            committed = [record for record in merged if record.committed]
            candidate_watermark = committed[-1].tx_num if committed else 0
            if candidate_watermark < int(active["committed_watermark"]):
                raise DiagnosticsError(
                    "E_POSITION_ROLLBACK",
                    details={
                        "candidate_committed_watermark": f"{candidate_watermark:020d}",
                        "active_committed_watermark": active["committed_watermark"],
                    },
                )

        snapshot_id = self._next_snapshot_id()
        output = compute_lag(
            merged,
            logs,
            config,
            snapshot_version=snapshot_id,
            package_version=package_version,
            logical_clock_floor=logical_clock_floor,
        )
        if os.environ.get("RD_CRASH_BEFORE_COMMIT") == "1":
            os._exit(137)

        output_text = stable_json_text(output)
        output_digest = sha256_bytes(output_text.encode("utf-8"))
        tx_min = merged[0].tx_id
        tx_max = merged[-1].tx_id
        committed_watermark = output["committed_watermark"]
        replay_watermark = output["replay_watermark"]

        existing_snapshot = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_key=?", (snapshot_key,)
        ).fetchone()
        if existing_snapshot is not None:
            _set_operation_state(
                self.connection, op_id, "committed", existing_snapshot["snapshot_id"]
            )
            self.connection.execute("DELETE FROM staging_entries WHERE op_id=?", (op_id,))
            return {
                "ok": True,
                "status": "committed",
                "operation_id": op_id,
                "snapshot_version": existing_snapshot["snapshot_id"],
                "classification": classification,
                "output_digest": existing_snapshot["output_digest"],
                "reused_snapshot": True,
            }

        self.connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, snapshot_key, package_version, logical_clock_floor,
                tx_min, tx_max, manifest_digest, classification,
                committed_watermark, replay_watermark, config_path, config_json,
                lag_json, output_digest, migration_count, tombstone_count,
                window_count, entry_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                snapshot_key,
                package_version,
                logical_clock_floor,
                tx_min,
                tx_max,
                manifest_digest,
                classification,
                committed_watermark,
                replay_watermark,
                str(config.path),
                stable_json_text(config.as_dict()),
                output_text,
                output_digest,
                output["counts"]["migration_decision_count"],
                output["counts"]["tombstone_transaction_count"],
                output["counts"]["window_count"],
                len(merged),
            ),
        )
        for record in merged:
            self.connection.execute(
                "INSERT INTO snapshot_entries(snapshot_id, tx_num, tx_id, payload_json) VALUES(?,?,?,?)",
                (snapshot_id, record.tx_num, record.tx_id, stable_json_text(record.to_json())),
            )
        for log in logs:
            self.connection.execute(
                "INSERT INTO snapshot_logs(snapshot_id, seq, payload_json) VALUES(?,?,?)",
                (snapshot_id, log.seq, stable_json_text(log.to_json())),
            )
        migrations = selected_migrations(merged)
        for decision in migrations:
            self.connection.execute(
                "INSERT INTO snapshot_migrations(snapshot_id, tx_id, payload_json) VALUES(?,?,?)",
                (snapshot_id, decision["tx_id"], stable_json_text(decision)),
            )
        for window in output["windows"]:
            self.connection.execute(
                "INSERT INTO snapshot_windows(snapshot_id, window_index, payload_json) VALUES(?,?,?)",
                (snapshot_id, window["window_index"], stable_json_text(window)),
            )
        diagnostics: list[tuple[str, int, Any]] = []
        for index, comparison in enumerate(output["precision"]["transaction_comparisons"]):
            diagnostics.append(("transaction_comparison", index, comparison))
        for index, comparison in enumerate(output["precision"]["window_comparisons"]):
            diagnostics.append(("window_comparison", index, comparison))
        for index, transaction in enumerate(output["transactions"]):
            diagnostics.append(
                (
                    "transaction_error",
                    index,
                    {
                        "tx_id": transaction["tx_id"],
                        "error_upper_bound_exact": transaction["error_upper_bound_exact"],
                        "error_sources": transaction["error_sources"],
                    },
                )
            )
        for index, window in enumerate(output["windows"]):
            diagnostics.append(
                (
                    "window_error",
                    index,
                    {
                        "window_index": window["window_index"],
                        "error_upper_bound_exact": window["error_upper_bound_exact"],
                        "error_sources": window["error_sources"],
                    },
                )
            )
        for kind, ordinal, diagnostic in diagnostics:
            self.connection.execute(
                """
                INSERT INTO snapshot_diagnostics(snapshot_id, kind, ordinal, payload_json)
                VALUES(?,?,?,?)
                """,
                (snapshot_id, kind, ordinal, stable_json_text(diagnostic)),
            )

        self.connection.execute(
            "UPDATE active_snapshot SET snapshot_id=? WHERE singleton=1", (snapshot_id,)
        )
        _set_operation_state(self.connection, op_id, "committed", snapshot_id)
        self.connection.execute("DELETE FROM staging_entries WHERE op_id=?", (op_id,))
        self.connection.execute("DELETE FROM staging_package WHERE op_id=?", (op_id,))
        return {
            "ok": True,
            "status": "committed",
            "operation_id": op_id,
            "snapshot_version": snapshot_id,
            "classification": classification,
            "output_digest": output_digest,
            "reused_snapshot": False,
        }

    # ------------------------------------------------------------------
    # reads and verification
    # ------------------------------------------------------------------

    def _snapshot_entries(self, snapshot_id: int) -> list[PositionRecord]:
        rows = self.connection.execute(
            "SELECT payload_json FROM snapshot_entries WHERE snapshot_id=? ORDER BY tx_num",
            (snapshot_id,),
        ).fetchall()
        return [_record_from_payload(_json_load(row["payload_json"])) for row in rows]

    def _snapshot_logs(self, snapshot_id: int) -> list[LogRecord]:
        rows = self.connection.execute(
            "SELECT payload_json FROM snapshot_logs WHERE snapshot_id=? ORDER BY seq",
            (snapshot_id,),
        ).fetchall()
        return [_log_from_payload(_json_load(row["payload_json"])) for row in rows]

    def lag_output(self) -> dict[str, Any]:
        row = self.active_row()
        if row is None:
            raise DiagnosticsError("E_STATE", message="no active snapshot")
        return _json_load(row["lag_json"])

    def status(self) -> dict[str, Any]:
        active = self.active_row()
        pending = self.connection.execute(
            "SELECT op_id, kind, classification FROM operations WHERE state='staged' ORDER BY row_number"
        ).fetchall()
        if active is None:
            return {
                "ok": True,
                "status": "empty",
                "active_snapshot": None,
                "pending_operations": [dict(row) for row in pending],
            }
        return {
            "ok": True,
            "status": "ready",
            "active_snapshot": {
                "snapshot_version": active["snapshot_id"],
                "package_version": active["package_version"],
                "logical_clock_floor": active["logical_clock_floor"],
                "tx_min": active["tx_min"],
                "tx_max": active["tx_max"],
                "committed_watermark": active["committed_watermark"],
                "replay_watermark": active["replay_watermark"],
                "classification": active["classification"],
                "migration_decision_count": active["migration_count"],
                "tombstone_transaction_count": active["tombstone_count"],
                "window_count": active["window_count"],
                "entry_count": active["entry_count"],
                "output_digest": active["output_digest"],
            },
            "pending_operations": [dict(row) for row in pending],
        }

    def verify(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        active = self.active_row()
        if active is None:
            return {
                "ok": False,
                "status": "failed",
                "checks": [
                    {"name": "active_snapshot_present", "ok": False, "details": "no active snapshot"}
                ],
            }
        checks.append({"name": "active_snapshot_present", "ok": True})

        entries = self._snapshot_entries(active["snapshot_id"])
        checks.append(
            {
                "name": "snapshot_entry_count",
                "ok": len(entries) == active["entry_count"],
                "expected": active["entry_count"],
                "actual": len(entries),
            }
        )
        sorted_entries = sorted(entries, key=lambda item: item.tx_num)
        contiguous = all(
            right.tx_num == left.tx_num + 1
            for left, right in zip(sorted_entries, sorted_entries[1:])
        )
        checks.append(
            {
                "name": "snapshot_entries_contiguous",
                "ok": contiguous,
                "count": len(entries),
            }
        )
        logs = self._snapshot_logs(active["snapshot_id"])
        checks.append(
            {
                "name": "replay_logs_present",
                "ok": bool(logs),
                "count": len(logs),
            }
        )
        config = load_config(active["config_path"])
        config_same = stable_json_text(config.as_dict()) == active["config_json"]
        checks.append({"name": "configuration_unchanged", "ok": config_same})

        try:
            recomputed = compute_lag(
                entries,
                logs,
                config,
                snapshot_version=active["snapshot_id"],
                package_version=active["package_version"],
                logical_clock_floor=active["logical_clock_floor"],
            )
            checks.append({"name": "lag_recompute_succeeded", "ok": True})
        except DiagnosticsError as exc:
            recomputed = None
            checks.append(
                {
                    "name": "lag_recompute_succeeded",
                    "ok": False,
                    "error_code": exc.code,
                    "message": exc.message,
                }
            )
        if recomputed is not None:
            expected_text = stable_json_text(recomputed)
            persisted_output = _json_load(active["lag_json"])
            output_equal = stable_json_text(persisted_output) == expected_text
            persisted_output_digest = sha256_bytes(
                stable_json_text(persisted_output).encode("utf-8")
            )
            checks.append(
                {
                    "name": "lag_output_matches_recompute",
                    "ok": output_equal,
                    "expected_digest": sha256_bytes(expected_text.encode("utf-8")),
                    "persisted_digest": active["output_digest"],
                }
            )
            checks.append(
                {
                    "name": "output_digest_matches_persisted_output",
                    "ok": persisted_output_digest == active["output_digest"],
                    "expected": active["output_digest"],
                    "actual": persisted_output_digest,
                }
            )

            persisted_windows = [
                _json_load(row["payload_json"])
                for row in self.connection.execute(
                    "SELECT payload_json FROM snapshot_windows WHERE snapshot_id=? ORDER BY window_index",
                    (active["snapshot_id"],),
                ).fetchall()
            ]
            checks.append(
                {
                    "name": "window_summaries_match",
                    "ok": persisted_windows == recomputed["windows"],
                    "count": len(persisted_windows),
                }
            )
            migration_count = self.connection.execute(
                "SELECT COUNT(*) AS count FROM snapshot_migrations WHERE snapshot_id=?",
                (active["snapshot_id"],),
            ).fetchone()["count"]
            persisted_migrations = [
                _json_load(row["payload_json"])
                for row in self.connection.execute(
                    "SELECT payload_json FROM snapshot_migrations WHERE snapshot_id=? ORDER BY tx_id",
                    (active["snapshot_id"],),
                ).fetchall()
            ]
            checks.append(
                {
                    "name": "migration_decision_count",
                    "ok": migration_count == recomputed["counts"]["migration_decision_count"],
                    "expected": recomputed["counts"]["migration_decision_count"],
                    "actual": migration_count,
                }
            )
            checks.append(
                {
                    "name": "migration_decisions_match",
                    "ok": persisted_migrations == selected_migrations(entries),
                    "count": migration_count,
                }
            )
            tombstone_count = recomputed["counts"]["tombstone_transaction_count"]
            checks.append(
                {
                    "name": "tombstone_count",
                    "ok": tombstone_count == active["tombstone_count"],
                    "expected": active["tombstone_count"],
                    "actual": tombstone_count,
                }
            )
            diagnostics_count = self.connection.execute(
                "SELECT COUNT(*) AS count FROM snapshot_diagnostics WHERE snapshot_id=?",
                (active["snapshot_id"],),
            ).fetchone()["count"]
            expected_diagnostics = (
                len(recomputed["precision"]["transaction_comparisons"])
                + len(recomputed["precision"]["window_comparisons"])
                + len(recomputed["transactions"])
                + len(recomputed["windows"])
            )
            checks.append(
                {
                    "name": "error_bound_records_persisted",
                    "ok": diagnostics_count == expected_diagnostics,
                    "expected": expected_diagnostics,
                    "actual": diagnostics_count,
                }
            )

        all_ok = all(check["ok"] for check in checks)
        return {
            "ok": all_ok,
            "status": "verified" if all_ok else "failed",
            "snapshot_version": active["snapshot_id"],
            "output_digest": active["output_digest"],
            "checks": checks,
        }


def load_config_for_staging(manifest_path: Path, state: State) -> Config:
    """Package staging uses the active snapshot's delay configuration.

    A package manifest is not a diagnostic config, so package application is
    defined as using the currently active precision/clock configuration.  When
    no snapshot exists, packages are rejected before this helper is called.
    """

    active = state.active_row()
    if active is None:
        raise DiagnosticsError(
            "E_PACKAGE_INCOMPLETE",
            message="a package requires an active diagnostic configuration",
            location=str(manifest_path),
        )
    return load_config(active["config_path"])
