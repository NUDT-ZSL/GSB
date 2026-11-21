"""State database lifecycle, atomic event boundaries and recovery helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import DiagnosticError, E_NO_STATE, E_STATE_CORRUPT
from .model import SampleBundle
from .schema import SCHEMA_SQL
from .util import canonical_json, pretty_json


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


class StateStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "state.db"
        self.connection: sqlite3.Connection | None = None

    @classmethod
    def exists(cls, state_dir: str | Path) -> bool:
        return (Path(state_dir) / "state.db").exists()

    def open(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise DiagnosticError(E_NO_STATE, f"state database not found: {self.db_path}")
        if self.connection is None:
            self.connection = _connect(self.db_path)
            try:
                self.connection.execute("SELECT 1 FROM run_info LIMIT 1")
            except sqlite3.Error as exc:
                raise DiagnosticError(E_STATE_CORRUPT, f"state database is invalid: {exc}")
        return self.connection

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "StateStore":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def create(
        self,
        bundle: SampleBundle,
        run_db_path: Path,
        transactions: list[dict[str, Any]],
        operations: list[dict[str, Any]],
    ) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.connection = _connect(self.db_path)
        self.connection.executescript(SCHEMA_SQL)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                INSERT INTO run_info(
                    id, fingerprint, manifest_json, run_db_path, status, initialized_bundle
                ) VALUES (1, ?, ?, ?, 'READY', ?)
                """,
                (
                    bundle.fingerprint,
                    canonical_json(bundle.manifest_json),
                    str(run_db_path),
                    "initial",
                ),
            )
            for tx in transactions:
                self.connection.execute(
                    """
                    INSERT INTO transactions(
                        tx_id, start_order, version, isolation, budget_ms,
                        depends_json, status, current_op_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, 'NEW', 1)
                    """,
                    (
                        tx["tx_id"],
                        tx["start_order"],
                        tx["version"],
                        tx["isolation"],
                        tx["budget_ms"],
                        canonical_json(tx["depends_on"]),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO tx_accounts(tx_id) VALUES (?)", (tx["tx_id"],)
                )
            for op in operations:
                self.connection.execute(
                    """
                    INSERT INTO operations(
                        tx_id, op_seq, op_version, op_id, kind, payload_json,
                        resource_key, resource_type, table_name, row_id,
                        range_start, range_end, requested_mode, index_path,
                        status, plan_json, worker_pid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL)
                    """,
                    (
                        op["tx_id"],
                        op["op_seq"],
                        op["op_version"],
                        op["op_id"],
                        op["op"],
                        canonical_json(op),
                        op.get("resource_key"),
                        op.get("resource_type"),
                        op.get("table", ""),
                        op.get("row_id"),
                        op.get("range_start"),
                        op.get("range_end"),
                        op.get("requested_mode"),
                        op.get("index_path"),
                    ),
                )
            first_raw = bundle.clock.samples[0]
            self.connection.execute(
                """
                INSERT INTO clock_state(
                    id, next_sequence, next_raw, last_sample_id, last_raw, raw_units_per_ms
                ) VALUES (1, 0, ?, NULL, NULL, ?)
                """,
                (first_raw, bundle.clock.raw_units_per_ms),
            )
            self.connection.execute(
                """
                INSERT INTO schedule_state(id, rr_start_order, steps, status, next_queue_no)
                VALUES (1, 0, 0, 'READY', 1)
                """
            )
            self.connection.execute(
                "INSERT INTO commit_bundles VALUES (?, 'SYSTEM', 'INIT', NULL, NULL, ?)",
                ("init", canonical_json({"fingerprint": bundle.fingerprint})),
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.open()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @contextmanager
    def bundle(
        self,
        bundle_id: str,
        tx_id: str,
        stage: str,
        intent_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """Commit all event state, clock watermark and reconstruction data atomically."""

        connection = self.open()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            watermark = connection.execute(
                "SELECT last_sample_id, last_raw FROM clock_state WHERE id=1"
            ).fetchone()
            watermark_sample = watermark["last_sample_id"] if watermark else None
            watermark_raw = watermark["last_raw"] if watermark else None
            queue_summary = [
                {
                    "queue_no": item["queue_no"],
                    "wait_id": item["wait_id"],
                    "tx_id": item["tx_id"],
                    "resource_key": item["resource_key"],
                    "requested_mode": item["requested_mode"],
                    "status": item["status"],
                    "resolution": item["resolution"],
                }
                for item in connection.execute(
                    "SELECT * FROM wait_queue ORDER BY queue_no"
                )
            ]
            lock_summary = [
                {
                    "resource_key": item["resource_key"],
                    "table_name": item["table_name"],
                    "mode": item["mode"],
                    "tx_id": item["tx_id"],
                    "acquired_sample": item["acquired_sample"],
                }
                for item in connection.execute(
                    "SELECT * FROM lock_holders ORDER BY table_name, resource_key, tx_id"
                )
            ]
            atomic_payload = dict(payload or {})
            atomic_payload["queue_summary"] = queue_summary
            atomic_payload["lock_summary"] = lock_summary
            connection.execute(
                """
                INSERT OR IGNORE INTO commit_bundles(
                    bundle_id, tx_id, stage, clock_watermark_sample,
                    clock_watermark_raw, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle_id,
                    tx_id,
                    stage,
                    watermark_sample,
                    watermark_raw,
                    canonical_json(atomic_payload),
                ),
            )
            if intent_id is not None:
                connection.execute(
                    "UPDATE recovery_intents SET status='COMMITTED', bundle_id=? WHERE intent_id=?",
                    (bundle_id, intent_id),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def stage_intent(
        self, tx_id: str, op_seq: int, stage: str, payload: dict[str, Any]
    ) -> int:
        connection = self.open()
        cursor = connection.execute(
            """
            INSERT INTO recovery_intents(tx_id, op_seq, stage, status, payload_json)
            VALUES (?, ?, ?, 'STAGED', ?)
            """,
            (tx_id, op_seq, stage, canonical_json(payload)),
        )
        return int(cursor.lastrowid)

    def get_manifest(self) -> dict[str, Any]:
        connection = self.open()
        import json

        row = connection.execute("SELECT * FROM run_info WHERE id=1").fetchone()
        if row is None:
            raise DiagnosticError(E_STATE_CORRUPT, "run_info is missing")
        manifest = json.loads(row["manifest_json"])
        return {
            "fingerprint": row["fingerprint"],
            "manifest": manifest,
            "run_db_path": row["run_db_path"],
            "status": row["status"],
        }

    def set_run_status(self, status: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE run_info SET status=? WHERE id=1", (status,))

    def status_payload(self) -> dict[str, Any]:
        connection = self.open()
        run_info = connection.execute("SELECT * FROM run_info WHERE id=1").fetchone()
        schedule = connection.execute("SELECT * FROM schedule_state WHERE id=1").fetchone()
        clock = connection.execute("SELECT * FROM clock_state WHERE id=1").fetchone()
        transactions = [
            dict(row)
            for row in connection.execute(
                """
                SELECT tx_id, start_order, isolation, budget_ms, status,
                       current_op_seq, started_sample, finished_sample,
                       wait_count, wait_total_units, wait_error_micro, end_reason
                FROM transactions ORDER BY start_order, tx_id
                """
            )
        ]
        locks = [
            dict(row)
            for row in connection.execute(
                """
                SELECT resource_key, resource_type, table_name, row_id,
                       range_start, range_end, mode, tx_id, op_seq,
                       acquired_sample, reentrant_count
                FROM lock_holders ORDER BY table_name, resource_key, tx_id
                """
            )
        ]
        queue = [
            dict(row)
            for row in connection.execute(
                """
                SELECT wait_id, queue_no, tx_id, op_seq, resource_key,
                       requested_mode, holder_tx, holder_txs_json, index_path,
                       enqueue_sample, status, resolve_sample, resolution
                FROM wait_queue ORDER BY queue_no
                """
            )
        ]
        recovery = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM recovery_runs ORDER BY recovery_id"
            )
        ]
        deadlocks = [
            dict(row)
            for row in connection.execute("SELECT * FROM deadlocks ORDER BY deadlock_id")
        ]
        return {
            "run": dict(run_info) if run_info else None,
            "schedule": dict(schedule) if schedule else None,
            "clock": dict(clock) if clock else None,
            "transactions": transactions,
            "locks": locks,
            "queue": queue,
            "recovery": recovery,
            "deadlocks": deadlocks,
        }

    def integrity_check(self) -> tuple[bool, str]:
        connection = self.open()
        row = connection.execute("PRAGMA integrity_check").fetchone()
        result = str(row[0]) if row else "no result"
        return result == "ok", result

    def dump_debug(self) -> str:
        return pretty_json(self.status_payload())
