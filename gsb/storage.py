from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .aggregation import (
    merge_partial,
    move_channels,
    partial_ledger_digest,
    route_partial,
    state_bytes,
)
from .budget import (
    ACCELERATED_MODE,
    EXACT_MODE,
    decide_mode,
)
from .errors import (
    GSBBatchConflictError,
    GSBCorruptStateError,
    GSBIoError,
    GSBMemoryBudgetError,
    GSBStateConflictError,
)
from .hotness import HotTracker
from .models import ColumnBatch, EngineConfig, QuerySpec, Sample
from .spill import SpillManager
from .util import (
    atomic_write,
    canonical_json,
    sha256_json,
    stable_json,
)


SCHEMA_VERSION = 1


DEFAULT_STATS: dict[str, Any] = {
    "spills": 0,
    "spill_groups": 0,
    "spill_bytes": 0,
    "readbacks": 0,
    "readback_groups": 0,
    "readback_bytes": 0,
    "prepared_recovered": 0,
    "prepared_discarded": 0,
    "half_committed_repaired": 0,
    "batches_applied": 0,
    "state_bytes_peak": 0,
}


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_runs (
    query_id TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    sample_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    current_mode TEXT NOT NULL,
    elapsed_ms REAL NOT NULL,
    switch_overhead_used INTEGER NOT NULL,
    watermark_ordinal INTEGER NOT NULL,
    watermark_batch_index INTEGER,
    applied_batch_count INTEGER NOT NULL,
    hot_state_json TEXT NOT NULL,
    hot_version INTEGER NOT NULL,
    spill_active INTEGER NOT NULL,
    spill_generation INTEGER NOT NULL,
    spill_path TEXT,
    spill_digest TEXT,
    spill_groups INTEGER NOT NULL,
    spill_bytes INTEGER NOT NULL,
    stats_json TEXT NOT NULL,
    consistency_json TEXT,
    result_json TEXT,
    result_digest TEXT,
    report_json TEXT,
    output_path TEXT,
    updated_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    query_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    digest TEXT NOT NULL,
    actual_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    partials_digest TEXT NOT NULL,
    logical_ms REAL,
    mode TEXT,
    staged_by TEXT NOT NULL,
    PRIMARY KEY (query_id, ordinal),
    UNIQUE (query_id, batch_index)
);

CREATE TABLE IF NOT EXISTS batch_partials (
    query_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    group_token TEXT NOT NULL,
    key_json TEXT NOT NULL,
    sort_key TEXT NOT NULL,
    state_json TEXT NOT NULL,
    rows INTEGER NOT NULL,
    PRIMARY KEY (query_id, ordinal, group_token),
    FOREIGN KEY (query_id, ordinal)
        REFERENCES batches(query_id, ordinal) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS group_states (
    query_id TEXT NOT NULL,
    group_token TEXT NOT NULL,
    key_json TEXT NOT NULL,
    sort_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    PRIMARY KEY (query_id, group_token)
);

CREATE TABLE IF NOT EXISTS group_state_versions (
    query_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    group_token TEXT NOT NULL,
    version INTEGER NOT NULL,
    delta_digest TEXT NOT NULL,
    PRIMARY KEY (query_id, ordinal, group_token)
);

CREATE TABLE IF NOT EXISTS hot_events (
    query_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    trigger_batch_index INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (query_id, version)
);

CREATE TABLE IF NOT EXISTS mode_events (
    query_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    mode TEXT NOT NULL,
    previous_mode TEXT NOT NULL,
    elapsed_before_ms REAL NOT NULL,
    elapsed_after_ms REAL NOT NULL,
    switched INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (query_id, ordinal)
);

CREATE TABLE IF NOT EXISTS spill_events (
    query_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    groups INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    digest TEXT NOT NULL,
    path TEXT NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (query_id, generation)
);

CREATE TABLE IF NOT EXISTS resource_leases (
    lease_id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    created_ns INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_status
    ON batches(query_id, status, ordinal);
CREATE INDEX IF NOT EXISTS idx_versions_batch
    ON group_state_versions(query_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_leases_query
    ON resource_leases(query_id);
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Storage:
    def __init__(self, db_path: Path, work_dir: Path) -> None:
        self.db_path = db_path.resolve()
        self.work_dir = work_dir.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self._ensure_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                conn.execute("INSERT INTO meta(key, value) VALUES('seq', '0')")
            elif int(row["value"]) != SCHEMA_VERSION:
                raise GSBIoError(
                    "unsupported SQLite state schema version",
                    {"found": row["value"], "expected": SCHEMA_VERSION},
                )

    def _next_seq(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
        value = int(row["value"]) + 1 if row is not None else 1
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
        )
        return value

    @staticmethod
    def _initial_hot_state(config: EngineConfig) -> dict[str, Any]:
        return HotTracker(config.hot_key_min_rows, config.skew_ratio).to_dict()

    def initialize_query(self, sample: Sample, query: QuerySpec) -> sqlite3.Row:
        spec_json = canonical_json(query.to_dict())
        spec_hash = sha256_json(query.to_dict())
        config_json = canonical_json(sample.config.to_dict())
        config_hash = sha256_json(sample.config.to_dict())
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM query_runs WHERE query_id = ?",
                (query.query_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["spec_hash"] != spec_hash
                    or row["config_hash"] != config_hash
                    or row["sample_fingerprint"] != sample.fingerprint
                ):
                    raise GSBStateConflictError(
                        "persisted query state does not match the current sample",
                        {
                            "query_id": query.query_id,
                            "stored_spec_hash": row["spec_hash"],
                            "current_spec_hash": spec_hash,
                            "stored_config_hash": row["config_hash"],
                            "current_config_hash": config_hash,
                        },
                    )
                return row
            seq = self._next_seq(conn)
            conn.execute(
                """
                INSERT INTO query_runs(
                    query_id, spec_json, spec_hash, config_json, config_hash,
                    sample_fingerprint, status, current_mode, elapsed_ms,
                    switch_overhead_used, watermark_ordinal, watermark_batch_index,
                    applied_batch_count, hot_state_json, hot_version,
                    spill_active, spill_generation, spill_path, spill_digest,
                    spill_groups, spill_bytes, stats_json, consistency_json,
                    result_json, result_digest, report_json, output_path, updated_seq
                ) VALUES(?, ?, ?, ?, ?, ?, 'running', ?, 0.0, 0, 0, NULL, 0, ?,
                         0, 0, 0, NULL, NULL, 0, 0, ?, NULL, NULL, NULL, NULL, NULL, ?)
                """,
                (
                    query.query_id,
                    spec_json,
                    spec_hash,
                    config_json,
                    config_hash,
                    sample.fingerprint,
                    EXACT_MODE,
                    canonical_json(self._initial_hot_state(sample.config)),
                    canonical_json(DEFAULT_STATS),
                    seq,
                ),
            )
            row = conn.execute(
                "SELECT * FROM query_runs WHERE query_id = ?",
                (query.query_id,),
            ).fetchone()
            assert row is not None
            return row

    def get_query(self, query_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM query_runs WHERE query_id = ?",
            (query_id,),
        ).fetchone()

    def query_ids(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT query_id FROM query_runs ORDER BY query_id"
        ).fetchall()
        return [row["query_id"] for row in rows]

    def stage_batch(
        self,
        query: QuerySpec,
        batch: ColumnBatch,
        partial: dict[str, dict[str, Any]],
        worker_id: str,
    ) -> str:
        digest = partial_ledger_digest(partial)
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM batches WHERE query_id = ? AND ordinal = ?",
                (query.query_id, batch.ordinal),
            ).fetchone()
            if existing is not None:
                if existing["actual_digest"] != batch.actual_digest:
                    raise GSBBatchConflictError(
                        "a different batch digest is already staged at this ordinal",
                        {
                            "query_id": query.query_id,
                            "ordinal": batch.ordinal,
                            "stored_digest": existing["actual_digest"],
                            "incoming_digest": batch.actual_digest,
                        },
                    )
                if existing["status"] == "committed":
                    return "already_committed"
                if existing["partials_digest"] == digest:
                    return "already_prepared"
                conn.execute(
                    "DELETE FROM batch_partials WHERE query_id = ? AND ordinal = ?",
                    (query.query_id, batch.ordinal),
                )
                conn.execute(
                    "DELETE FROM batches WHERE query_id = ? AND ordinal = ?",
                    (query.query_id, batch.ordinal),
                )
            conn.execute(
                """
                INSERT INTO batches(
                    query_id, ordinal, batch_index, row_count, digest,
                    actual_digest, status, partials_digest, logical_ms, mode, staged_by
                ) VALUES(?, ?, ?, ?, ?, ?, 'prepared', ?, NULL, NULL, ?)
                """,
                (
                    query.query_id,
                    batch.ordinal,
                    batch.batch_index,
                    batch.row_count,
                    batch.digest,
                    batch.actual_digest,
                    digest,
                    worker_id,
                ),
            )
            for token in sorted(partial, key=lambda item: partial[item]["sort_key"]):
                state = partial[token]
                conn.execute(
                    """
                    INSERT INTO batch_partials(
                        query_id, ordinal, group_token, key_json, sort_key,
                        state_json, rows
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        query.query_id,
                        batch.ordinal,
                        token,
                        canonical_json(state["key"]),
                        state["sort_key"],
                        canonical_json(state),
                        int(state["rows"]),
                    ),
                )
        return "staged"

    def recover_prepared(self, query: QuerySpec) -> dict[str, int]:
        recovered = 0
        discarded = 0
        half_committed = 0
        with self.transaction() as conn:
            half_rows = conn.execute(
                """
                SELECT g.query_id, g.ordinal, g.group_token
                FROM group_state_versions g
                JOIN batches b
                  ON b.query_id = g.query_id AND b.ordinal = g.ordinal
                WHERE g.query_id = ? AND b.status != 'committed'
                """,
                (query.query_id,),
            ).fetchall()
            if half_rows:
                half_committed = len(half_rows)
                conn.execute(
                    """
                    DELETE FROM group_state_versions
                    WHERE query_id = ? AND ordinal IN (
                        SELECT ordinal FROM batches
                        WHERE query_id = ? AND status != 'committed'
                    )
                    """,
                    (query.query_id, query.query_id),
                )
            rows = conn.execute(
                "SELECT * FROM batches WHERE query_id = ? AND status = 'prepared' "
                "ORDER BY ordinal",
                (query.query_id,),
            ).fetchall()
            for row in rows:
                partials = self._load_partial_rows(
                    conn, query.query_id, row["ordinal"]
                )
                digest = partial_ledger_digest(partials)
                if digest == row["partials_digest"]:
                    recovered += 1
                else:
                    conn.execute(
                        "DELETE FROM batch_partials WHERE query_id = ? AND ordinal = ?",
                        (query.query_id, row["ordinal"]),
                    )
                    conn.execute(
                        "DELETE FROM batches WHERE query_id = ? AND ordinal = ?",
                        (query.query_id, row["ordinal"]),
                    )
                    discarded += 1
            query_row = conn.execute(
                "SELECT * FROM query_runs WHERE query_id = ?",
                (query.query_id,),
            ).fetchone()
            if query_row is None:
                raise GSBStateConflictError("query state is missing", {"query_id": query.query_id})
            stats = json.loads(query_row["stats_json"])
            stats["prepared_recovered"] = int(stats.get("prepared_recovered", 0)) + recovered
            stats["prepared_discarded"] = int(stats.get("prepared_discarded", 0)) + discarded
            stats["half_committed_repaired"] = (
                int(stats.get("half_committed_repaired", 0)) + half_committed
            )
            conn.execute(
                "UPDATE query_runs SET stats_json = ?, updated_seq = ? WHERE query_id = ?",
                (canonical_json(stats), self._next_seq(conn), query.query_id),
            )
        return {
            "recovered": recovered,
            "discarded": discarded,
            "half_committed_repaired": half_committed,
        }

    @staticmethod
    def _load_partial_rows(
        conn: sqlite3.Connection,
        query_id: str,
        ordinal: int,
    ) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM batch_partials WHERE query_id = ? AND ordinal = ?",
            (query_id, ordinal),
        ).fetchall()
        return {
            row["group_token"]: json.loads(row["state_json"])
            for row in rows
        }

    def next_prepared_ordinal(self, query: QuerySpec) -> int | None:
        query_row = self.get_query(query.query_id)
        if query_row is None:
            return None
        row = self.connection.execute(
            "SELECT ordinal FROM batches WHERE query_id = ? AND ordinal = ? "
            "AND status = 'prepared'",
            (query.query_id, int(query_row["watermark_ordinal"]) + 1),
        ).fetchone()
        return int(row["ordinal"]) if row is not None else None

    def _load_group_rows(
        self,
        conn: sqlite3.Connection,
        query_id: str,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, int],
    ]:
        normal: dict[str, dict[str, Any]] = {}
        hot: dict[str, dict[str, Any]] = {}
        versions: dict[str, int] = {}
        rows = conn.execute(
            "SELECT * FROM group_states WHERE query_id = ? ORDER BY sort_key",
            (query_id,),
        ).fetchall()
        for row in rows:
            state = json.loads(row["state_json"])
            if row["channel"] == "hot":
                hot[row["group_token"]] = state
            else:
                normal[row["group_token"]] = state
            versions[row["group_token"]] = int(row["version"])
        return normal, hot, versions

    def _load_spill_state(
        self,
        conn: sqlite3.Connection,
        record: sqlite3.Row,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, int],
        int,
        int,
        dict[str, Any],
    ]:
        if not record["spill_active"] or not record["spill_path"]:
            empty: dict[str, dict[str, Any]] = {}
            return empty, {}, {}, 0, 0, json.loads(record["stats_json"])
        manager = SpillManager(self.work_dir, record["query_id"])
        entries, byte_count = manager.read(
            Path(record["spill_path"]),
            str(record["spill_digest"]),
        )
        normal: dict[str, dict[str, Any]] = {}
        hot: dict[str, dict[str, Any]] = {}
        versions: dict[str, int] = {}
        for entry in entries:
            token = entry["group_token"]
            state = entry["state"]
            versions[token] = int(entry["version"])
            if entry["channel"] == "hot":
                hot[token] = state
            else:
                normal[token] = state
        stats = json.loads(record["stats_json"])
        stats["readbacks"] = int(stats.get("readbacks", 0)) + 1
        stats["readback_groups"] = int(stats.get("readback_groups", 0)) + len(entries)
        stats["readback_bytes"] = int(stats.get("readback_bytes", 0)) + byte_count
        return normal, hot, versions, len(entries), byte_count, stats

    def _snapshot_entries(
        self,
        normal: dict[str, dict[str, Any]],
        hot: dict[str, dict[str, Any]],
        versions: dict[str, int],
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for channel, groups in (("normal", normal), ("hot", hot)):
            for token in sorted(groups, key=lambda item: groups[item]["sort_key"]):
                state = groups[token]
                entries.append(
                    {
                        "group_token": token,
                        "key": state["key"],
                        "sort_key": state["sort_key"],
                        "channel": channel,
                        "version": int(versions.get(token, 0)),
                        "state": state,
                    }
                )
        entries.sort(key=lambda entry: (entry["sort_key"], entry["group_token"]))
        return entries

    def _persist_state(
        self,
        conn: sqlite3.Connection,
        record: sqlite3.Row,
        query: QuerySpec,
        config: EngineConfig,
        batch: ColumnBatch,
        normal: dict[str, dict[str, Any]],
        hot: dict[str, dict[str, Any]],
        versions: dict[str, int],
        stats: dict[str, Any],
    ) -> tuple[bool, int, bool]:
        total_bytes = state_bytes(normal, "normal") + state_bytes(hot, "hot")
        stats["state_bytes_peak"] = max(int(stats.get("state_bytes_peak", 0)), total_bytes)
        spilled = total_bytes > config.memory_limit_bytes
        spill_active = int(record["spill_active"])
        spill_generation = int(record["spill_generation"])
        if spilled:
            generation = spill_generation + 1
            entries = self._snapshot_entries(normal, hot, versions)
            manager = SpillManager(self.work_dir, query.query_id)
            path, digest, byte_count = manager.write(generation, entries)
            spill_active = 1
            spill_generation = generation
            stats["spills"] = int(stats.get("spills", 0)) + 1
            stats["spill_groups"] = int(stats.get("spill_groups", 0)) + len(entries)
            stats["spill_bytes"] = int(stats.get("spill_bytes", 0)) + byte_count
            event = {
                "generation": generation,
                "trigger_ordinal": batch.ordinal,
                "trigger_batch_index": batch.batch_index,
                "groups": len(entries),
                "bytes": byte_count,
                "digest": digest,
                "path": str(path),
                "memory_limit_bytes": config.memory_limit_bytes,
                "observed_state_bytes": total_bytes,
            }
            conn.execute(
                """
                INSERT INTO spill_events(
                    query_id, generation, ordinal, batch_index, groups,
                    bytes, digest, path, event_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query.query_id,
                    generation,
                    batch.ordinal,
                    batch.batch_index,
                    len(entries),
                    byte_count,
                    digest,
                    str(path),
                    canonical_json(event),
                ),
            )
            conn.execute(
                "DELETE FROM group_states WHERE query_id = ?",
                (query.query_id,),
            )
        else:
            conn.execute(
                "DELETE FROM group_states WHERE query_id = ?",
                (query.query_id,),
            )
            for channel, groups in (("normal", normal), ("hot", hot)):
                for token in sorted(groups, key=lambda item: groups[item]["sort_key"]):
                    state = groups[token]
                    conn.execute(
                        """
                        INSERT INTO group_states(
                            query_id, group_token, key_json, sort_key,
                            channel, version, state_json
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            query.query_id,
                            token,
                            canonical_json(state["key"]),
                            state["sort_key"],
                            channel,
                            int(versions.get(token, 0)),
                            canonical_json(state),
                        ),
                    )
            spill_active = 0
        return spilled, spill_generation, bool(spill_active)

    def apply_next_ready(
        self,
        sample: Sample,
        query: QuerySpec,
        worker_id: str,
        lease_bytes: int,
        crash_hook: Any = None,
    ) -> dict[str, Any] | None:
        next_ordinal = self.next_prepared_ordinal(query)
        if next_ordinal is None:
            return None
        lease = self.acquire_memory_lease(
            query.query_id,
            lease_bytes,
            sample.config.memory_limit_bytes,
        )
        try:
            with self.transaction() as conn:
                record = conn.execute(
                    "SELECT * FROM query_runs WHERE query_id = ?",
                    (query.query_id,),
                ).fetchone()
                if record is None:
                    raise GSBStateConflictError(
                        "query state disappeared",
                        {"query_id": query.query_id},
                    )
                watermark = int(record["watermark_ordinal"])
                if next_ordinal != watermark + 1:
                    return None
                batch_row = conn.execute(
                    "SELECT * FROM batches WHERE query_id = ? AND ordinal = ? "
                    "AND status = 'prepared'",
                    (query.query_id, next_ordinal),
                ).fetchone()
                if batch_row is None:
                    return None
                partial = self._load_partial_rows(conn, query.query_id, next_ordinal)
                if partial_ledger_digest(partial) != batch_row["partials_digest"]:
                    raise GSBCorruptStateError(
                        "batch partial ledger digest mismatch",
                        {
                            "query_id": query.query_id,
                            "ordinal": next_ordinal,
                        },
                    )
                batch = sample.batches[next_ordinal - 1]
                if int(batch_row["batch_index"]) != batch.batch_index:
                    raise GSBBatchConflictError(
                        "staged batch index does not match sample plan",
                        {
                            "ordinal": next_ordinal,
                            "staged": batch_row["batch_index"],
                            "sample": batch.batch_index,
                        },
                    )
                if int(record["spill_active"]):
                    normal, hot, versions, _, _, stats = self._load_spill_state(conn, record)
                else:
                    normal, hot, versions = self._load_group_rows(conn, query.query_id)
                    stats = json.loads(record["stats_json"])
                tracker_state = json.loads(record["hot_state_json"])
                tracker = HotTracker(
                    sample.config.hot_key_min_rows,
                    sample.config.skew_ratio,
                    tracker_state,
                )
                hot_event = tracker.step(batch, partial)
                rows_by_ordinal = {
                    item.ordinal: item.row_count for item in sample.batches
                }
                decision = decide_mode(
                    sample.config,
                    str(record["current_mode"]),
                    float(record["elapsed_ms"]),
                    next_ordinal,
                    rows_by_ordinal,
                    bool(record["switch_overhead_used"]),
                )
                accelerated = decision.mode == ACCELERATED_MODE
                hot_tokens = set(tracker.ever_hot)
                if accelerated:
                    move_channels(normal, hot, hot_tokens)
                    route_partial(partial, normal, hot, hot_tokens, True, query)
                else:
                    merge_partial(normal, partial, query)
                for token in partial:
                    versions[token] = next_ordinal
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO group_state_versions(
                            query_id, ordinal, batch_index, group_token,
                            version, delta_digest
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            query.query_id,
                            next_ordinal,
                            batch.batch_index,
                            token,
                            next_ordinal,
                            batch_row["partials_digest"],
                        ),
                    )
                stats["batches_applied"] = int(stats.get("batches_applied", 0)) + 1
                switch_overhead_used = bool(record["switch_overhead_used"])
                if decision.switched:
                    switch_overhead_used = True
                spilled, generation, spill_active = self._persist_state(
                    conn,
                    record,
                    query,
                    sample.config,
                    batch,
                    normal,
                    hot,
                    versions,
                    stats,
                )
                logical_ms = decision.elapsed_after_ms - decision.elapsed_before_ms
                conn.execute(
                    """
                    UPDATE batches SET status = 'committed', logical_ms = ?, mode = ?
                    WHERE query_id = ? AND ordinal = ?
                    """,
                    (
                        logical_ms,
                        decision.mode,
                        query.query_id,
                        next_ordinal,
                    ),
                )
                event = decision.to_dict()
                event["ordinal"] = next_ordinal
                event["batch_index"] = batch.batch_index
                event["logical_ms"] = logical_ms
                conn.execute(
                    """
                    INSERT OR REPLACE INTO mode_events(
                        query_id, ordinal, batch_index, mode, previous_mode,
                        elapsed_before_ms, elapsed_after_ms, switched, event_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        query.query_id,
                        next_ordinal,
                        batch.batch_index,
                        decision.mode,
                        decision.previous_mode,
                        decision.elapsed_before_ms,
                        decision.elapsed_after_ms,
                        1 if decision.switched else 0,
                        canonical_json(event),
                    ),
                )
                if hot_event is not None:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO hot_events(
                            query_id, version, trigger_batch_index, event_json
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (
                            query.query_id,
                            hot_event["version"],
                            hot_event["trigger_batch_index"],
                            canonical_json(hot_event),
                        ),
                    )
                conn.execute(
                    """
                    UPDATE query_runs SET
                        status = 'running',
                        current_mode = ?,
                        elapsed_ms = ?,
                        switch_overhead_used = ?,
                        watermark_ordinal = ?,
                        watermark_batch_index = ?,
                        applied_batch_count = ?,
                        hot_state_json = ?,
                        hot_version = ?,
                        spill_active = ?,
                        spill_generation = ?,
                        spill_path = ?,
                        spill_digest = ?,
                        spill_groups = ?,
                        spill_bytes = ?,
                        stats_json = ?,
                        updated_seq = ?
                    WHERE query_id = ?
                    """,
                    (
                        decision.mode,
                        decision.elapsed_after_ms,
                        1 if switch_overhead_used else 0,
                        next_ordinal,
                        batch.batch_index,
                        int(record["applied_batch_count"]) + 1,
                        canonical_json(tracker.to_dict()),
                        tracker.version,
                        1 if spill_active else 0,
                        generation,
                        record["spill_path"] if not spilled else self._current_spill_path(
                            query.query_id, generation
                        ),
                        record["spill_digest"] if not spilled else None,
                        int(record["spill_groups"]) if not spilled else 0,
                        int(record["spill_bytes"]) if not spilled else 0,
                        canonical_json(stats),
                        self._next_seq(conn),
                        query.query_id,
                    ),
                )
                if spilled:
                    metadata = conn.execute(
                        "SELECT path, digest, groups, bytes FROM spill_events "
                        "WHERE query_id = ? AND generation = ?",
                        (query.query_id, generation),
                    ).fetchone()
                    assert metadata is not None
                    conn.execute(
                        "UPDATE query_runs SET spill_path = ?, spill_digest = ?, "
                        "spill_groups = ?, spill_bytes = ? WHERE query_id = ?",
                        (
                            metadata["path"],
                            metadata["digest"],
                            metadata["groups"],
                            metadata["bytes"],
                            query.query_id,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE query_runs SET spill_path = NULL, spill_digest = NULL, "
                        "spill_groups = 0, spill_bytes = 0 WHERE query_id = ?",
                        (query.query_id,),
                    )
                if crash_hook is not None:
                    crash_hook("apply_after_state_before_commit", next_ordinal, batch.batch_index)
            if crash_hook is not None:
                crash_hook("apply_after_commit", next_ordinal, batch.batch_index)
            return {
                "ordinal": next_ordinal,
                "batch_index": batch.batch_index,
                "mode": decision.mode,
                "switched": decision.switched,
                "elapsed_ms": decision.elapsed_after_ms,
                "spilled": spilled,
                "spill_generation": generation,
            }
        finally:
            self.release_memory_lease(lease)

    def _current_spill_path(self, query_id: str, generation: int) -> str:
        return str(SpillManager(self.work_dir, query_id).path_for_generation(generation))

    def acquire_memory_lease(
        self,
        query_id: str,
        bytes_requested: int,
        limit_bytes: int,
        timeout_seconds: float = 5.0,
    ) -> str:
        lock_path = self.work_dir / "locks" / "memory.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        requested = max(1, min(int(bytes_requested), int(limit_bytes)))
        deadline = time.monotonic() + timeout_seconds
        while True:
            with lock_path.open("a+b") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    with self.transaction() as conn:
                        leases = conn.execute(
                            "SELECT * FROM resource_leases WHERE query_id = ?",
                            (query_id,),
                        ).fetchall()
                        used = 0
                        for lease in leases:
                            if not _pid_alive(int(lease["pid"])):
                                conn.execute(
                                    "DELETE FROM resource_leases WHERE lease_id = ?",
                                    (lease["lease_id"],),
                                )
                                continue
                            used += int(lease["bytes"])
                        if used + requested <= limit_bytes:
                            lease_id = f"{os.getpid()}:{uuid.uuid4().hex}"
                            conn.execute(
                                """
                                INSERT INTO resource_leases(
                                    lease_id, query_id, pid, bytes, created_ns
                                ) VALUES(?, ?, ?, ?, ?)
                                """,
                                (
                                    lease_id,
                                    query_id,
                                    os.getpid(),
                                    requested,
                                    time.monotonic_ns(),
                                ),
                            )
                            return lease_id
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            if time.monotonic() >= deadline:
                raise GSBMemoryBudgetError(
                    "memory budget is fully leased by concurrent workers",
                    {
                        "query_id": query_id,
                        "requested_bytes": requested,
                        "limit_bytes": limit_bytes,
                    },
                )
            time.sleep(0.02)

    def release_memory_lease(self, lease_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM resource_leases WHERE lease_id = ?",
                (lease_id,),
            )

    def apply_all_ready(
        self,
        sample: Sample,
        query: QuerySpec,
        worker_id: str,
        crash_hook: Any = None,
    ) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        while True:
            next_ordinal = self.next_prepared_ordinal(query)
            if next_ordinal is None:
                break
            partial = self.connection.execute(
                "SELECT COALESCE(SUM(rows), 0) AS rows FROM batch_partials "
                "WHERE query_id = ? AND ordinal = ?",
                (query.query_id, next_ordinal),
            ).fetchone()
            current_state_size = self.connection.execute(
                "SELECT COALESCE(SUM(length(state_json) + length(group_token) + 96), 0) "
                "AS bytes FROM group_states WHERE query_id = ?",
                (query.query_id,),
            ).fetchone()["bytes"]
            query_row = self.get_query(query.query_id)
            assert query_row is not None
            if query_row["spill_active"]:
                current_state_size = max(
                    current_state_size,
                    int(query_row["spill_bytes"] or 0),
                )
            lease_bytes = max(
                4096,
                int(current_state_size) + int(partial["rows"] or 0) * 96,
            )
            result = self.apply_next_ready(
                sample,
                query,
                worker_id,
                lease_bytes,
                crash_hook,
            )
            if result is None:
                break
            applied.append(result)
        return applied

    def committed_batches(self, query_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM batches WHERE query_id = ? AND status = 'committed' "
            "ORDER BY ordinal",
            (query_id,),
        ).fetchall()

    def staged_batches(self, query_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM batches WHERE query_id = ? ORDER BY ordinal",
            (query_id,),
        ).fetchall()

    def mode_events(self, query_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM mode_events WHERE query_id = ? ORDER BY ordinal",
            (query_id,),
        ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]

    def hot_events(self, query_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM hot_events WHERE query_id = ? ORDER BY version",
            (query_id,),
        ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]

    def spill_events(self, query_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM spill_events WHERE query_id = ? ORDER BY generation",
            (query_id,),
        ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]

    def mode_by_ordinal(self, query_id: str) -> dict[int, str]:
        rows = self.connection.execute(
            "SELECT ordinal, mode FROM batches WHERE query_id = ? AND status = 'committed'",
            (query_id,),
        ).fetchall()
        return {int(row["ordinal"]): str(row["mode"]) for row in rows}

    def load_durable_state(
        self,
        query: QuerySpec,
        mutate_stats: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
        record = self.get_query(query.query_id)
        if record is None:
            raise GSBStateConflictError("query state is missing", {"query_id": query.query_id})
        if mutate_stats:
            with self.transaction() as conn:
                record = conn.execute(
                    "SELECT * FROM query_runs WHERE query_id = ?",
                    (query.query_id,),
                ).fetchone()
                assert record is not None
                if int(record["spill_active"]):
                    normal, hot, _, _, _, stats = self._load_spill_state(conn, record)
                    conn.execute(
                        "UPDATE query_runs SET stats_json = ?, updated_seq = ? "
                        "WHERE query_id = ?",
                        (canonical_json(stats), self._next_seq(conn), query.query_id),
                    )
                else:
                    normal, hot, _ = self._load_group_rows(conn, query.query_id)
                    stats = json.loads(record["stats_json"])
                return normal, hot, stats
        if int(record["spill_active"]):
            normal, hot, _, _, _, stats = self._load_spill_state(
                self.connection, record
            )
            return normal, hot, stats
        normal, hot, _ = self._load_group_rows(self.connection, query.query_id)
        return normal, hot, json.loads(record["stats_json"])

    def load_partials_for_committed(
        self,
        query_id: str,
    ) -> list[tuple[sqlite3.Row, dict[str, dict[str, Any]]]]:
        batches = self.committed_batches(query_id)
        result: list[tuple[sqlite3.Row, dict[str, dict[str, Any]]]] = []
        for batch in batches:
            partial = self._load_partial_rows(
                self.connection, query_id, int(batch["ordinal"])
            )
            result.append((batch, partial))
        return result

    def finalize_query(
        self,
        query: QuerySpec,
        result: dict[str, Any],
        report: dict[str, Any],
        output_path: Path,
        consistency: dict[str, Any],
        result_digest: str,
        status: str = "complete",
    ) -> None:
        with self.transaction() as conn:
            seq = self._next_seq(conn)
            conn.execute(
                """
                UPDATE query_runs SET
                    status = ?, result_json = ?, result_digest = ?,
                    report_json = ?, output_path = ?, consistency_json = ?,
                    updated_seq = ?
                WHERE query_id = ?
                """,
                (
                    status,
                    canonical_json(result),
                    result_digest,
                    canonical_json(report),
                    str(output_path),
                    canonical_json(consistency),
                    seq,
                    query.query_id,
                ),
            )

    def mark_failed(self, query_id: str, message: str) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT report_json FROM query_runs WHERE query_id = ?",
                (query_id,),
            ).fetchone()
            report = json.loads(row["report_json"]) if row and row["report_json"] else {}
            report["status"] = "failed"
            report["failure"] = message
            conn.execute(
                "UPDATE query_runs SET status = 'failed', report_json = ?, updated_seq = ? "
                "WHERE query_id = ?",
                (canonical_json(report), self._next_seq(conn), query_id),
            )

    def write_result_file(self, path: Path, payload: dict[str, Any]) -> str:
        data = stable_json(payload).encode("utf-8")
        atomic_write(path, data)
        return sha256_json(payload)

    def stats(self, query_id: str) -> dict[str, Any]:
        row = self.get_query(query_id)
        if row is None:
            return {}
        return json.loads(row["stats_json"])

    def lease_summary(self, query_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM resource_leases WHERE query_id = ? ORDER BY lease_id",
            (query_id,),
        ).fetchall()
        return {
            "active_leases": len(rows),
            "leased_bytes": sum(int(row["bytes"]) for row in rows),
            "pids": sorted(int(row["pid"]) for row in rows),
        }

