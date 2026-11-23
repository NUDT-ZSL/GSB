from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .config import canonical_json, parse_configuration, stable_hash
from .errors import EngineError, fail
from .fixed import RoundingDirection, to_units
from .locks import FileLock
from .models import Configuration, EventSpec

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL CHECK (version >= 1),
    snapshot_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_versions (
    version INTEGER PRIMARY KEY,
    logical_clock_ms INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    state_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS streams (
    tenant TEXT NOT NULL,
    metric TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    sample_period_ms INTEGER NOT NULL,
    sample_bytes INTEGER NOT NULL,
    partition INTEGER NOT NULL,
    route_hash TEXT NOT NULL,
    PRIMARY KEY (tenant, metric)
);
CREATE TABLE IF NOT EXISTS partitions (
    partition INTEGER PRIMARY KEY,
    arrival_seq INTEGER NOT NULL,
    buffer_depth INTEGER NOT NULL,
    buffer_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    identity_hash TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    metric TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    logical_time_ms INTEGER NOT NULL,
    sample_seq INTEGER NOT NULL,
    sample_bytes INTEGER NOT NULL,
    sample_period_ms INTEGER NOT NULL,
    partition INTEGER NOT NULL,
    partition_arrival_seq INTEGER NOT NULL,
    global_order_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('BUFFERED', 'PERSISTED', 'DROPPED')),
    decision TEXT NOT NULL,
    reason TEXT,
    first_constraint_json TEXT,
    event_hash TEXT NOT NULL,
    trace_id TEXT,
    created_version INTEGER NOT NULL,
    updated_version INTEGER NOT NULL,
    flush_clock_ms INTEGER,
    UNIQUE (partition, partition_arrival_seq)
);
CREATE INDEX IF NOT EXISTS ix_samples_state_order
    ON samples (state, tenant, logical_time_ms, sample_seq, partition, partition_arrival_seq);
CREATE INDEX IF NOT EXISTS ix_samples_partition_state ON samples (partition, state);
CREATE TABLE IF NOT EXISTS tenant_quotas (
    tenant TEXT PRIMARY KEY,
    window_limit_units INTEGER NOT NULL,
    borrow_limit_units INTEGER NOT NULL,
    allow_borrow INTEGER NOT NULL,
    fixed_scale INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    comparison_tolerance_units INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_windows (
    tenant TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    limit_units INTEGER NOT NULL,
    borrow_limit_units INTEGER NOT NULL,
    allow_borrow INTEGER NOT NULL,
    used_units INTEGER NOT NULL,
    borrowed_units INTEGER NOT NULL,
    frozen_remaining_units INTEGER,
    return_obligation_units INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'FROZEN')),
    created_version INTEGER NOT NULL,
    updated_version INTEGER NOT NULL,
    PRIMARY KEY (tenant, window_id)
);
CREATE TABLE IF NOT EXISTS quota_window_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    remaining_units INTEGER,
    used_units INTEGER NOT NULL,
    borrowed_units INTEGER NOT NULL,
    return_obligation_units INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_charges (
    sample_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    required_units INTEGER NOT NULL,
    base_units INTEGER NOT NULL,
    borrowed_units INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS borrow_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id TEXT NOT NULL UNIQUE,
    tenant TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    borrowed_units INTEGER NOT NULL,
    repay_required_units INTEGER NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS token_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    capacity_units INTEGER NOT NULL,
    balance_units INTEGER NOT NULL,
    refill_rate_units_per_second INTEGER NOT NULL,
    clock_step_ms INTEGER NOT NULL,
    fixed_scale INTEGER NOT NULL,
    token_cost_units INTEGER NOT NULL,
    last_refill_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS token_charges (
    sample_id TEXT PRIMARY KEY,
    cost_units INTEGER NOT NULL,
    balance_before_units INTEGER NOT NULL,
    balance_after_units INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS persisted_samples (
    sample_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    metric TEXT NOT NULL,
    partition INTEGER NOT NULL,
    partition_arrival_seq INTEGER NOT NULL,
    logical_time_ms INTEGER NOT NULL,
    sample_bytes INTEGER NOT NULL,
    data_file TEXT NOT NULL,
    file_offset INTEGER NOT NULL,
    record_hash TEXT NOT NULL,
    quota_window_id INTEGER NOT NULL,
    token_cost_units INTEGER NOT NULL,
    flush_clock_ms INTEGER NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS precision_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    step TEXT NOT NULL,
    tenant TEXT,
    window_id INTEGER,
    partition INTEGER,
    source_value TEXT NOT NULL,
    scale INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    discarded_remainder_units TEXT NOT NULL,
    error_bound_units TEXT NOT NULL,
    cumulative_error_units TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_precision_scope ON precision_ops (scope, tenant, window_id, partition);
CREATE TABLE IF NOT EXISTS precision_sensitive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    entity_a TEXT NOT NULL,
    entity_b TEXT NOT NULL,
    availability_a_units INTEGER NOT NULL,
    availability_b_units INTEGER NOT NULL,
    delta_units INTEGER NOT NULL,
    tolerance_units INTEGER NOT NULL,
    decision_depends INTEGER NOT NULL,
    sources_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS flush_rounds (
    round_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    budget_ms INTEGER NOT NULL,
    spent_ms INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    persisted_count INTEGER NOT NULL,
    buffered_count INTEGER NOT NULL,
    abandoned_count INTEGER NOT NULL,
    deferred_count INTEGER NOT NULL,
    token_balance_after_units INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    partition INTEGER NOT NULL,
    start_state TEXT NOT NULL,
    outcome TEXT NOT NULL,
    terminal_state TEXT NOT NULL,
    reason TEXT,
    required_units INTEGER,
    available_units INTEGER,
    first_constraint_json TEXT,
    budget_before_ms INTEGER,
    budget_after_ms INTEGER,
    created_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_round ON attempts (round_id, id);
CREATE TABLE IF NOT EXISTS ingest_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    metric TEXT NOT NULL,
    partition INTEGER NOT NULL,
    partition_arrival_seq INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    logical_clock_ms INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id TEXT,
    partition INTEGER,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL
);
"""


def _json(value: Any) -> str:
    return canonical_json(value)


def _d(value: Decimal) -> str:
    return format(value, "f")


class Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.state_dir = self.root / "state"
        self.data_dir = self.root / "data"
        self.buffer_dir = self.root / "buffer"
        self.log_dir = self.root / "logs"
        self.db_path = self.state_dir / "engine.sqlite3"
        self.log_path = self.log_dir / "engine.jsonl"

    def _connect(self) -> sqlite3.Connection:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self, config: Configuration) -> dict[str, Any]:
        first = not self.db_path.exists()
        conn = self._connect()
        try:
            if first:
                for directory in (self.data_dir, self.buffer_dir, self.log_dir):
                    directory.mkdir(parents=True, exist_ok=True)
                conn.executescript(SCHEMA)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._seed(conn, config)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                return {
                    "ok": True,
                    "action": "initialized",
                    "version": 1,
                    "config_hash": config.config_hash,
                    "db_path": str(self.db_path),
                }

            conn.executescript(SCHEMA)
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                fail("E_INCOMPLETE_STATE", "state database is missing schema metadata")
            if int(row["value"]) != SCHEMA_VERSION:
                fail(
                    "E_SCHEMA_VERSION",
                    "state database schema version is unsupported",
                    found=int(row["value"]),
                    expected=SCHEMA_VERSION,
                )
            existing = conn.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
            if existing is None or existing["value"] != config.config_hash:
                fail(
                    "E_CONFIG_CONFLICT",
                    "state database was initialized with a different configuration",
                    existing=existing["value"] if existing else None,
                    incoming=config.config_hash,
                )
            version = self._current_version(conn)
            return {
                "ok": True,
                "action": "unchanged",
                "version": version,
                "config_hash": config.config_hash,
                "db_path": str(self.db_path),
            }
        finally:
            conn.close()

    def _seed(self, conn: sqlite3.Connection, config: Configuration) -> None:
        for directory in (self.data_dir, self.buffer_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        version = 1
        meta = {
            "schema_version": str(SCHEMA_VERSION),
            "engine_version": "0.1.0",
            "config_hash": config.config_hash,
            "config_json": _json(config.raw),
            "logical_clock_ms": "0",
        }
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            sorted(meta.items()),
        )
        for stream in config.streams:
            conn.execute(
                """
                INSERT INTO streams(
                    tenant, metric, tags_json, sample_period_ms, sample_bytes,
                    partition, route_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stream.tenant,
                    stream.metric,
                    _json(list(stream.tags)),
                    stream.sample_period_ms,
                    stream.sample_bytes,
                    stream.partition,
                    stream.route_hash,
                ),
            )
        for partition in range(config.partitions.partition_count):
            conn.execute(
                "INSERT INTO partitions(partition, arrival_seq, buffer_depth, buffer_digest) "
                "VALUES (?, 0, 0, ?)",
                (partition, stable_hash([])),
            )
        quota_scale = config.quotas.fixed_scale
        tolerance_units = to_units(
            config.quotas.comparison_tolerance_bytes, quota_scale, "FLOOR"
        )
        for tenant, declaration in sorted(config.quotas.tenants.items()):
            limit = to_units(declaration.window_limit_bytes, quota_scale, "FLOOR")
            borrow = to_units(declaration.borrow_limit_bytes, quota_scale, "FLOOR")
            conn.execute(
                """
                INSERT INTO tenant_quotas(
                    tenant, window_limit_units, borrow_limit_units, allow_borrow,
                    fixed_scale, window_ms, comparison_tolerance_units
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant,
                    limit.units,
                    borrow.units,
                    int(declaration.allow_borrow),
                    quota_scale,
                    config.quotas.window_ms,
                    tolerance_units.units,
                ),
            )
            self._insert_precision_op(
                conn,
                version=version,
                logical_clock_ms=0,
                scope="quota",
                step="window_limit_scale",
                tenant=tenant,
                window_id=None,
                partition=None,
                source_value=_d(declaration.window_limit_bytes),
                scale=quota_scale,
                direction="FLOOR",
                discarded=limit.discarded_remainder_units,
                error_bound=limit.error_bound_units,
                detail={"unit": "bytes", "declared_scale": quota_scale},
            )
            self._insert_precision_op(
                conn,
                version=version,
                logical_clock_ms=0,
                scope="quota",
                step="borrow_limit_scale",
                tenant=tenant,
                window_id=None,
                partition=None,
                source_value=_d(declaration.borrow_limit_bytes),
                scale=quota_scale,
                direction="FLOOR",
                discarded=borrow.discarded_remainder_units,
                error_bound=borrow.error_bound_units,
                detail={"unit": "bytes", "declared_scale": quota_scale},
            )

        for stream in sorted(config.streams, key=lambda item: (item.tenant, item.metric)):
            converted = to_units(stream.sample_bytes, quota_scale, "FLOOR")
            self._insert_precision_op(
                conn,
                version=version,
                logical_clock_ms=0,
                scope="quota",
                step="sample_bytes_multiply",
                tenant=stream.tenant,
                window_id=None,
                partition=stream.partition,
                source_value=str(stream.sample_bytes),
                scale=quota_scale,
                direction="FLOOR",
                discarded=converted.discarded_remainder_units,
                error_bound=converted.error_bound_units,
                detail={
                    "metric": stream.metric,
                    "operation": "sample_bytes * declared_scale",
                },
            )

        token_scale = config.tokens.fixed_scale
        capacity = config.tokens.capacity_tokens * token_scale
        refill = config.tokens.refill_rate_per_second * token_scale
        token_cost = config.partitions.token_cost_per_sample * token_scale
        conn.execute(
            """
            INSERT INTO token_state(
                id, capacity_units, balance_units, refill_rate_units_per_second,
                clock_step_ms, fixed_scale, token_cost_units, last_refill_ms
            ) VALUES (1, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                capacity,
                capacity,
                refill,
                config.tokens.clock_step_ms,
                token_scale,
                token_cost,
            ),
        )
        for step, value in (
            ("capacity_tokens_multiply", config.tokens.capacity_tokens),
            ("refill_rate_multiply", config.tokens.refill_rate_per_second),
        ):
            converted = to_units(value, token_scale, "FLOOR")
            self._insert_precision_op(
                conn,
                version=version,
                logical_clock_ms=0,
                scope="token",
                step=step,
                tenant=None,
                window_id=None,
                partition=None,
                source_value=str(value),
                scale=token_scale,
                direction="FLOOR",
                discarded=converted.discarded_remainder_units,
                error_bound=converted.error_bound_units,
                detail={"unit": "tokens", "declared_scale": token_scale},
            )

        snapshot, snapshot_sha = self._build_snapshot(conn, 0)
        conn.execute(
            """
            INSERT INTO state_versions(version, logical_clock_ms, state_json, state_sha256, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (version, 0, snapshot, snapshot_sha, "initialize"),
        )
        conn.execute(
            """
            INSERT INTO ledger_pointer(id, version, snapshot_sha256, reason)
            VALUES (1, ?, ?, ?)
            """,
            (version, snapshot_sha, "initialize"),
        )

    @staticmethod
    def _insert_precision_op(
        conn: sqlite3.Connection,
        *,
        version: int,
        logical_clock_ms: int,
        scope: str,
        step: str,
        tenant: str | None,
        window_id: int | None,
        partition: int | None,
        source_value: str,
        scale: int,
        direction: RoundingDirection,
        discarded: Decimal,
        error_bound: Decimal,
        detail: dict[str, Any],
    ) -> int:
        row = conn.execute(
            """
            SELECT cumulative_error_units FROM precision_ops
            WHERE scope=? AND COALESCE(tenant,'')=COALESCE(?,'')
              AND COALESCE(window_id,-1)=COALESCE(?,-1)
              AND COALESCE(partition,-1)=COALESCE(?,-1)
            ORDER BY id DESC LIMIT 1
            """,
            (scope, tenant, window_id, partition),
        ).fetchone()
        cumulative = Decimal(row["cumulative_error_units"]) if row else Decimal(0)
        cumulative += error_bound
        cursor = conn.execute(
            """
            INSERT INTO precision_ops(
                scope, step, tenant, window_id, partition, source_value, scale,
                rounding_direction, discarded_remainder_units, error_bound_units,
                cumulative_error_units, detail_json, logical_clock_ms, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                step,
                tenant,
                window_id,
                partition,
                source_value,
                scale,
                direction,
                _d(discarded),
                _d(error_bound),
                _d(cumulative),
                _json(detail),
                logical_clock_ms,
                version,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _current_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT version FROM ledger_pointer WHERE id=1").fetchone()
        if row is None:
            fail("E_INCOMPLETE_STATE", "ledger pointer is missing")
        return int(row["version"])

    def current_version(self) -> int:
        conn = self._connect()
        try:
            return self._current_version(conn)
        finally:
            conn.close()

    def logical_clock(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='logical_clock_ms'").fetchone()
            if row is None:
                fail("E_INCOMPLETE_STATE", "logical clock metadata is missing")
            return int(row["value"])
        finally:
            conn.close()

    def load_configuration(self) -> Configuration:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='config_json'").fetchone()
            if row is None:
                fail("E_CONFIG_NOT_LOADED", "configuration has not been loaded")
            raw = json.loads(row["value"])
        finally:
            conn.close()
        return parse_configuration(raw["streams"], raw["partitions"], raw["quotas"], raw["tokens"])

    @contextmanager
    def transaction(
        self,
        reason: str,
        *,
        expected_version: int | None = None,
    ) -> Iterator["Transaction"]:
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._current_version(conn)
            if expected_version is not None and expected_version != current:
                fail(
                    "E_STALE_VERSION",
                    "expected ledger version does not match the current version",
                    expected=expected_version,
                    current=current,
                )
            tx = Transaction(self, conn, current + 1, reason)
            try:
                yield tx
                tx.finalize()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _build_snapshot(
        self, conn: sqlite3.Connection, logical_clock_ms: int
    ) -> tuple[str, str]:
        partitions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT partition, arrival_seq, buffer_depth, buffer_digest
                FROM partitions ORDER BY partition
                """
            )
        ]
        quotas = [
            dict(row)
            for row in conn.execute(
                """
                SELECT tenant, window_id, limit_units, used_units, borrowed_units,
                       frozen_remaining_units, return_obligation_units, status,
                       updated_version
                FROM quota_windows ORDER BY tenant, window_id
                """
            )
        ]
        buffers = [
            {
                "sample_id": row["sample_id"],
                "partition": row["partition"],
                "partition_arrival_seq": row["partition_arrival_seq"],
                "tenant": row["tenant"],
                "logical_time_ms": row["logical_time_ms"],
                "sample_seq": row["sample_seq"],
            }
            for row in conn.execute(
                """
                SELECT sample_id, partition, partition_arrival_seq, tenant,
                       logical_time_ms, sample_seq
                FROM samples WHERE state='BUFFERED'
                ORDER BY tenant, logical_time_ms, sample_seq, partition,
                         partition_arrival_seq, sample_id
                """
            )
        ]
        token = conn.execute("SELECT * FROM token_state WHERE id=1").fetchone()
        token_dict = dict(token) if token is not None else {}
        sensitive = [
            {
                "kind": row["kind"],
                "entity_a": row["entity_a"],
                "entity_b": row["entity_b"],
                "delta_units": row["delta_units"],
                "tolerance_units": row["tolerance_units"],
                "sources": json.loads(row["sources_json"]),
            }
            for row in conn.execute(
                """
                SELECT kind, entity_a, entity_b, delta_units, tolerance_units, sources_json
                FROM precision_sensitive ORDER BY id
                """
            )
        ]
        persisted_count = conn.execute("SELECT COUNT(*) AS n FROM persisted_samples").fetchone()["n"]
        snapshot = {
            "logical_clock_ms": logical_clock_ms,
            "partitions": partitions,
            "quotas": quotas,
            "token": token_dict,
            "buffer": buffers,
            "buffer_digest": stable_hash(buffers),
            "persisted_count": int(persisted_count),
            "precision_sensitive": sensitive,
        }
        text = _json(snapshot)
        return text, stable_hash(snapshot)

    def refresh_derived_files(self) -> None:
        conn = self._connect()
        try:
            clock = int(
                conn.execute("SELECT value FROM meta WHERE key='logical_clock_ms'").fetchone()[
                    "value"
                ]
            )
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT sample_id, tenant, metric, tags_json, logical_time_ms,
                           sample_seq, sample_bytes, sample_period_ms, partition,
                           partition_arrival_seq, global_order_key, decision, reason
                    FROM samples WHERE state='BUFFERED'
                    ORDER BY tenant, logical_time_ms, sample_seq, partition,
                             partition_arrival_seq, sample_id
                    """
                )
            ]
        finally:
            conn.close()
        by_partition: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            by_partition.setdefault(int(row["partition"]), []).append(row)
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        for partition, buffered in sorted(by_partition.items()):
            payload = {
                "partition": partition,
                "logical_clock_ms": clock,
                "buffer_depth": len(buffered),
                "buffer_digest": stable_hash([row["sample_id"] for row in buffered]),
                "samples": buffered,
            }
            target = self.buffer_dir / f"partition-{partition:04d}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(_json(payload) + "\n", encoding="utf-8")
            os.replace(temporary, target)

    def buffer_snapshot_health(self, *, cleanup: bool = False) -> dict[str, Any]:
        conn = self._connect()
        try:
            expected = {
                int(row["partition"]): {
                    "buffer_depth": int(row["buffer_depth"]),
                    "buffer_digest": row["buffer_digest"],
                }
                for row in conn.execute(
                    "SELECT partition, buffer_depth, buffer_digest FROM partitions"
                )
            }
        finally:
            conn.close()
        stale: list[dict[str, Any]] = []
        temporary: list[str] = []
        for partition, expected_row in sorted(expected.items()):
            target = self.buffer_dir / f"partition-{partition:04d}.json"
            temporary_path = target.with_suffix(".json.tmp")
            if temporary_path.exists():
                temporary.append(str(temporary_path))
                if cleanup:
                    temporary_path.unlink()
            reason = None
            if not target.exists():
                reason = "missing"
            else:
                try:
                    payload = json.loads(target.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = None
                    reason = "unreadable"
                if payload is not None and (
                    int(payload.get("buffer_depth", -1)) != expected_row["buffer_depth"]
                    or payload.get("buffer_digest") != expected_row["buffer_digest"]
                ):
                    reason = "digest_or_depth_mismatch"
            if reason is not None:
                stale.append({"partition": partition, "reason": reason})
        return {
            "checked_partitions": len(expected),
            "stale_snapshots": stale,
            "temporary_files": temporary,
        }

    def append_log(self, record: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        line = _json(record) + "\n"
        with FileLock(self.log_dir / "engine.lock"):
            with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())


class Transaction:
    def __init__(
        self,
        store: Store,
        conn: sqlite3.Connection,
        version: int,
        reason: str,
    ) -> None:
        self.store = store
        self.conn = conn
        self.version = version
        self.reason = reason

    def finalize(self) -> None:
        self._refresh_partitions()
        clock = self.logical_clock()
        snapshot, snapshot_sha = self.store._build_snapshot(self.conn, clock)
        old = self.store._current_version(self.conn)
        if self.version <= old:
            fail(
                "E_VERSION_REGRESSION",
                "ledger version must strictly increase",
                current=old,
                attempted=self.version,
            )
        self.conn.execute(
            """
            INSERT INTO state_versions(version, logical_clock_ms, state_json, state_sha256, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (self.version, clock, snapshot, snapshot_sha, self.reason),
        )
        self.conn.execute(
            """
            UPDATE ledger_pointer
            SET version=?, snapshot_sha256=?, reason=?
            WHERE id=1 AND version=?
            """,
            (self.version, snapshot_sha, self.reason, old),
        )
        if self.conn.execute("SELECT changes() AS n").fetchone()["n"] != 1:
            fail(
                "E_STALE_VERSION",
                "ledger pointer changed while committing",
                current=old,
                attempted=self.version,
            )

    def _refresh_partitions(self) -> None:
        rows = self.conn.execute(
            """
            SELECT partition, COUNT(*) AS depth
            FROM samples
            WHERE state='BUFFERED'
            GROUP BY partition
            """
        ).fetchall()
        depths = {int(row["partition"]): int(row["depth"]) for row in rows}
        partitions = [
            row["partition"] for row in self.conn.execute("SELECT partition FROM partitions")
        ]
        for partition in partitions:
            ids = [
                row["sample_id"]
                for row in self.conn.execute(
                    """
                    SELECT sample_id FROM samples
                    WHERE state='BUFFERED' AND partition=?
                    ORDER BY tenant, logical_time_ms, sample_seq,
                             partition_arrival_seq, sample_id
                    """,
                    (partition,),
                )
            ]
            self.conn.execute(
                "UPDATE partitions SET buffer_depth=?, buffer_digest=? WHERE partition=?",
                (depths.get(int(partition), 0), stable_hash(ids), partition),
            )

    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def logical_clock(self) -> int:
        return int(self.meta_get("logical_clock_ms") or "0")

    def set_clock(self, logical_clock_ms: int, *, allow_equal: bool = True) -> int:
        current = self.logical_clock()
        if logical_clock_ms < current or (not allow_equal and logical_clock_ms == current):
            fail(
                "E_CLOCK_REGRESSION",
                "logical clock must be monotonic",
                current_ms=current,
                requested_ms=logical_clock_ms,
            )
        self.meta_set("logical_clock_ms", str(logical_clock_ms))
        return logical_clock_ms

    def record_precision_op(
        self,
        *,
        scope: str,
        step: str,
        tenant: str | None,
        window_id: int | None,
        partition: int | None,
        source_value: str,
        scale: int,
        direction: RoundingDirection,
        discarded: Decimal,
        error_bound: Decimal,
        detail: dict[str, Any],
    ) -> int:
        return self.store._insert_precision_op(
            self.conn,
            version=self.version,
            logical_clock_ms=self.logical_clock(),
            scope=scope,
            step=step,
            tenant=tenant,
            window_id=window_id,
            partition=partition,
            source_value=source_value,
            scale=scale,
            direction=direction,
            discarded=discarded,
            error_bound=error_bound,
            detail=detail,
        )

    def partition_state(self, partition: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM partitions WHERE partition=?", (partition,)).fetchone()
        if row is None:
            fail("E_BAD_PARTITION", "partition does not exist", partition=partition)
        return dict(row)

    def buffer_depth(self, partition: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM samples WHERE state='BUFFERED' AND partition=?",
            (partition,),
        ).fetchone()
        return int(row["n"])

    def sample_by_id(self, sample_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM samples WHERE sample_id=?", (sample_id,)).fetchone()
        return dict(row) if row else None

    def sample_by_identity(self, identity_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM samples WHERE identity_hash=?", (identity_hash,)
        ).fetchone()
        return dict(row) if row else None

    def insert_sample(
        self,
        event: EventSpec,
        *,
        partition: int,
        state: str,
        decision: str,
        reason: str | None,
        first_constraint: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        identity = {
            "tenant": event.tenant,
            "metric": event.metric,
            "tags": [list(item) for item in event.tags],
            "logical_time_ms": event.logical_time_ms,
            "sample_seq": event.sample_seq,
            "sample_bytes": event.sample_bytes,
        }
        identity_hash = stable_hash(identity)
        existing = self.sample_by_identity(identity_hash)
        if existing is not None:
            return existing, False
        existing = self.sample_by_id(event.event_id)
        if existing is not None:
            if existing["identity_hash"] != identity_hash:
                fail(
                    "E_EVENT_ID_CONFLICT",
                    "event_id was already used for a different sample",
                    event_id=event.event_id,
                )
            return existing, False

        current = self.partition_state(partition)
        arrival_seq = int(current["arrival_seq"]) + 1
        self.conn.execute(
            "UPDATE partitions SET arrival_seq=? WHERE partition=?",
            (arrival_seq, partition),
        )
        global_order_key = (
            f"{event.logical_time_ms:020d}:{event.sample_seq:020d}:"
            f"{partition:06d}:{arrival_seq:020d}:{event.event_id}"
        )
        self.conn.execute(
            """
            INSERT INTO samples(
                sample_id, identity_hash, event_id, tenant, metric, tags_json,
                logical_time_ms, sample_seq, sample_bytes, sample_period_ms,
                partition, partition_arrival_seq, global_order_key, state,
                decision, reason, first_constraint_json, event_hash, trace_id,
                created_version, updated_version, flush_clock_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                event.event_id,
                identity_hash,
                event.event_id,
                event.tenant,
                event.metric,
                _json(list(event.tags)),
                event.logical_time_ms,
                event.sample_seq,
                event.sample_bytes,
                event.sample_period_ms,
                partition,
                arrival_seq,
                global_order_key,
                state,
                decision,
                reason,
                _json(first_constraint) if first_constraint else None,
                stable_hash(identity),
                event.trace_id,
                self.version,
                self.version,
            ),
        )
        row = self.sample_by_id(event.event_id)
        assert row is not None
        return row, True

    def record_ingest_decision(
        self,
        sample: dict[str, Any],
        *,
        decision: str,
        reason: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ingest_decisions(
                sample_id, event_id, tenant, metric, partition,
                partition_arrival_seq, decision, reason, logical_clock_ms,
                detail_json, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample["sample_id"],
                sample["event_id"],
                sample["tenant"],
                sample["metric"],
                sample["partition"],
                sample["partition_arrival_seq"],
                decision,
                reason,
                int(sample["logical_time_ms"]),
                _json(detail or {}),
                self.version,
            ),
        )

    def active_window_id(self, logical_clock_ms: int) -> int:
        row = self.conn.execute(
            "SELECT window_ms FROM tenant_quotas LIMIT 1"
        ).fetchone()
        if row is None:
            fail("E_BAD_QUOTA", "no tenant quota declaration")
        return logical_clock_ms // int(row["window_ms"])

    def freeze_windows(self, logical_clock_ms: int) -> list[dict[str, Any]]:
        current_window = self.active_window_id(logical_clock_ms)
        rows = self.conn.execute(
            """
            SELECT * FROM quota_windows
            WHERE status='ACTIVE' AND window_id < ?
            ORDER BY tenant, window_id
            """,
            (current_window,),
        ).fetchall()
        frozen: list[dict[str, Any]] = []
        for row in rows:
            remaining = max(
                0,
                int(row["limit_units"])
                + int(row["borrow_limit_units"])
                - int(row["used_units"]),
            )
            self.conn.execute(
                """
                UPDATE quota_windows
                SET status='FROZEN', frozen_remaining_units=?, updated_version=?
                WHERE tenant=? AND window_id=?
                """,
                (remaining, self.version, row["tenant"], row["window_id"]),
            )
            self.conn.execute(
                """
                INSERT INTO quota_window_events(
                    tenant, window_id, event, remaining_units, used_units,
                    borrowed_units, return_obligation_units, logical_clock_ms, version
                ) VALUES (?, ?, 'FROZEN', ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["tenant"],
                    row["window_id"],
                    remaining,
                    row["used_units"],
                    row["borrowed_units"],
                    row["return_obligation_units"],
                    logical_clock_ms,
                    self.version,
                ),
            )
            frozen.append(
                {
                    "tenant": row["tenant"],
                    "window_id": row["window_id"],
                    "frozen_remaining_units": remaining,
                }
            )
        return frozen

    def ensure_quota_window(self, tenant: str, logical_clock_ms: int) -> dict[str, Any]:
        declaration = self.conn.execute(
            "SELECT * FROM tenant_quotas WHERE tenant=?", (tenant,)
        ).fetchone()
        if declaration is None:
            fail("E_BAD_QUOTA", "tenant has no quota declaration", tenant=tenant)
        window_id = logical_clock_ms // int(declaration["window_ms"])
        row = self.conn.execute(
            "SELECT * FROM quota_windows WHERE tenant=? AND window_id=?",
            (tenant, window_id),
        ).fetchone()
        if row is None:
            start_ms = window_id * int(declaration["window_ms"])
            end_ms = start_ms + int(declaration["window_ms"])
            self.conn.execute(
                """
                INSERT INTO quota_windows(
                    tenant, window_id, window_start_ms, window_end_ms,
                    limit_units, borrow_limit_units, allow_borrow, used_units,
                    borrowed_units, frozen_remaining_units, return_obligation_units,
                    status, created_version, updated_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, 0, 'ACTIVE', ?, ?)
                """,
                (
                    tenant,
                    window_id,
                    start_ms,
                    end_ms,
                    declaration["window_limit_units"],
                    declaration["borrow_limit_units"],
                    declaration["allow_borrow"],
                    self.version,
                    self.version,
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM quota_windows WHERE tenant=? AND window_id=?",
                (tenant, window_id),
            ).fetchone()
        return dict(row)

    def quota_availability(self, tenant: str, logical_clock_ms: int) -> dict[str, Any]:
        row = self.ensure_quota_window(tenant, logical_clock_ms)
        limit = int(row["limit_units"])
        used = int(row["used_units"])
        borrowed = int(row["borrowed_units"])
        borrow_limit = int(row["borrow_limit_units"])
        base_remaining = max(0, limit - used)
        borrow_remaining = (
            max(0, borrow_limit - borrowed) if int(row["allow_borrow"]) else 0
        )
        return {
            "tenant": tenant,
            "window_id": int(row["window_id"]),
            "available_units": base_remaining + borrow_remaining,
            "base_remaining_units": base_remaining,
            "borrow_remaining_units": borrow_remaining,
            "used_units": used,
            "borrowed_units": borrowed,
            "limit_units": limit,
            "borrow_limit_units": borrow_limit,
            "allow_borrow": bool(row["allow_borrow"]),
        }

    def charge_quota(
        self,
        *,
        sample_id: str,
        tenant: str,
        required_units: int,
        logical_clock_ms: int,
        sample_bytes: int,
        scale: int,
    ) -> dict[str, Any]:
        existing = self.conn.execute(
            "SELECT * FROM quota_charges WHERE sample_id=?", (sample_id,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        window = self.ensure_quota_window(tenant, logical_clock_ms)
        limit = int(window["limit_units"])
        used = int(window["used_units"])
        borrowed = int(window["borrowed_units"])
        borrow_limit = int(window["borrow_limit_units"])
        base_remaining = max(0, limit - used)
        base_use = min(required_units, base_remaining)
        borrow_use = required_units - base_use
        if borrow_use > 0:
            if not int(window["allow_borrow"]):
                fail(
                    "E_QUOTA_EXHAUSTED",
                    "tenant quota is exhausted and borrowing is disabled",
                    tenant=tenant,
                    window_id=window["window_id"],
                    required_units=required_units,
                    available_units=base_remaining,
                )
            if borrowed + borrow_use > borrow_limit:
                fail(
                    "E_QUOTA_EXHAUSTED",
                    "tenant borrow limit would be exceeded",
                    tenant=tenant,
                    window_id=window["window_id"],
                    required_units=required_units,
                    available_units=base_remaining + max(0, borrow_limit - borrowed),
                )
        self.conn.execute(
            """
            UPDATE quota_windows
            SET used_units=?, borrowed_units=?,
                return_obligation_units=return_obligation_units+?,
                updated_version=?
            WHERE tenant=? AND window_id=?
            """,
            (
                used + required_units,
                borrowed + borrow_use,
                borrow_use,
                self.version,
                tenant,
                window["window_id"],
            ),
        )
        self.conn.execute(
            """
            INSERT INTO quota_charges(
                sample_id, tenant, window_id, required_units, base_units,
                borrowed_units, logical_clock_ms, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample_id,
                tenant,
                window["window_id"],
                required_units,
                base_use,
                borrow_use,
                logical_clock_ms,
                self.version,
            ),
        )
        if borrow_use > 0:
            self.conn.execute(
                """
                INSERT INTO borrow_ledger(
                    sample_id, tenant, window_id, borrowed_units,
                    repay_required_units, status, version
                ) VALUES (?, ?, ?, ?, ?, 'OUTSTANDING', ?)
                """,
                (
                    sample_id,
                    tenant,
                    window["window_id"],
                    borrow_use,
                    borrow_use,
                    self.version,
                ),
            )
        converted = to_units(sample_bytes, scale, "FLOOR")
        self.record_precision_op(
            scope="quota",
            step="charge_multiply",
            tenant=tenant,
            window_id=int(window["window_id"]),
            partition=None,
            source_value=str(sample_bytes),
            scale=scale,
            direction="FLOOR",
            discarded=converted.discarded_remainder_units,
            error_bound=converted.error_bound_units,
            detail={"operation": "sample_bytes * scale", "required_units": required_units},
        )
        self.record_precision_op(
            scope="quota",
            step="used_add",
            tenant=tenant,
            window_id=int(window["window_id"]),
            partition=None,
            source_value=str(required_units),
            scale=scale,
            direction="FLOOR",
            discarded=Decimal(0),
            error_bound=Decimal(0),
            detail={"before_units": used, "after_units": used + required_units},
        )
        if borrow_use > 0:
            self.record_precision_op(
                scope="quota",
                step="borrow_add",
                tenant=tenant,
                window_id=int(window["window_id"]),
                partition=None,
                source_value=str(borrow_use),
                scale=scale,
                direction="FLOOR",
                discarded=Decimal(0),
                error_bound=Decimal(0),
                detail={"repay_required_units": borrow_use},
            )
        return {
            "sample_id": sample_id,
            "tenant": tenant,
            "window_id": int(window["window_id"]),
            "required_units": required_units,
            "base_units": base_use,
            "borrowed_units": borrow_use,
        }

    def token_state(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM token_state WHERE id=1").fetchone()
        if row is None:
            fail("E_INCOMPLETE_STATE", "token state is missing")
        return dict(row)

    def advance_tokens(self, logical_clock_ms: int) -> dict[str, Any]:
        row = self.token_state()
        step = int(row["clock_step_ms"])
        effective_ms = (logical_clock_ms // step) * step
        last = int(row["last_refill_ms"])
        if effective_ms < last:
            fail(
                "E_CLOCK_REGRESSION",
                "token refill clock cannot move backwards",
                current_ms=last,
                requested_ms=effective_ms,
            )
        elapsed = effective_ms - last
        rate = int(row["refill_rate_units_per_second"])
        exact = Decimal(elapsed) * Decimal(rate) / Decimal(1000)
        converted = to_units(exact, 1, "FLOOR")
        old_balance = int(row["balance_units"])
        capacity = int(row["capacity_units"])
        uncapped = old_balance + converted.units
        new_balance = min(capacity, uncapped)
        capacity_overflow = uncapped - new_balance
        time_remainder = logical_clock_ms - effective_ms
        if elapsed > 0:
            self.record_precision_op(
                scope="token",
                step="refill_mul_div",
                tenant=None,
                window_id=None,
                partition=None,
                source_value=f"{elapsed}*{rate}/1000",
                scale=1,
                direction="FLOOR",
                discarded=converted.discarded_remainder_units
                + Decimal(capacity_overflow),
                error_bound=converted.error_bound_units,
                detail={
                    "elapsed_ms": elapsed,
                    "refill_rate_units_per_second": rate,
                    "capacity_overflow_units": capacity_overflow,
                    "logical_time_quantization_remainder_ms": time_remainder,
                    "balance_before_units": old_balance,
                    "balance_after_units": new_balance,
                },
            )
        self.conn.execute(
            "UPDATE token_state SET balance_units=?, last_refill_ms=? WHERE id=1",
            (new_balance, effective_ms),
        )
        return {
            "balance_units": new_balance,
            "capacity_units": capacity,
            "last_refill_ms": effective_ms,
            "logical_time_quantization_remainder_ms": time_remainder,
            "refilled_units": new_balance - old_balance,
        }

    def charge_token(
        self,
        *,
        sample_id: str,
        cost_units: int,
        logical_clock_ms: int,
    ) -> dict[str, Any]:
        existing = self.conn.execute(
            "SELECT * FROM token_charges WHERE sample_id=?", (sample_id,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        advance = self.advance_tokens(logical_clock_ms)
        before = int(advance["balance_units"])
        if before < cost_units:
            fail(
                "E_TOKEN_EXHAUSTED",
                "global flush token balance is insufficient",
                required_units=cost_units,
                available_units=before,
            )
        after = before - cost_units
        self.conn.execute(
            "UPDATE token_state SET balance_units=? WHERE id=1",
            (after,),
        )
        self.conn.execute(
            """
            INSERT INTO token_charges(
                sample_id, cost_units, balance_before_units,
                balance_after_units, logical_clock_ms, version
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sample_id, cost_units, before, after, logical_clock_ms, self.version),
        )
        self.record_precision_op(
            scope="token",
            step="charge_subtract",
            tenant=None,
            window_id=None,
            partition=None,
            source_value=str(cost_units),
            scale=1,
            direction="FLOOR",
            discarded=Decimal(0),
            error_bound=Decimal(0),
            detail={"balance_before_units": before, "balance_after_units": after},
        )
        return {
            "sample_id": sample_id,
            "cost_units": cost_units,
            "balance_before_units": before,
            "balance_after_units": after,
        }

    def buffered_samples(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT * FROM samples WHERE state='BUFFERED'
                ORDER BY tenant, logical_time_ms, sample_seq, partition,
                         partition_arrival_seq, sample_id
                """
            )
        ]

    def buffer_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM samples WHERE state='BUFFERED'"
            ).fetchone()["n"]
        )

    def mark_persisted(
        self,
        sample: dict[str, Any],
        *,
        data_file: str,
        file_offset: int,
        record_hash: str,
        quota_window_id: int,
        token_cost_units: int,
        logical_clock_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE samples
            SET state='PERSISTED', decision='PERSISTED', reason=NULL,
                updated_version=?, flush_clock_ms=?
            WHERE sample_id=? AND state!='PERSISTED'
            """,
            (self.version, logical_clock_ms, sample["sample_id"]),
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO persisted_samples(
                sample_id, tenant, metric, partition, partition_arrival_seq,
                logical_time_ms, sample_bytes, data_file, file_offset,
                record_hash, quota_window_id, token_cost_units,
                flush_clock_ms, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample["sample_id"],
                sample["tenant"],
                sample["metric"],
                sample["partition"],
                sample["partition_arrival_seq"],
                sample["logical_time_ms"],
                sample["sample_bytes"],
                data_file,
                file_offset,
                record_hash,
                quota_window_id,
                token_cost_units,
                logical_clock_ms,
                self.version,
            ),
        )

    def drop_sample(
        self,
        sample_id: str,
        *,
        reason: str,
        decision: str = "DROPPED",
        first_constraint: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE samples
            SET state='DROPPED', decision=?, reason=?,
                first_constraint_json=COALESCE(?, first_constraint_json),
                updated_version=?
            WHERE sample_id=? AND state='BUFFERED'
            """,
            (
                decision,
                reason,
                _json(first_constraint) if first_constraint else None,
                self.version,
                sample_id,
            ),
        )

    def update_buffer_decision(self, sample_id: str, decision: str, reason: str | None) -> None:
        self.conn.execute(
            """
            UPDATE samples SET decision=?, reason=?, updated_version=?
            WHERE sample_id=? AND state='BUFFERED'
            """,
            (decision, reason, self.version, sample_id),
        )

    def record_attempt(
        self,
        *,
        round_id: str,
        sample: dict[str, Any],
        start_state: str,
        outcome: str,
        terminal_state: str,
        reason: str | None,
        required_units: int | None = None,
        available_units: int | None = None,
        first_constraint: dict[str, Any] | None = None,
        budget_before_ms: int | None = None,
        budget_after_ms: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO attempts(
                round_id, sample_id, partition, start_state, outcome,
                terminal_state, reason,
                required_units, available_units, first_constraint_json,
                budget_before_ms, budget_after_ms, created_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                round_id,
                sample["sample_id"],
                sample["partition"],
                start_state,
                outcome,
                terminal_state,
                reason,
                required_units,
                available_units,
                _json(first_constraint) if first_constraint else None,
                budget_before_ms,
                budget_after_ms,
                self.version,
            ),
        )

    def record_round(
        self,
        *,
        round_id: str,
        trigger: str,
        logical_clock_ms: int,
        budget_ms: int,
        spent_ms: int,
        candidate_count: int,
        persisted_count: int,
        buffered_count: int,
        abandoned_count: int,
        deferred_count: int,
        token_balance_after_units: int,
        summary: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO flush_rounds(
                round_id, version, trigger, logical_clock_ms, budget_ms,
                spent_ms, candidate_count, persisted_count, buffered_count,
                abandoned_count, deferred_count, token_balance_after_units,
                summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                round_id,
                self.version,
                trigger,
                logical_clock_ms,
                budget_ms,
                spent_ms,
                candidate_count,
                persisted_count,
                buffered_count,
                abandoned_count,
                deferred_count,
                token_balance_after_units,
                _json(summary),
            ),
        )

    def record_precision_sensitive(
        self,
        *,
        round_id: str,
        kind: str,
        entity_a: str,
        entity_b: str,
        availability_a_units: int,
        availability_b_units: int,
        delta_units: int,
        tolerance_units: int,
        decision_depends: bool,
        sources: list[dict[str, Any]],
        logical_clock_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO precision_sensitive(
                round_id, kind, entity_a, entity_b, availability_a_units,
                availability_b_units, delta_units, tolerance_units,
                decision_depends, sources_json, logical_clock_ms, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                round_id,
                kind,
                entity_a,
                entity_b,
                availability_a_units,
                availability_b_units,
                delta_units,
                tolerance_units,
                int(decision_depends),
                _json(sources),
                logical_clock_ms,
                self.version,
            ),
        )

    def precision_sources(
        self,
        *,
        tenant: str | None,
        window_id: int | None,
        partition: int | None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        clauses = []
        args: list[Any] = []
        if tenant is not None:
            clauses.append("tenant=?")
            args.append(tenant)
        if window_id is not None:
            clauses.append("(window_id=? OR window_id IS NULL)")
            args.append(window_id)
        if partition is not None:
            clauses.append("(partition=? OR partition IS NULL)")
            args.append(partition)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT id, scope, step, tenant, window_id, partition, scale,
                   rounding_direction, discarded_remainder_units,
                   cumulative_error_units
            FROM precision_ops{where}
            ORDER BY id DESC LIMIT ?
            """,
            (*args, limit),
        ).fetchall()
        return [
            {
                "register_id": int(row["id"]),
                "scope": row["scope"],
                "step": row["step"],
                "tenant": row["tenant"],
                "window_id": row["window_id"],
                "partition": row["partition"],
                "scale": int(row["scale"]),
                "rounding_direction": row["rounding_direction"],
                "confirmed_remainder_units": row["discarded_remainder_units"],
                "cumulative_error_bound_units": row["cumulative_error_units"],
            }
            for row in rows
        ]

    def record_recovery(
        self,
        *,
        action: str,
        sample_id: str | None,
        partition: int | None,
        detail: dict[str, Any],
        logical_clock_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO recovery_events(
                sample_id, partition, action, detail_json, logical_clock_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (sample_id, partition, action, _json(detail), logical_clock_ms),
        )

    def persisted_sample(self, sample_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM persisted_samples WHERE sample_id=?", (sample_id,)
        ).fetchone()
        return dict(row) if row else None

