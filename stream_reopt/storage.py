from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import EngineError
from .models import SampleSpec, Shape


SCHEMA_VERSION = 1
EMPTY_ROWS_DIGEST = "4168d8bba9550a5de7470f6bb012b46e33214c4f61f0626b4781c6208b14f859"


@dataclass(frozen=True)
class RuntimeState:
    watermark_arrival: int
    committed_count: int
    shape_version: int
    shape: Shape
    last_reopt_clock_ms: int
    drift_accumulator: dict[str, Any]
    precision_sensitive: bool
    result_digest: str
    result_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "watermark_arrival": self.watermark_arrival,
            "committed_count": self.committed_count,
            "shape_version": self.shape_version,
            "shape": self.shape.to_dict(),
            "last_reopt_clock_ms": self.last_reopt_clock_ms,
            "drift_accumulator": self.drift_accumulator,
            "precision_sensitive": self.precision_sensitive,
            "result_digest": self.result_digest,
            "result_rows": self.result_rows,
        }


@dataclass(frozen=True)
class BatchRecord:
    arrival: int
    ordinal: int
    table_name: str
    row_count: int
    digest: str
    column_digests: dict[str, str]


def _shape_from_json(payload: str) -> Shape:
    data = json.loads(payload)
    return Shape(tuple(data["join_order"]), tuple(data["algorithms"]))


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")
        self.conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self.conn = connection
        except OSError as exc:
            raise EngineError("E_IO", "unable to open sqlite database", {"path": str(self.db_path), "error": str(exc)})
        return self.conn

    @contextmanager
    def transaction(self, before_commit: Callable[[], None] | None = None) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                    if before_commit is not None:
                        before_commit()
                    connection.execute("COMMIT")
                except BaseException:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def initialize(self, sample: SampleSpec) -> None:
        connection = self._connect()
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS committed_batches (
                arrival INTEGER PRIMARY KEY,
                ordinal INTEGER NOT NULL UNIQUE,
                table_name TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                digest TEXT NOT NULL,
                column_digests_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                watermark_arrival INTEGER NOT NULL,
                committed_count INTEGER NOT NULL,
                shape_version INTEGER NOT NULL,
                shape_json TEXT NOT NULL,
                last_reopt_clock_ms INTEGER NOT NULL,
                drift_accumulator_json TEXT NOT NULL,
                precision_sensitive INTEGER NOT NULL,
                result_digest TEXT NOT NULL,
                result_rows INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                seq INTEGER PRIMARY KEY,
                arrival INTEGER NOT NULL UNIQUE,
                committed_count INTEGER NOT NULL,
                input_rows INTEGER NOT NULL,
                output_rows INTEGER NOT NULL,
                observed_selectivity REAL NOT NULL,
                drift_sources_json TEXT NOT NULL,
                error_chain_json TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                shape_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drift_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                seq INTEGER NOT NULL,
                arrival INTEGER NOT NULL,
                trigger INTEGER NOT NULL,
                clock_ms INTEGER NOT NULL,
                sources_json TEXT NOT NULL,
                rejected_json TEXT NOT NULL,
                decision TEXT NOT NULL,
                error_code TEXT,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reopt_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                seq INTEGER NOT NULL,
                arrival INTEGER NOT NULL,
                from_version INTEGER NOT NULL,
                to_version INTEGER NOT NULL,
                version_changed INTEGER NOT NULL,
                from_shape_json TEXT NOT NULL,
                to_shape_json TEXT NOT NULL,
                trigger_sources_json TEXT NOT NULL,
                rejected_candidates_json TEXT NOT NULL,
                switch_cost REAL NOT NULL,
                cost_before REAL NOT NULL,
                cost_after REAL NOT NULL,
                precision_sensitive INTEGER NOT NULL,
                error_chain_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS shape_history (
                version INTEGER PRIMARY KEY,
                created_seq INTEGER NOT NULL,
                created_arrival INTEGER NOT NULL,
                shape_json TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_drift_events_seq ON drift_events(seq);
            CREATE INDEX IF NOT EXISTS idx_reopt_events_seq ON reopt_events(seq);
            """
        )
        plan_hash = sample.plan_hash()
        with self.transaction() as tx:
            existing = {
                row["key"]: row["value"] for row in tx.execute("SELECT key, value FROM meta").fetchall()
            }
            if existing:
                if existing.get("schema_version") != str(SCHEMA_VERSION):
                    raise EngineError(
                        "E_STATE_MISMATCH",
                        "database schema version does not match runtime",
                        {
                            "expected": SCHEMA_VERSION,
                            "actual": existing.get("schema_version"),
                        },
                    )
                if existing.get("plan_hash") != plan_hash:
                    raise EngineError(
                        "E_STATE_MISMATCH",
                        "database belongs to a different plan/sample",
                        {"expected_plan_hash": plan_hash, "actual_plan_hash": existing.get("plan_hash")},
                    )
            else:
                meta = {
                    "schema_version": str(SCHEMA_VERSION),
                    "sample_id": sample.sample_id,
                    "query_id": sample.plan.query_id,
                    "plan_hash": plan_hash,
                    "global_seed": str(sample.global_seed),
                    "total_batches": str(len(sample.plan.batches)),
                }
                tx.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    sorted(meta.items()),
                )
            state = tx.execute("SELECT id FROM runtime_state WHERE id = 1").fetchone()
            if state is None:
                tx.execute(
                    """
                    INSERT INTO runtime_state(
                        id, watermark_arrival, committed_count, shape_version, shape_json,
                        last_reopt_clock_ms, drift_accumulator_json, precision_sensitive,
                        result_digest, result_rows
                    ) VALUES (1, 0, 0, 1, ?, 0, '{}', 0, ?, 0)
                    """,
                    (
                        json.dumps(sample.plan.initial_shape.to_dict(), sort_keys=True),
                        EMPTY_ROWS_DIGEST,
                    ),
                )
                tx.execute(
                    "INSERT INTO shape_history(version, created_seq, created_arrival, shape_json, reason) "
                    "VALUES (1, 0, 0, ?, 'initial')",
                    (json.dumps(sample.plan.initial_shape.to_dict(), sort_keys=True),),
                )

    def state(self, tx: sqlite3.Connection | None = None) -> RuntimeState:
        connection = tx or self._connect()
        row = connection.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone()
        if row is None:
            raise EngineError("E_NO_STATE", "runtime state is not initialized")
        return RuntimeState(
            watermark_arrival=row["watermark_arrival"],
            committed_count=row["committed_count"],
            shape_version=row["shape_version"],
            shape=_shape_from_json(row["shape_json"]),
            last_reopt_clock_ms=row["last_reopt_clock_ms"],
            drift_accumulator=json.loads(row["drift_accumulator_json"]),
            precision_sensitive=bool(row["precision_sensitive"]),
            result_digest=row["result_digest"],
            result_rows=row["result_rows"],
        )

    def batch_records(self, tx: sqlite3.Connection | None = None) -> list[BatchRecord]:
        connection = tx or self._connect()
        rows = connection.execute("SELECT * FROM committed_batches ORDER BY ordinal").fetchall()
        return [
            BatchRecord(
                arrival=row["arrival"],
                ordinal=row["ordinal"],
                table_name=row["table_name"],
                row_count=row["row_count"],
                digest=row["digest"],
                column_digests=json.loads(row["column_digests_json"]),
            )
            for row in rows
        ]

    def batch_record(self, arrival: int, tx: sqlite3.Connection | None = None) -> BatchRecord | None:
        connection = tx or self._connect()
        row = connection.execute("SELECT * FROM committed_batches WHERE arrival = ?", (arrival,)).fetchone()
        if row is None:
            return None
        return BatchRecord(
            arrival=row["arrival"],
            ordinal=row["ordinal"],
            table_name=row["table_name"],
            row_count=row["row_count"],
            digest=row["digest"],
            column_digests=json.loads(row["column_digests_json"]),
        )

    def insert_batch(
        self,
        tx: sqlite3.Connection,
        arrival: int,
        ordinal: int,
        table_name: str,
        row_count: int,
        digest: str,
        column_digests: dict[str, str],
    ) -> None:
        tx.execute(
            """
            INSERT INTO committed_batches(arrival, ordinal, table_name, row_count, digest, column_digests_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (arrival, ordinal, table_name, row_count, digest, json.dumps(column_digests, sort_keys=True)),
        )

    def insert_observation(
        self,
        tx: sqlite3.Connection,
        *,
        seq: int,
        arrival: int,
        committed_count: int,
        input_rows: int,
        output_rows: int,
        observed_selectivity: float,
        drift_sources: list[dict[str, Any]],
        error_chain: dict[str, Any],
        result_digest: str,
        shape_version: int,
    ) -> None:
        tx.execute(
            """
            INSERT INTO observations(
                seq, arrival, committed_count, input_rows, output_rows, observed_selectivity,
                drift_sources_json, error_chain_json, result_digest, shape_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                arrival,
                committed_count,
                input_rows,
                output_rows,
                observed_selectivity,
                json.dumps(drift_sources, sort_keys=True),
                json.dumps(error_chain, sort_keys=True),
                result_digest,
                shape_version,
            ),
        )

    def insert_drift_event(
        self,
        tx: sqlite3.Connection,
        *,
        seq: int,
        arrival: int,
        trigger: bool,
        clock_ms: int,
        sources: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        decision: str,
        error_code: str | None,
        details: dict[str, Any],
    ) -> None:
        tx.execute(
            """
            INSERT INTO drift_events(
                seq, arrival, trigger, clock_ms, sources_json, rejected_json,
                decision, error_code, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                arrival,
                int(trigger),
                clock_ms,
                json.dumps(sources, sort_keys=True),
                json.dumps(rejected, sort_keys=True),
                decision,
                error_code,
                json.dumps(details, sort_keys=True),
            ),
        )

    def insert_reopt_event(
        self,
        tx: sqlite3.Connection,
        *,
        seq: int,
        arrival: int,
        from_version: int,
        to_version: int,
        from_shape: Shape,
        to_shape: Shape,
        trigger_sources: list[dict[str, Any]],
        rejected_candidates: list[dict[str, Any]],
        switch_cost: float,
        cost_before: float,
        cost_after: float,
        precision_sensitive: bool,
        error_chain: dict[str, Any],
        outcome: str,
        error_code: str | None,
    ) -> None:
        tx.execute(
            """
            INSERT INTO reopt_events(
                seq, arrival, from_version, to_version, version_changed, from_shape_json,
                to_shape_json, trigger_sources_json, rejected_candidates_json, switch_cost,
                cost_before, cost_after, precision_sensitive, error_chain_json, outcome, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                arrival,
                from_version,
                to_version,
                int(from_version != to_version),
                json.dumps(from_shape.to_dict(), sort_keys=True),
                json.dumps(to_shape.to_dict(), sort_keys=True),
                json.dumps(trigger_sources, sort_keys=True),
                json.dumps(rejected_candidates, sort_keys=True),
                switch_cost,
                cost_before,
                cost_after,
                int(precision_sensitive),
                json.dumps(error_chain, sort_keys=True),
                outcome,
                error_code,
            ),
        )

    def update_state(
        self,
        tx: sqlite3.Connection,
        *,
        watermark_arrival: int,
        committed_count: int,
        shape_version: int,
        shape: Shape,
        last_reopt_clock_ms: int,
        drift_accumulator: dict[str, Any],
        precision_sensitive: bool,
        result_digest: str,
        result_rows: int,
    ) -> None:
        cursor = tx.execute(
            """
            UPDATE runtime_state SET
                watermark_arrival = ?, committed_count = ?, shape_version = ?, shape_json = ?,
                last_reopt_clock_ms = ?, drift_accumulator_json = ?, precision_sensitive = ?,
                result_digest = ?, result_rows = ?
            WHERE id = 1
            """,
            (
                watermark_arrival,
                committed_count,
                shape_version,
                json.dumps(shape.to_dict(), sort_keys=True),
                last_reopt_clock_ms,
                json.dumps(drift_accumulator, sort_keys=True),
                int(precision_sensitive),
                result_digest,
                result_rows,
            ),
        )
        if cursor.rowcount != 1:
            raise EngineError("E_CORRUPT_STATE", "runtime state pointer is missing")

    def insert_shape_history(
        self,
        tx: sqlite3.Connection,
        *,
        version: int,
        seq: int,
        arrival: int,
        shape: Shape,
        reason: str,
    ) -> None:
        tx.execute(
            "INSERT INTO shape_history(version, created_seq, created_arrival, shape_json, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (version, seq, arrival, json.dumps(shape.to_dict(), sort_keys=True), reason),
        )

    def _observation_rows(
        self, connection: sqlite3.Connection, limit: int | None
    ) -> list[dict[str, Any]]:
        if limit is None:
            rows = connection.execute("SELECT * FROM observations ORDER BY seq").fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM observations ORDER BY seq DESC LIMIT ?", (max(0, limit),)
            ).fetchall()[::-1]
        return [
            {
                "seq": row["seq"],
                "arrival": row["arrival"],
                "committed_count": row["committed_count"],
                "input_rows": row["input_rows"],
                "output_rows": row["output_rows"],
                "observed_selectivity": row["observed_selectivity"],
                "drift_sources": json.loads(row["drift_sources_json"]),
                "error_chain": json.loads(row["error_chain_json"]),
                "result_digest": row["result_digest"],
                "shape_version": row["shape_version"],
            }
            for row in rows
        ]

    def observations(
        self, limit: int | None = None, tx: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        if tx is not None:
            return self._observation_rows(tx, limit)
        with self.read_snapshot() as connection:
            return self._observation_rows(connection, limit)

    def _drift_rows(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM drift_events ORDER BY event_id").fetchall()
        return [
            {
                "event_id": row["event_id"],
                "seq": row["seq"],
                "arrival": row["arrival"],
                "trigger": bool(row["trigger"]),
                "clock_ms": row["clock_ms"],
                "sources": json.loads(row["sources_json"]),
                "rejected": json.loads(row["rejected_json"]),
                "decision": row["decision"],
                "error_code": row["error_code"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def drift_events(self, tx: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if tx is not None:
            return self._drift_rows(tx)
        with self.read_snapshot() as connection:
            return self._drift_rows(connection)

    def _reopt_rows(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM reopt_events ORDER BY event_id").fetchall()
        return [
            {
                "event_id": row["event_id"],
                "seq": row["seq"],
                "arrival": row["arrival"],
                "from_version": row["from_version"],
                "to_version": row["to_version"],
                "version_changed": bool(row["version_changed"]),
                "from_shape": json.loads(row["from_shape_json"]),
                "to_shape": json.loads(row["to_shape_json"]),
                "trigger_sources": json.loads(row["trigger_sources_json"]),
                "rejected_candidates": json.loads(row["rejected_candidates_json"]),
                "switch_cost": row["switch_cost"],
                "cost_before": row["cost_before"],
                "cost_after": row["cost_after"],
                "precision_sensitive": bool(row["precision_sensitive"]),
                "error_chain": json.loads(row["error_chain_json"]),
                "outcome": row["outcome"],
                "error_code": row["error_code"],
            }
            for row in rows
        ]

    def reopt_events(self, tx: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if tx is not None:
            return self._reopt_rows(tx)
        with self.read_snapshot() as connection:
            return self._reopt_rows(connection)

    def _history_rows(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM shape_history ORDER BY version").fetchall()
        return [
            {
                "version": row["version"],
                "created_seq": row["created_seq"],
                "created_arrival": row["created_arrival"],
                "shape": json.loads(row["shape_json"]),
                "reason": row["reason"],
            }
            for row in rows
        ]

    def shape_history(self, tx: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if tx is not None:
            return self._history_rows(tx)
        with self.read_snapshot() as connection:
            return self._history_rows(connection)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Storage":
        self._connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def read_status_without_sample(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        raise EngineError("E_NO_STATE", "database does not exist", {"path": str(path)})
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN")
        meta_rows = connection.execute("SELECT key, value FROM meta").fetchall()
        meta = {row["key"]: row["value"] for row in meta_rows}
        state_row = connection.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone()
        if state_row is None:
            raise EngineError("E_NO_STATE", "runtime state is not initialized", {"path": str(path)})
        batch_rows = connection.execute(
            "SELECT arrival, ordinal, table_name, row_count, digest FROM committed_batches ORDER BY ordinal"
        ).fetchall()
        drift_rows = connection.execute("SELECT * FROM drift_events ORDER BY event_id").fetchall()
        reopt_rows = connection.execute("SELECT * FROM reopt_events ORDER BY event_id").fetchall()
        history_rows = connection.execute("SELECT * FROM shape_history ORDER BY version").fetchall()
        shape = json.loads(state_row["shape_json"])
        drift_records = [
            {
                "event_id": row["event_id"],
                "seq": row["seq"],
                "arrival": row["arrival"],
                "trigger": bool(row["trigger"]),
                "clock_ms": row["clock_ms"],
                "sources": json.loads(row["sources_json"]),
                "rejected": json.loads(row["rejected_json"]),
                "decision": row["decision"],
                "error_code": row["error_code"],
                "details": json.loads(row["details_json"]),
            }
            for row in drift_rows
        ]
        reopt_records = [
            {
                "event_id": row["event_id"],
                "seq": row["seq"],
                "arrival": row["arrival"],
                "from_version": row["from_version"],
                "to_version": row["to_version"],
                "version_changed": bool(row["version_changed"]),
                "from_shape": json.loads(row["from_shape_json"]),
                "to_shape": json.loads(row["to_shape_json"]),
                "trigger_sources": json.loads(row["trigger_sources_json"]),
                "rejected_candidates": json.loads(row["rejected_candidates_json"]),
                "switch_cost": row["switch_cost"],
                "cost_before": row["cost_before"],
                "cost_after": row["cost_after"],
                "precision_sensitive": bool(row["precision_sensitive"]),
                "error_chain": json.loads(row["error_chain_json"]),
                "outcome": row["outcome"],
                "error_code": row["error_code"],
            }
            for row in reopt_rows
        ]
        payload = {
            "ok": True,
            "sample_id": meta.get("sample_id"),
            "query_id": meta.get("query_id"),
            "watermark": {
                "last_arrival": state_row["watermark_arrival"],
                "committed_batches": state_row["committed_count"],
                "total_batches": int(meta.get("total_batches", "0")),
            },
            "shape_version": state_row["shape_version"],
            "current_shape": shape,
            "last_reopt_clock_ms": state_row["last_reopt_clock_ms"],
            "precision_sensitive": bool(state_row["precision_sensitive"]),
            "result": {
                "rows": state_row["result_rows"],
                "digest": state_row["result_digest"],
            },
            "drift_accumulator": json.loads(state_row["drift_accumulator_json"]),
            "committed_batches": [
                {
                    "arrival": row["arrival"],
                    "ordinal": row["ordinal"],
                    "table": row["table_name"],
                    "row_count": row["row_count"],
                    "digest": row["digest"],
                }
                for row in batch_rows
            ],
            "drift_records": drift_records,
            "reopt_records": reopt_records,
            "shape_history": [
                {
                    "version": row["version"],
                    "created_seq": row["created_seq"],
                    "created_arrival": row["created_arrival"],
                    "shape": json.loads(row["shape_json"]),
                    "reason": row["reason"],
                }
                for row in history_rows
            ],
        }
        connection.execute("COMMIT")
        return payload
    finally:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        connection.close()
