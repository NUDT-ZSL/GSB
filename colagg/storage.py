from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .canonical import canonical_dumps, sha256_json, strict_json_loads
from .errors import (
    E_BATCH_CONFLICT,
    E_IO,
    E_NOT_FOUND,
    E_PLAN_CONFLICT,
    E_RECOVERY_FAILED,
    EngineError,
)
from .models import (
    Batch,
    ExecutionConfig,
    InjectionManifest,
    Plan,
    Query,
)
from .validation import parse_batch, parse_config, parse_manifest, parse_query


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    query_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    plan_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    mode TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    batch_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_digest TEXT,
    error_code TEXT,
    error_json TEXT,
    committed_clock INTEGER,
    PRIMARY KEY (mode, batch_seq)
);

CREATE TABLE IF NOT EXISTS group_states (
    mode TEXT NOT NULL,
    group_hash TEXT NOT NULL,
    group_key_json TEXT NOT NULL,
    version INTEGER NOT NULL,
    state_json TEXT,
    state_digest TEXT,
    spill_id TEXT,
    status TEXT NOT NULL,
    last_touch INTEGER NOT NULL,
    PRIMARY KEY (mode, group_hash)
);

CREATE TABLE IF NOT EXISTS group_commits (
    mode TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    group_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (mode, batch_seq, group_hash)
);

CREATE TABLE IF NOT EXISTS spill_files (
    spill_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    group_hash TEXT NOT NULL,
    group_version INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_clock INTEGER NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (mode, group_hash, group_version, spill_id)
);

CREATE TABLE IF NOT EXISTS spill_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    group_hash TEXT NOT NULL,
    direction TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    start_clock INTEGER NOT NULL,
    end_clock INTEGER NOT NULL,
    duration_ticks INTEGER NOT NULL,
    reused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS results (
    mode TEXT NOT NULL,
    kind TEXT NOT NULL,
    group_hash TEXT NOT NULL,
    group_key_json TEXT NOT NULL,
    item_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    window_start INTEGER,
    window_end INTEGER,
    value_json TEXT NOT NULL,
    PRIMARY KEY (mode, kind, group_hash, item_id)
);

CREATE TABLE IF NOT EXISTS failures (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    phase TEXT NOT NULL,
    error_code TEXT NOT NULL,
    column_name TEXT,
    group_hash TEXT,
    group_key_json TEXT,
    details_json TEXT NOT NULL,
    logical_time INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_failures (
    mode TEXT NOT NULL,
    group_hash TEXT NOT NULL,
    group_key_json TEXT NOT NULL,
    error_code TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    phase TEXT NOT NULL,
    details_json TEXT NOT NULL,
    logical_time INTEGER NOT NULL,
    PRIMARY KEY (mode, group_hash)
);

CREATE TABLE IF NOT EXISTS conversion_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    row_index INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    raw_repr TEXT NOT NULL,
    scale INTEGER NOT NULL,
    scaled INTEGER,
    direction TEXT NOT NULL,
    nonfinite TEXT,
    UNIQUE (mode, batch_seq, row_index, column_name)
);

CREATE INDEX IF NOT EXISTS idx_batches_status
    ON batches (mode, status, batch_seq);
CREATE INDEX IF NOT EXISTS idx_results_group
    ON results (mode, group_hash, kind, item_id);
CREATE INDEX IF NOT EXISTS idx_failures_batch
    ON failures (mode, batch_seq, failure_id);
"""


class Store:
    def __init__(self, db_path: str | Path, root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.root = Path(root).resolve()
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not open")
        return self._conn

    def open(self) -> "Store":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Store":
        if self._conn is None:
            self.open()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def initialize(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        with self.transaction():
            defaults = {
                "schema_version": str(SCHEMA_VERSION),
                "logical_clock": "0",
                "watermark": "0",
                "state_digest": "",
                "loaded_batches": "0",
            }
            for key, value in defaults.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (key, value),
                )

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def clock(self) -> int:
        return int(self.get_meta("logical_clock", "0") or 0)

    def advance_clock(self, ticks: int) -> tuple[int, int]:
        start = self.clock()
        end = start + max(1, int(ticks))
        self.set_meta("logical_clock", str(end))
        return start, end

    def register_plan(self, plan: Plan, batches: Sequence[Batch]) -> dict[str, Any]:
        mode = plan.config.numeric_mode
        query_json = canonical_dumps(plan.query.as_payload())
        config_json = canonical_dumps(plan.config.as_payload())
        manifest_json = canonical_dumps(plan.manifest.as_payload())
        inserted = 0
        skipped = 0
        with self.transaction():
            row = self.conn.execute("SELECT * FROM plan WHERE id = 1").fetchone()
            if row is not None:
                if (
                    row["plan_digest"] != plan.plan_digest
                    or row["mode"] != mode
                ):
                    raise EngineError(
                        E_PLAN_CONFLICT,
                        "workspace already contains a different plan",
                        stored_plan_digest=row["plan_digest"],
                        incoming_plan_digest=plan.plan_digest,
                        stored_mode=row["mode"],
                        incoming_mode=mode,
                    )
            else:
                self.conn.execute(
                    """
                    INSERT INTO plan
                        (id, mode, query_json, config_json, manifest_json, plan_digest)
                    VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (mode, query_json, config_json, manifest_json, plan.plan_digest),
                )

            for batch in batches:
                existing = self.conn.execute(
                    "SELECT * FROM batches WHERE mode = ? AND batch_seq = ?",
                    (mode, batch.batch_seq),
                ).fetchone()
                if existing is not None:
                    if existing["batch_digest"] != batch.digest:
                        raise EngineError(
                            E_BATCH_CONFLICT,
                            "batch sequence was loaded with a different digest",
                            batch_seq=batch.batch_seq,
                            stored_digest=existing["batch_digest"],
                            incoming_digest=batch.digest,
                        )
                    skipped += 1
                    continue
                watermark = int(self.get_meta("watermark", "0") or 0)
                if batch.batch_seq <= watermark:
                    raise EngineError(
                        E_BATCH_CONFLICT,
                        "cannot insert a batch at or below the committed watermark",
                        batch_seq=batch.batch_seq,
                        watermark=watermark,
                    )
                self.conn.execute(
                    """
                    INSERT INTO batches
                        (mode, batch_seq, row_count, batch_digest, payload_json,
                         status, result_digest, error_code, error_json, committed_clock)
                    VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, NULL)
                    """,
                    (
                        mode,
                        batch.batch_seq,
                        batch.row_count,
                        batch.digest,
                        canonical_dumps(batch.payload),
                    ),
                )
                inserted += 1
            self.set_meta("loaded_batches", str(self._count_batches()))
            self._update_state_digest()
        return {
            "mode": mode,
            "plan_digest": plan.plan_digest,
            "batches_inserted": inserted,
            "batches_already_present": skipped,
            "batch_count": self._count_batches(),
        }

    def _count_batches(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()
        return int(row["n"])

    def load_plan(self) -> Plan:
        row = self.conn.execute("SELECT * FROM plan WHERE id = 1").fetchone()
        if row is None:
            raise EngineError(E_NOT_FOUND, "workspace has no loaded plan")
        query = parse_query(strict_json_loads(row["query_json"]))
        config = parse_config(strict_json_loads(row["config_json"]))
        manifest = parse_manifest(strict_json_loads(row["manifest_json"]))
        plan = Plan.build(query, config, manifest)
        if plan.plan_digest != row["plan_digest"]:
            raise EngineError(
                E_RECOVERY_FAILED,
                "stored plan bytes do not match plan_digest",
                stored_digest=row["plan_digest"],
                computed_digest=plan.plan_digest,
            )
        if plan.config.numeric_mode != row["mode"]:
            raise EngineError(
                E_RECOVERY_FAILED,
                "stored plan mode is inconsistent",
                stored_mode=row["mode"],
                config_mode=plan.config.numeric_mode,
            )
        return plan

    def load_batches(self, mode: str | None = None) -> list[Batch]:
        if mode is None:
            row = self.conn.execute("SELECT mode FROM plan WHERE id = 1").fetchone()
            if row is None:
                raise EngineError(E_NOT_FOUND, "workspace has no loaded plan")
            mode = str(row["mode"])
        plan = self.load_plan()
        rows = self.conn.execute(
            "SELECT * FROM batches WHERE mode = ? ORDER BY batch_seq",
            (mode,),
        ).fetchall()
        batches: list[Batch] = []
        for row in rows:
            raw = strict_json_loads(row["payload_json"])
            batch = parse_batch(
                raw,
                plan.config,
                expected_seq=int(row["batch_seq"]),
            )
            if batch.digest != row["batch_digest"]:
                raise EngineError(
                    E_RECOVERY_FAILED,
                    "stored batch payload digest is inconsistent",
                    batch_seq=batch.batch_seq,
                    stored_digest=row["batch_digest"],
                    computed_digest=batch.digest,
                )
            batches.append(batch)
        return batches

    def next_pending_batch(
        self,
        mode: str,
        target_seq: int | None = None,
    ) -> sqlite3.Row | None:
        if target_seq is not None:
            return self.conn.execute(
                """
                SELECT * FROM batches
                WHERE mode = ? AND batch_seq = ? AND status = 'pending'
                """,
                (mode, target_seq),
            ).fetchone()
        return self.conn.execute(
            """
            SELECT * FROM batches
            WHERE mode = ? AND status = 'pending'
            ORDER BY batch_seq
            LIMIT 1
            """,
            (mode,),
        ).fetchone()

    def batch_status(self, mode: str, seq: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM batches WHERE mode = ? AND batch_seq = ?",
            (mode, seq),
        ).fetchone()

    def record_failure(
        self,
        *,
        mode: str,
        batch_seq: int,
        phase: str,
        code: str,
        details: dict[str, Any],
        logical_time: int,
        column: str | None = None,
        group_hash: str | None = None,
        group_key_json: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO failures
                (mode, batch_seq, phase, error_code, column_name, group_hash,
                 group_key_json, details_json, logical_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mode,
                batch_seq,
                phase,
                code,
                column,
                group_hash,
                group_key_json,
                canonical_dumps(details),
                logical_time,
            ),
        )

    def mark_group_failed(
        self,
        *,
        mode: str,
        batch_seq: int,
        phase: str,
        code: str,
        group_hash_value: str,
        group_key_json: str,
        details: dict[str, Any],
        logical_time: int,
        column: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO group_failures
                (mode, group_hash, group_key_json, error_code, batch_seq, phase,
                 details_json, logical_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mode, group_hash) DO UPDATE SET
                error_code = excluded.error_code,
                batch_seq = excluded.batch_seq,
                phase = excluded.phase,
                details_json = excluded.details_json,
                logical_time = excluded.logical_time
            """,
            (
                mode,
                group_hash_value,
                group_key_json,
                code,
                batch_seq,
                phase,
                canonical_dumps(details),
                logical_time,
            ),
        )
        self.conn.execute(
            """
            UPDATE group_states
            SET status = 'failed'
            WHERE mode = ? AND group_hash = ?
            """,
            (mode, group_hash_value),
        )
        self.record_failure(
            mode=mode,
            batch_seq=batch_seq,
            phase=phase,
            code=code,
            details=details,
            logical_time=logical_time,
            column=column,
            group_hash=group_hash_value,
            group_key_json=group_key_json,
        )

    def batch_digests(self, mode: str) -> dict[int, str]:
        rows = self.conn.execute(
            "SELECT batch_seq, batch_digest FROM batches WHERE mode = ? ORDER BY batch_seq",
            (mode,),
        ).fetchall()
        return {int(row["batch_seq"]): str(row["batch_digest"]) for row in rows}

    def _update_state_digest(self) -> str:
        digest = compute_state_digest(self.conn)
        self.set_meta("state_digest", digest)
        return digest

    def state_digest(self) -> str:
        return self.get_meta("state_digest", "") or ""


def compute_state_digest(conn: sqlite3.Connection) -> str:
    payload: dict[str, Any] = {}
    group_rows = conn.execute(
        """
        SELECT mode, group_hash, group_key_json, version, state_json, state_digest,
               spill_id, status, last_touch
        FROM group_states
        ORDER BY mode, group_hash
        """
    ).fetchall()
    payload["groups"] = [dict(row) for row in group_rows]

    result_rows = conn.execute(
        """
        SELECT mode, kind, group_hash, group_key_json, item_id, version,
               window_start, window_end, value_json
        FROM results
        ORDER BY mode, kind, group_hash, item_id
        """
    ).fetchall()
    payload["results"] = [dict(row) for row in result_rows]

    failure_rows = conn.execute(
        """
        SELECT mode, group_hash, group_key_json, error_code, batch_seq, phase,
               details_json, logical_time
        FROM group_failures
        ORDER BY mode, group_hash
        """
    ).fetchall()
    payload["group_failures"] = [dict(row) for row in failure_rows]

    commit_rows = conn.execute(
        """
        SELECT mode, batch_seq, group_hash, version
        FROM group_commits
        ORDER BY mode, batch_seq, group_hash
        """
    ).fetchall()
    payload["group_commits"] = [dict(row) for row in commit_rows]

    audit_row = conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(scale), 0) AS scale_sum,
               COALESCE(SUM(COALESCE(scaled, 0)), 0) AS scaled_sum
        FROM conversion_audit
        """
    ).fetchone()
    payload["conversion_audit"] = dict(audit_row)
    return sha256_json(payload)
