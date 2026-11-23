"""SQLite-backed atomic state store."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .aggregate import aggregate_from_row, build_aggregate, QUANTILES
from .canonical import canonical_json, digest
from .errors import EngineError
from .failpoints import process_exit_at
from .samples import window_bounds
from .snapshots import (
    finalize_snapshot,
    merge_patch,
    snapshot_for_clock,
    snapshot_json,
)


SCHEMA_VERSION = 1

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS db_model (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        digest TEXT NOT NULL,
        model_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stats_snapshots (
        version INTEGER PRIMARY KEY,
        effective_at_ms INTEGER NOT NULL,
        digest TEXT NOT NULL,
        covered_tables_json TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        classification TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stats_pointer (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        version INTEGER NOT NULL REFERENCES stats_snapshots(version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sample_watermark (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        last_seq INTEGER,
        last_clock_ms INTEGER NOT NULL,
        window_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS samples (
        seq INTEGER PRIMARY KEY,
        logical_clock_ms INTEGER NOT NULL,
        sql_text TEXT NOT NULL,
        template_id TEXT NOT NULL,
        window_key TEXT NOT NULL,
        snapshot_version INTEGER NOT NULL REFERENCES stats_snapshots(version),
        known_snapshot_version INTEGER NOT NULL,
        duration_ms INTEGER NOT NULL,
        scanned_rows INTEGER NOT NULL,
        returned_rows INTEGER NOT NULL,
        index_hit TEXT,
        content_digest TEXT NOT NULL,
        structure_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_samples_group
    ON samples(template_id, snapshot_version, seq)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_samples_window
    ON samples(window_key, seq)
    """,
    """
    CREATE TABLE IF NOT EXISTS windows (
        window_key TEXT PRIMARY KEY,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        sample_count INTEGER NOT NULL,
        first_seq INTEGER NOT NULL,
        last_seq INTEGER NOT NULL,
        min_clock_ms INTEGER NOT NULL,
        max_clock_ms INTEGER NOT NULL,
        snapshot_versions_json TEXT NOT NULL,
        summary_digest TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS template_aggregates (
        template_id TEXT NOT NULL,
        snapshot_version INTEGER NOT NULL REFERENCES stats_snapshots(version),
        structure_json TEXT NOT NULL,
        sample_count INTEGER NOT NULL,
        durations_json TEXT NOT NULL,
        scanned_json TEXT NOT NULL,
        returned_json TEXT NOT NULL,
        window_keys_json TEXT NOT NULL,
        min_seq INTEGER NOT NULL,
        max_seq INTEGER NOT NULL,
        min_clock_ms INTEGER NOT NULL,
        max_clock_ms INTEGER NOT NULL,
        total_scanned_rows INTEGER NOT NULL,
        total_returned_rows INTEGER NOT NULL,
        avg_scanned_rows REAL NOT NULL,
        avg_returned_rows REAL NOT NULL,
        p50 REAL NOT NULL,
        p90 REAL NOT NULL,
        p95 REAL NOT NULL,
        p99 REAL NOT NULL,
        digest TEXT NOT NULL,
        PRIMARY KEY (template_id, snapshot_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        template_id TEXT NOT NULL,
        snapshot_version INTEGER NOT NULL REFERENCES stats_snapshots(version),
        aggregate_digest TEXT NOT NULL,
        profile_digest TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_run_id INTEGER,
        PRIMARY KEY (template_id, snapshot_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        max_workers INTEGER NOT NULL,
        memory_budget INTEGER NOT NULL,
        memory_unit INTEGER NOT NULL,
        logical_clock_ms INTEGER NOT NULL,
        status TEXT NOT NULL,
        memory_charged INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_tasks (
        run_id INTEGER NOT NULL REFERENCES plan_runs(run_id),
        template_id TEXT NOT NULL,
        snapshot_version INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        candidate_count INTEGER NOT NULL,
        memory_units INTEGER NOT NULL,
        charged INTEGER NOT NULL,
        status TEXT NOT NULL,
        error_json TEXT,
        abandon_json TEXT,
        profile_id TEXT,
        PRIMARY KEY (run_id, template_id, snapshot_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invalidation_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_version INTEGER NOT NULL,
        new_version INTEGER NOT NULL,
        classification TEXT NOT NULL,
        closure_digest TEXT NOT NULL,
        closure_json TEXT NOT NULL
    )
    """,
)


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.connection: sqlite3.Connection | None = None

    def open(self) -> "StateStore":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.connection = sqlite3.connect(
                self.path,
                timeout=30.0,
                isolation_level=None,
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error as exc:
            raise EngineError(
                "ERR_STATE_OPEN",
                "cannot open state database",
                {"path": str(self.path), "error": str(exc)},
            ) from exc
        return self

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "StateStore":
        return self.open()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("state store is not open")
        return self.connection

    def require_initialized(self) -> None:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'initialized'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise EngineError(
                "ERR_STATE_NOT_INITIALIZED",
                "state database has not been initialized",
                {"path": str(self.path)},
            ) from exc
        if row is None or row["value"] != "1":
            raise EngineError(
                "ERR_STATE_NOT_INITIALIZED",
                "state database has not been initialized",
                {"path": str(self.path)},
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Hold one consistent WAL snapshot across a multi-query read."""

        try:
            self.conn.execute("BEGIN DEFERRED")
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _create_schema(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            self.conn.execute(statement)

    def _reset_tables(self) -> None:
        for table in (
            "invalidation_events",
            "plan_tasks",
            "plan_runs",
            "profiles",
            "template_aggregates",
            "windows",
            "samples",
            "sample_watermark",
            "stats_pointer",
            "stats_snapshots",
            "db_model",
            "meta",
        ):
            self.conn.execute(f"DELETE FROM {table}")

    def initialize(
        self,
        *,
        model: dict[str, Any],
        snapshot: dict[str, Any],
        window_ms: int,
        reset: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(window_ms, int) or isinstance(window_ms, bool) or window_ms <= 0:
            raise EngineError(
                "ERR_WINDOW_CONFIG", "window_ms must be a positive integer"
            )
        try:
            with self.transaction() as conn:
                self._create_schema()
                existing = conn.execute(
                    "SELECT value FROM meta WHERE key = 'initialized'"
                ).fetchone()
                if existing is not None and not reset:
                    model_row = conn.execute(
                        "SELECT digest FROM db_model WHERE singleton = 1"
                    ).fetchone()
                    snapshot_row = conn.execute(
                        "SELECT version, digest FROM stats_snapshots ORDER BY version LIMIT 1"
                    ).fetchone()
                    same = (
                        model_row is not None
                        and model_row["digest"] == model["digest"]
                        and snapshot_row is not None
                        and snapshot_row["version"] == snapshot["version"]
                        and snapshot_row["digest"] == snapshot["digest"]
                    )
                    if not same:
                        raise EngineError(
                            "ERR_STATE_CONFLICT",
                            "state is already initialized with different inputs",
                            {
                                "state": str(self.path),
                                "model_digest": model_row["digest"] if model_row else None,
                                "snapshot_version": (
                                    snapshot_row["version"] if snapshot_row else None
                                ),
                            },
                        )
                    return {
                        "state": "already_loaded",
                        "model_digest": model["digest"],
                        "snapshot_version": snapshot["version"],
                    }
                if existing is not None:
                    self._reset_tables()
                self._create_schema()
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('protocol_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('initialized', '1')"
                )
                conn.execute(
                    "INSERT INTO db_model(singleton, digest, model_json) VALUES(1, ?, ?)",
                    (model["digest"], canonical_json(model)),
                )
                conn.execute(
                    """
                    INSERT INTO stats_snapshots(
                        version, effective_at_ms, digest, covered_tables_json,
                        snapshot_json, classification
                    ) VALUES (?, ?, ?, ?, ?, 'initial')
                    """,
                    (
                        snapshot["version"],
                        snapshot["effective_at_ms"],
                        snapshot["digest"],
                        canonical_json(snapshot["covered_tables"]),
                        snapshot_json(snapshot),
                    ),
                )
                conn.execute(
                    "INSERT INTO stats_pointer(singleton, version) VALUES(1, ?)",
                    (snapshot["version"],),
                )
                conn.execute(
                    """
                    INSERT INTO sample_watermark(
                        singleton, last_seq, last_clock_ms, window_ms
                    ) VALUES(1, NULL, 0, ?)
                    """,
                    (window_ms,),
                )
                return {
                    "state": "loaded",
                    "model_digest": model["digest"],
                    "snapshot_version": snapshot["version"],
                    "window_ms": window_ms,
                }
        except sqlite3.Error as exc:
            raise EngineError(
                "ERR_STATE_IO",
                "cannot initialize state database",
                {"path": str(self.path), "error": str(exc)},
            ) from exc

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def get_db_model(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT model_json FROM db_model WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise EngineError(
                "ERR_STATE_NOT_INITIALIZED", "database model is not loaded"
            )
        return json.loads(row["model_json"])

    def list_snapshots(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT snapshot_json FROM stats_snapshots ORDER BY version"
        ).fetchall()
        return [json.loads(row["snapshot_json"]) for row in rows]

    def current_snapshot(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT s.snapshot_json
            FROM stats_pointer p
            JOIN stats_snapshots s ON s.version = p.version
            WHERE p.singleton = 1
            """
        ).fetchone()
        if row is None:
            raise EngineError(
                "ERR_STATE_NOT_INITIALIZED", "statistics pointer is not initialized"
            )
        return json.loads(row["snapshot_json"])

    def snapshot_by_version(self, version: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT snapshot_json FROM stats_snapshots WHERE version = ?", (version,)
        ).fetchone()
        return None if row is None else json.loads(row["snapshot_json"])

    def watermark(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT last_seq, last_clock_ms, window_ms "
            "FROM sample_watermark WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise EngineError(
                "ERR_STATE_NOT_INITIALIZED", "sample watermark is not initialized"
            )
        return {
            "last_seq": row["last_seq"],
            "last_clock_ms": row["last_clock_ms"],
            "window_ms": row["window_ms"],
        }

    def _refresh_aggregate(
        self, conn: sqlite3.Connection, template_id: str, snapshot_version: int
    ) -> None:
        rows = conn.execute(
            """
            SELECT seq, logical_clock_ms, duration_ms, scanned_rows,
                   returned_rows, window_key, structure_json
            FROM samples
            WHERE template_id = ? AND snapshot_version = ?
            ORDER BY seq
            """,
            (template_id, snapshot_version),
        ).fetchall()
        if not rows:
            return
        samples = [
            {
                "seq": row["seq"],
                "logical_clock_ms": row["logical_clock_ms"],
                "duration_ms": row["duration_ms"],
                "scanned_rows": row["scanned_rows"],
                "returned_rows": row["returned_rows"],
                "window_key": row["window_key"],
            }
            for row in rows
        ]
        structure = json.loads(rows[0]["structure_json"])
        aggregate = build_aggregate(
            template_id=template_id,
            snapshot_version=snapshot_version,
            structure=structure,
            samples=samples,
        )
        conn.execute(
            """
            INSERT INTO template_aggregates(
                template_id, snapshot_version, structure_json, sample_count,
                durations_json, scanned_json, returned_json, window_keys_json,
                min_seq, max_seq, min_clock_ms, max_clock_ms,
                total_scanned_rows, total_returned_rows,
                avg_scanned_rows, avg_returned_rows,
                p50, p90, p95, p99, digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(template_id, snapshot_version) DO UPDATE SET
                structure_json=excluded.structure_json,
                sample_count=excluded.sample_count,
                durations_json=excluded.durations_json,
                scanned_json=excluded.scanned_json,
                returned_json=excluded.returned_json,
                window_keys_json=excluded.window_keys_json,
                min_seq=excluded.min_seq,
                max_seq=excluded.max_seq,
                min_clock_ms=excluded.min_clock_ms,
                max_clock_ms=excluded.max_clock_ms,
                total_scanned_rows=excluded.total_scanned_rows,
                total_returned_rows=excluded.total_returned_rows,
                avg_scanned_rows=excluded.avg_scanned_rows,
                avg_returned_rows=excluded.avg_returned_rows,
                p50=excluded.p50,
                p90=excluded.p90,
                p95=excluded.p95,
                p99=excluded.p99,
                digest=excluded.digest
            """,
            (
                aggregate["template_id"],
                aggregate["snapshot_version"],
                canonical_json(aggregate["structure"]),
                aggregate["sample_count"],
                canonical_json(aggregate["durations"]),
                canonical_json(aggregate["scanned_rows"]),
                canonical_json(aggregate["returned_rows"]),
                canonical_json(aggregate["window_keys"]),
                aggregate["min_seq"],
                aggregate["max_seq"],
                aggregate["min_clock_ms"],
                aggregate["max_clock_ms"],
                aggregate["total_scanned_rows"],
                aggregate["total_returned_rows"],
                aggregate["avg_scanned_rows"],
                aggregate["avg_returned_rows"],
                aggregate["p50"],
                aggregate["p90"],
                aggregate["p95"],
                aggregate["p99"],
                aggregate["aggregate_digest"],
            ),
        )

    def _refresh_window(self, conn: sqlite3.Connection, window_key: str) -> None:
        rows = conn.execute(
            """
            SELECT seq, logical_clock_ms, snapshot_version
            FROM samples WHERE window_key = ? ORDER BY seq
            """,
            (window_key,),
        ).fetchall()
        if not rows:
            return
        start_text, end_text = window_key[1:].split("-", 1)
        start = int(start_text)
        end = int(end_text)
        versions = sorted({row["snapshot_version"] for row in rows})
        summary = {
            "window_key": window_key,
            "start_ms": start,
            "end_ms": end,
            "sample_count": len(rows),
            "first_seq": rows[0]["seq"],
            "last_seq": rows[-1]["seq"],
            "min_clock_ms": min(row["logical_clock_ms"] for row in rows),
            "max_clock_ms": max(row["logical_clock_ms"] for row in rows),
            "snapshot_versions": versions,
        }
        conn.execute(
            """
            INSERT INTO windows(
                window_key, start_ms, end_ms, sample_count, first_seq, last_seq,
                min_clock_ms, max_clock_ms, snapshot_versions_json, summary_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_key) DO UPDATE SET
                sample_count=excluded.sample_count,
                first_seq=excluded.first_seq,
                last_seq=excluded.last_seq,
                min_clock_ms=excluded.min_clock_ms,
                max_clock_ms=excluded.max_clock_ms,
                snapshot_versions_json=excluded.snapshot_versions_json,
                summary_digest=excluded.summary_digest
            """,
            (
                window_key,
                start,
                end,
                summary["sample_count"],
                summary["first_seq"],
                summary["last_seq"],
                summary["min_clock_ms"],
                summary["max_clock_ms"],
                canonical_json(versions),
                digest(summary),
            ),
        )

    def ingest_samples(
        self,
        samples: list[dict[str, Any]],
        *,
        injected_clock_ms: int | None = None,
    ) -> dict[str, Any]:
        self.require_initialized()
        watermark = self.watermark()
        for sample in samples:
            if sample["window_end_ms"] - sample["window_start_ms"] != watermark["window_ms"]:
                raise EngineError(
                    "ERR_WINDOW_CONFIG",
                    "incoming sample was validated with a different window size",
                    {
                        "state_window_ms": watermark["window_ms"],
                        "incoming_window_ms": (
                            sample["window_end_ms"] - sample["window_start_ms"]
                        ),
                    },
                )

        snapshots = self.list_snapshots()
        if not snapshots:
            raise EngineError("ERR_SNAPSHOT_NOT_EFFECTIVE", "no snapshots are loaded")
        self._preflight_samples(samples, snapshots)

        chunks: list[list[dict[str, Any]]] = []
        for sample in samples:
            if not chunks or chunks[-1][-1]["window_key"] != sample["window_key"]:
                chunks.append([sample])
            else:
                chunks[-1].append(sample)

        inserted = 0
        duplicates = 0
        for chunk_index, chunk in enumerate(chunks):
            with self.transaction() as conn:
                current = conn.execute(
                    "SELECT last_seq, last_clock_ms FROM sample_watermark WHERE singleton = 1"
                ).fetchone()
                last_seq = current["last_seq"]
                high_clock = current["last_clock_ms"]
                affected_groups: set[tuple[str, int]] = set()
                affected_windows: set[str] = set()
                chunk_inserted = 0
                chunk_duplicates = 0
                for sample in chunk:
                    existing = conn.execute(
                        "SELECT content_digest FROM samples WHERE seq = ?",
                        (sample["seq"],),
                    ).fetchone()
                    if existing is not None:
                        if existing["content_digest"] != sample["content_digest"]:
                            raise EngineError(
                                "ERR_SAMPLE_CONFLICT",
                                "sample sequence already exists with different content",
                                {"seq": sample["seq"]},
                            )
                        chunk_duplicates += 1
                        continue
                    if last_seq is not None and sample["seq"] <= last_seq:
                        raise EngineError(
                            "ERR_SAMPLE_SEQ_ORDER",
                            "new sample sequence is below the committed watermark",
                            {
                                "seq": sample["seq"],
                                "last_seq": last_seq,
                            },
                        )
                    snapshot = snapshot_for_clock(snapshots, sample["logical_clock_ms"])
                    if snapshot is None:
                        raise EngineError(
                            "ERR_SNAPSHOT_NOT_EFFECTIVE",
                            "no statistics snapshot is effective for the sample clock",
                            {
                                "seq": sample["seq"],
                                "logical_clock_ms": sample["logical_clock_ms"],
                            },
                        )
                    sealed = conn.execute(
                        "SELECT 1 FROM profiles WHERE template_id = ? AND snapshot_version = ?",
                        (sample["template_id"], snapshot["version"]),
                    ).fetchone()
                    if sealed is not None:
                        raise EngineError(
                            "ERR_WINDOW_SEALED",
                            "a committed profile already exists for this template and snapshot",
                            {
                                "seq": sample["seq"],
                                "template_id": sample["template_id"],
                                "snapshot_version": snapshot["version"],
                            },
                        )
                    conn.execute(
                        """
                        INSERT INTO samples(
                            seq, logical_clock_ms, sql_text, template_id, window_key,
                            snapshot_version, known_snapshot_version, duration_ms,
                            scanned_rows, returned_rows, index_hit, content_digest,
                            structure_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sample["seq"],
                            sample["logical_clock_ms"],
                            sample["sql"],
                            sample["template_id"],
                            sample["window_key"],
                            snapshot["version"],
                            max(snapshot_item["version"] for snapshot_item in snapshots),
                            sample["duration_ms"],
                            sample["scanned_rows"],
                            sample["returned_rows"],
                            sample["index_hit"],
                            sample["content_digest"],
                            canonical_json(sample["structure"]),
                        ),
                    )
                    last_seq = sample["seq"]
                    high_clock = max(high_clock, sample["logical_clock_ms"])
                    affected_groups.add((sample["template_id"], snapshot["version"]))
                    affected_windows.add(sample["window_key"])
                    chunk_inserted += 1

                for template_id, snapshot_version in sorted(affected_groups):
                    self._refresh_aggregate(conn, template_id, snapshot_version)
                for window_key in sorted(affected_windows):
                    self._refresh_window(conn, window_key)
                if injected_clock_ms is not None:
                    high_clock = max(high_clock, injected_clock_ms)
                conn.execute(
                    "UPDATE sample_watermark SET last_seq = ?, last_clock_ms = ? "
                    "WHERE singleton = 1",
                    (last_seq, high_clock),
                )
                process_exit_at("before_window_commit")
                inserted += chunk_inserted
                duplicates += chunk_duplicates
            process_exit_at("after_window")

        if not chunks and injected_clock_ms is not None:
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE sample_watermark SET last_clock_ms = MAX(last_clock_ms, ?) "
                    "WHERE singleton = 1",
                    (injected_clock_ms,),
                )
        final_watermark = self.watermark()
        return {
            "inserted": inserted,
            "duplicates": duplicates,
            "committed_samples": final_watermark["last_seq"],
            "logical_clock_watermark_ms": final_watermark["last_clock_ms"],
            "windows_touched": len({sample["window_key"] for sample in samples}),
        }

    def _preflight_samples(
        self,
        samples: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> None:
        watermark = self.watermark()
        last_seq = watermark["last_seq"]
        for sample in samples:
            existing = self.conn.execute(
                "SELECT content_digest FROM samples WHERE seq = ?",
                (sample["seq"],),
            ).fetchone()
            if existing is not None:
                if existing["content_digest"] != sample["content_digest"]:
                    raise EngineError(
                        "ERR_SAMPLE_CONFLICT",
                        "sample sequence already exists with different content",
                        {"seq": sample["seq"]},
                    )
                continue
            if last_seq is not None and sample["seq"] <= last_seq:
                raise EngineError(
                    "ERR_SAMPLE_SEQ_ORDER",
                    "new sample sequence is below the committed watermark",
                    {"seq": sample["seq"], "last_seq": last_seq},
                )
            snapshot = snapshot_for_clock(snapshots, sample["logical_clock_ms"])
            if snapshot is None:
                raise EngineError(
                    "ERR_SNAPSHOT_NOT_EFFECTIVE",
                    "no statistics snapshot is effective for the sample clock",
                    {
                        "seq": sample["seq"],
                        "logical_clock_ms": sample["logical_clock_ms"],
                    },
                )
            sealed = self.conn.execute(
                "SELECT 1 FROM profiles WHERE template_id = ? AND snapshot_version = ?",
                (sample["template_id"], snapshot["version"]),
            ).fetchone()
            if sealed is not None:
                raise EngineError(
                    "ERR_WINDOW_SEALED",
                    "a committed profile already exists for this template and snapshot",
                    {
                        "seq": sample["seq"],
                        "template_id": sample["template_id"],
                        "snapshot_version": snapshot["version"],
                    },
                )
            last_seq = sample["seq"]

    def list_windows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM windows ORDER BY start_ms, window_key"
        ).fetchall()
        return [
            {
                "window_key": row["window_key"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "sample_count": row["sample_count"],
                "first_seq": row["first_seq"],
                "last_seq": row["last_seq"],
                "min_clock_ms": row["min_clock_ms"],
                "max_clock_ms": row["max_clock_ms"],
                "snapshot_versions": json.loads(row["snapshot_versions_json"]),
                "summary_digest": row["summary_digest"],
            }
            for row in rows
        ]

    def list_aggregates(
        self, *, max_clock_ms: int | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM template_aggregates"
        parameters: tuple[Any, ...] = ()
        if max_clock_ms is not None:
            query += " WHERE max_clock_ms <= ?"
            parameters = (max_clock_ms,)
        query += " ORDER BY template_id, snapshot_version"
        rows = self.conn.execute(query, parameters).fetchall()
        return [aggregate_from_row(row) for row in rows]

    def get_aggregate(
        self, template_id: str, snapshot_version: int
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM template_aggregates "
            "WHERE template_id = ? AND snapshot_version = ?",
            (template_id, snapshot_version),
        ).fetchone()
        return None if row is None else aggregate_from_row(row)

    def list_profiles(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM profiles ORDER BY template_id, snapshot_version"
        ).fetchall()
        return [
            {
                "template_id": row["template_id"],
                "snapshot_version": row["snapshot_version"],
                "aggregate_digest": row["aggregate_digest"],
                "profile_digest": row["profile_digest"],
                "result": json.loads(row["result_json"]),
                "created_run_id": row["created_run_id"],
            }
            for row in rows
        ]

    def get_profile(
        self, template_id: str, snapshot_version: int
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM profiles WHERE template_id = ? AND snapshot_version = ?",
            (template_id, snapshot_version),
        ).fetchone()
        if row is None:
            return None
        return {
            "template_id": row["template_id"],
            "snapshot_version": row["snapshot_version"],
            "aggregate_digest": row["aggregate_digest"],
            "profile_digest": row["profile_digest"],
            "result": json.loads(row["result_json"]),
            "created_run_id": row["created_run_id"],
        }

    def create_plan_run(
        self,
        *,
        max_workers: int,
        memory_budget: int,
        memory_unit: int,
        logical_clock_ms: int,
    ) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO plan_runs(
                    max_workers, memory_budget, memory_unit,
                    logical_clock_ms, status, memory_charged
                ) VALUES (?, ?, ?, ?, 'running', 0)
                """,
                (max_workers, memory_budget, memory_unit, logical_clock_ms),
            )
            return int(cursor.lastrowid)

    def add_plan_tasks(self, run_id: int, tasks: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO plan_tasks(
                        run_id, template_id, snapshot_version, ordinal,
                        candidate_count, memory_units, charged, status,
                        profile_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task["template_id"],
                        task["snapshot_version"],
                        task["ordinal"],
                        task["candidate_count"],
                        task["memory_units"],
                        0,
                        task["status"],
                        task.get("profile_id"),
                    ),
                )

    def create_plan_run_with_tasks(
        self,
        *,
        max_workers: int,
        memory_budget: int,
        memory_unit: int,
        logical_clock_ms: int,
        tasks: list[dict[str, Any]],
    ) -> int:
        """Create a run and all initial task rows in one transaction."""

        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO plan_runs(
                    max_workers, memory_budget, memory_unit,
                    logical_clock_ms, status, memory_charged
                ) VALUES (?, ?, ?, ?, 'running', 0)
                """,
                (max_workers, memory_budget, memory_unit, logical_clock_ms),
            )
            run_id = int(cursor.lastrowid)
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO plan_tasks(
                        run_id, template_id, snapshot_version, ordinal,
                        candidate_count, memory_units, charged, status,
                        profile_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        run_id,
                        task["template_id"],
                        task["snapshot_version"],
                        task["ordinal"],
                        task["candidate_count"],
                        task["memory_units"],
                        task["status"],
                        task.get("profile_id"),
                    ),
                )
            return run_id

    def latest_run(self, *, include_finished: bool = True) -> dict[str, Any] | None:
        query = "SELECT * FROM plan_runs"
        if not include_finished:
            query += " WHERE status = 'running'"
        query += " ORDER BY run_id DESC LIMIT 1"
        row = self.conn.execute(query).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "max_workers": row["max_workers"],
            "memory_budget": row["memory_budget"],
            "memory_unit": row["memory_unit"],
            "logical_clock_ms": row["logical_clock_ms"],
            "status": row["status"],
            "memory_charged": row["memory_charged"],
        }

    def reopen_run(
        self,
        run_id: int,
        *,
        retry_abandoned: bool,
        retry_failed: bool,
        memory_budget: int | None = None,
        memory_unit: int | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            run = conn.execute(
                "SELECT * FROM plan_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise EngineError(
                    "ERR_PLAN_RUN_NOT_FOUND",
                    "plan run does not exist",
                    {"run_id": run_id},
                )
            conn.execute(
                "UPDATE plan_tasks SET status='pending', error_json=NULL "
                "WHERE run_id = ? AND status='running'",
                (run_id,),
            )
            if retry_abandoned:
                conn.execute(
                    "UPDATE plan_tasks SET status='pending', abandon_json=NULL "
                    "WHERE run_id = ? AND status='abandoned'",
                    (run_id,),
                )
            if retry_failed:
                conn.execute(
                    "UPDATE plan_tasks SET status='pending', error_json=NULL "
                    "WHERE run_id = ? AND status='failed'",
                    (run_id,),
                )
            conn.execute(
                "UPDATE plan_runs SET status='running' WHERE run_id = ?", (run_id,)
            )
            if memory_budget is not None:
                conn.execute(
                    "UPDATE plan_runs SET memory_budget = ? WHERE run_id = ?",
                    (memory_budget, run_id),
                )
            if memory_unit is not None:
                conn.execute(
                    "UPDATE plan_runs SET memory_unit = ? WHERE run_id = ?",
                    (memory_unit, run_id),
                )
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM plan_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise EngineError(
                "ERR_PLAN_RUN_NOT_FOUND", "plan run does not exist", {"run_id": run_id}
            )
        return {
            "run_id": row["run_id"],
            "max_workers": row["max_workers"],
            "memory_budget": row["memory_budget"],
            "memory_unit": row["memory_unit"],
            "logical_clock_ms": row["logical_clock_ms"],
            "status": row["status"],
            "memory_charged": row["memory_charged"],
        }

    def list_tasks(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM plan_tasks WHERE run_id = ? "
            "ORDER BY ordinal, template_id, snapshot_version",
            (run_id,),
        ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "template_id": row["template_id"],
                "snapshot_version": row["snapshot_version"],
                "ordinal": row["ordinal"],
                "candidate_count": row["candidate_count"],
                "memory_units": row["memory_units"],
                "charged": bool(row["charged"]),
                "status": row["status"],
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "abandon_reason": (
                    json.loads(row["abandon_json"]) if row["abandon_json"] else None
                ),
                "profile_id": row["profile_id"],
            }
            for row in rows
        ]

    def claim_task(
        self, run_id: int, template_id: str, snapshot_version: int
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            run = conn.execute(
                "SELECT * FROM plan_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            task = conn.execute(
                "SELECT * FROM plan_tasks WHERE run_id = ? "
                "AND template_id = ? AND snapshot_version = ?",
                (run_id, template_id, snapshot_version),
            ).fetchone()
            if run is None or task is None:
                raise EngineError(
                    "ERR_PLAN_TASK_NOT_FOUND",
                    "plan task does not exist",
                    {
                        "run_id": run_id,
                        "template_id": template_id,
                        "snapshot_version": snapshot_version,
                    },
                )
            if task["status"] == "committed":
                return {"claimed": False, "status": "committed"}
            if task["status"] == "abandoned":
                return {
                    "claimed": False,
                    "status": "abandoned",
                    "abandon_reason": (
                        json.loads(task["abandon_json"]) if task["abandon_json"] else None
                    ),
                }
            charged = bool(task["charged"])
            remaining = run["memory_budget"] - run["memory_charged"]
            if not charged and task["memory_units"] > remaining:
                return {
                    "claimed": False,
                    "status": "budget_exhausted",
                    "required_units": task["memory_units"],
                    "remaining_units": remaining,
                    "budget_units": run["memory_budget"],
                    "charged_units": run["memory_charged"],
                }
            if not charged:
                conn.execute(
                    "UPDATE plan_tasks SET charged = 1 WHERE run_id = ? "
                    "AND template_id = ? AND snapshot_version = ?",
                    (run_id, template_id, snapshot_version),
                )
                conn.execute(
                    "UPDATE plan_runs SET memory_charged = memory_charged + ? "
                    "WHERE run_id = ?",
                    (task["memory_units"], run_id),
                )
            conn.execute(
                "UPDATE plan_tasks SET status='running', error_json=NULL "
                "WHERE run_id = ? AND template_id = ? AND snapshot_version = ?",
                (run_id, template_id, snapshot_version),
            )
            return {"claimed": True, "charged": True}

    def abandon_pending_tasks(
        self,
        run_id: int,
        task_keys: list[tuple[str, int]],
        reason: dict[str, Any],
    ) -> int:
        if not task_keys:
            return 0
        count = 0
        with self.transaction() as conn:
            for template_id, snapshot_version in task_keys:
                cursor = conn.execute(
                    """
                    UPDATE plan_tasks
                    SET status='abandoned', abandon_json=?
                    WHERE run_id = ? AND template_id = ? AND snapshot_version = ?
                      AND status='pending'
                    """,
                    (
                        canonical_json(reason),
                        run_id,
                        template_id,
                        snapshot_version,
                    ),
                )
                count += cursor.rowcount
        return count

    def fail_task(
        self,
        run_id: int,
        template_id: str,
        snapshot_version: int,
        error: dict[str, Any],
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE plan_tasks
                SET status='failed', error_json=?
                WHERE run_id = ? AND template_id = ? AND snapshot_version = ?
                  AND status='running'
                """,
                (
                    canonical_json(error),
                    run_id,
                    template_id,
                    snapshot_version,
                ),
            )

    def commit_profile(
        self,
        run_id: int,
        *,
        template_id: str,
        snapshot_version: int,
        aggregate_digest: str,
        result: dict[str, Any],
    ) -> None:
        profile_digest = digest(result)
        profile_id = f"{template_id}:{snapshot_version}"
        with self.transaction() as conn:
            aggregate = conn.execute(
                "SELECT digest FROM template_aggregates "
                "WHERE template_id = ? AND snapshot_version = ?",
                (template_id, snapshot_version),
            ).fetchone()
            if aggregate is None or aggregate["digest"] != aggregate_digest:
                raise EngineError(
                    "ERR_AGGREGATE_CHANGED",
                    "aggregate changed before the profile could be committed",
                    {
                        "template_id": template_id,
                        "snapshot_version": snapshot_version,
                    },
                )
            existing = conn.execute(
                "SELECT profile_digest FROM profiles "
                "WHERE template_id = ? AND snapshot_version = ?",
                (template_id, snapshot_version),
            ).fetchone()
            if existing is not None:
                if existing["profile_digest"] != profile_digest:
                    raise EngineError(
                        "ERR_PROFILE_CONFLICT",
                        "profile already exists with different content",
                        {
                            "template_id": template_id,
                            "snapshot_version": snapshot_version,
                        },
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO profiles(
                        template_id, snapshot_version, aggregate_digest,
                        profile_digest, result_json, created_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        template_id,
                        snapshot_version,
                        aggregate_digest,
                        profile_digest,
                        canonical_json(result),
                        run_id,
                    ),
                )
            cursor = conn.execute(
                """
                UPDATE plan_tasks
                SET status='committed', profile_id=?
                WHERE run_id = ? AND template_id = ? AND snapshot_version = ?
                  AND status IN ('running','committed')
                """,
                (profile_id, run_id, template_id, snapshot_version),
            )
            if cursor.rowcount != 1:
                raise EngineError(
                    "ERR_PLAN_TASK_NOT_CLAIMED",
                    "cannot commit a task that is not currently running",
                    {
                        "run_id": run_id,
                        "template_id": template_id,
                        "snapshot_version": snapshot_version,
                    },
                )

    def finalize_run(self, run_id: int) -> str:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM plan_tasks "
                "WHERE run_id = ? GROUP BY status",
                (run_id,),
            ).fetchall()
            counts = {row["status"]: row["count"] for row in rows}
            if counts.get("running", 0) or counts.get("pending", 0):
                status = "interrupted"
            elif counts.get("failed", 0):
                status = "failed"
            elif counts.get("abandoned", 0):
                status = "completed_with_abandonment"
            elif counts.get("committed", 0) == sum(counts.values()):
                status = "completed"
            else:
                status = "completed"
            conn.execute(
                "UPDATE plan_runs SET status = ? WHERE run_id = ?", (status, run_id)
            )
            return status

    def apply_snapshot_patch(
        self,
        patch: dict[str, Any],
        *,
        model: dict[str, Any],
    ) -> dict[str, Any]:
        from .invalidation import compute_invalidation_closure

        self.require_initialized()
        try:
            with self.transaction() as conn:
                current_row = conn.execute(
                    """
                    SELECT s.snapshot_json
                    FROM stats_pointer p
                    JOIN stats_snapshots s ON s.version = p.version
                    WHERE p.singleton = 1
                    """
                ).fetchone()
                current = json.loads(current_row["snapshot_json"])
                new_snapshot, classification = merge_patch(current, patch)
                if classification == "no_op":
                    return {
                        "classification": "no_op",
                        "version": current["version"],
                        "changed": False,
                    }
                existing = conn.execute(
                    "SELECT snapshot_json FROM stats_snapshots WHERE version = ?",
                    (new_snapshot["version"],),
                ).fetchone()
                if existing is not None:
                    existing_snapshot = json.loads(existing["snapshot_json"])
                    if existing_snapshot["digest"] != new_snapshot["digest"]:
                        raise EngineError(
                            "ERR_SNAPSHOT_CONFLICT",
                            "snapshot version already exists with different content",
                            {"version": new_snapshot["version"]},
                        )
                    return {
                        "classification": "no_op",
                        "version": new_snapshot["version"],
                        "changed": False,
                    }
                conn.execute(
                    """
                    CREATE TEMP TABLE IF NOT EXISTS incoming_snapshot (
                        version INTEGER PRIMARY KEY,
                        effective_at_ms INTEGER NOT NULL,
                        digest TEXT NOT NULL,
                        covered_tables_json TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        classification TEXT NOT NULL
                    )
                    """
                )
                conn.execute("DELETE FROM incoming_snapshot")
                conn.execute(
                    """
                    INSERT INTO incoming_snapshot(
                        version, effective_at_ms, digest, covered_tables_json,
                        snapshot_json, classification
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_snapshot["version"],
                        new_snapshot["effective_at_ms"],
                        new_snapshot["digest"],
                        canonical_json(new_snapshot["covered_tables"]),
                        snapshot_json(new_snapshot),
                        classification,
                    ),
                )
                conn.execute(
                    "INSERT INTO stats_snapshots SELECT * FROM incoming_snapshot"
                )
                conn.execute(
                    "UPDATE stats_pointer SET version = ? WHERE singleton = 1",
                    (new_snapshot["version"],),
                )
                closure = compute_invalidation_closure(
                    conn,
                    base_snapshot=current,
                    new_snapshot=new_snapshot,
                    classification=classification,
                    model=model,
                )
                cursor = conn.execute(
                    """
                    INSERT INTO invalidation_events(
                        base_version, new_version, classification,
                        closure_digest, closure_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        current["version"],
                        new_snapshot["version"],
                        classification,
                        digest(closure),
                        canonical_json(closure),
                    ),
                )
                closure["event_id"] = int(cursor.lastrowid)
                return {
                    "classification": classification,
                    "version": new_snapshot["version"],
                    "effective_at_ms": new_snapshot["effective_at_ms"],
                    "digest": new_snapshot["digest"],
                    "changed": True,
                    "invalidation": closure,
                }
        except sqlite3.Error as exc:
            raise EngineError(
                "ERR_SNAPSHOT_COMMIT",
                "cannot commit statistics snapshot",
                {"error": str(exc)},
            ) from exc

    def list_invalidation_events(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM invalidation_events ORDER BY event_id"
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "base_version": row["base_version"],
                "new_version": row["new_version"],
                "classification": row["classification"],
                "closure_digest": row["closure_digest"],
                "closure": json.loads(row["closure_json"]),
            }
            for row in rows
        ]

    def count_samples(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS count FROM samples").fetchone()[
                "count"
            ]
        )

    def list_samples(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM samples ORDER BY seq"
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "logical_clock_ms": row["logical_clock_ms"],
                "sql": row["sql_text"],
                "template_id": row["template_id"],
                "window_key": row["window_key"],
                "snapshot_version": row["snapshot_version"],
                "known_snapshot_version": row["known_snapshot_version"],
                "duration_ms": row["duration_ms"],
                "scanned_rows": row["scanned_rows"],
                "returned_rows": row["returned_rows"],
                "index_hit": row["index_hit"],
                "content_digest": row["content_digest"],
                "structure": json.loads(row["structure_json"]),
            }
            for row in rows
        ]
