from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_dumps, sha256_json
from .errors import PolicyIntegrityError, PolicyRollbackError, StateConflictError

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clock_watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    clock_ms INTEGER NOT NULL,
    tolerance_ms INTEGER NOT NULL DEFAULT 0,
    updated_round_id TEXT
);
CREATE TABLE IF NOT EXISTS app_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
    object_key TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    last_access_clock_ms INTEGER NOT NULL CHECK (last_access_clock_ms >= 0),
    access_count INTEGER NOT NULL CHECK (access_count >= 0),
    heat_weight INTEGER NOT NULL,
    declared_tier TEXT NOT NULL CHECK (declared_tier IN ('hot','cold')),
    current_tier TEXT NOT NULL CHECK (current_tier IN ('hot','cold')),
    content_digest TEXT NOT NULL,
    blob_path TEXT,
    manifest_version INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY CHECK (seq >= 0),
    object_key TEXT NOT NULL REFERENCES objects(object_key),
    event_type TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_versions (
    manifest_version INTEGER PRIMARY KEY,
    generation INTEGER NOT NULL,
    generation_floor INTEGER NOT NULL,
    package_version TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    object_set_digest TEXT,
    decision TEXT NOT NULL,
    package_json TEXT NOT NULL,
    applied_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_entries (
    manifest_version INTEGER NOT NULL REFERENCES policy_versions(manifest_version),
    policy_key TEXT NOT NULL,
    value_json TEXT,
    source_format TEXT NOT NULL,
    source_version TEXT NOT NULL,
    declared_clock_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    absolute_effective_ms INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    PRIMARY KEY (manifest_version, policy_key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    policy_version INTEGER NOT NULL,
    budget_bytes INTEGER NOT NULL CHECK (budget_bytes > 0),
    claimed_bytes INTEGER NOT NULL DEFAULT 0 CHECK (claimed_bytes >= 0),
    spent_bytes INTEGER NOT NULL DEFAULT 0 CHECK (spent_bytes >= 0),
    concurrency_limit INTEGER NOT NULL CHECK (concurrency_limit > 0),
    status TEXT NOT NULL DEFAULT 'open',
    created_clock_ms INTEGER NOT NULL,
    finished_clock_ms INTEGER,
    CHECK (claimed_bytes <= budget_bytes)
);
CREATE TABLE IF NOT EXISTS migration_jobs (
    job_key TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    policy_version INTEGER NOT NULL,
    object_key TEXT NOT NULL REFERENCES objects(object_key),
    source_tier TEXT NOT NULL,
    target_tier TEXT NOT NULL,
    direction TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    urgency INTEGER NOT NULL DEFAULT 0,
    plan_rank INTEGER NOT NULL DEFAULT 0,
    cost_total_scaled INTEGER NOT NULL DEFAULT 0,
    cost_error_bound TEXT NOT NULL DEFAULT '0/1',
    precision_sensitive INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    outcome_category TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    backoff_until_clock_ms INTEGER,
    owner_id TEXT,
    owner_pid INTEGER,
    lease_expires_at REAL,
    fail_reason TEXT,
    planned_clock_ms INTEGER NOT NULL,
    claimed_clock_ms INTEGER,
    started_clock_ms INTEGER,
    finished_clock_ms INTEGER,
    UNIQUE (round_id, policy_version, object_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON migration_jobs(round_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_object ON migration_jobs(object_key);
CREATE INDEX IF NOT EXISTS idx_jobs_dispatch ON migration_jobs(round_id, status, plan_rank);
CREATE TABLE IF NOT EXISTS capacity_accounts (
    tier TEXT PRIMARY KEY CHECK (tier IN ('hot','cold')),
    capacity_bytes INTEGER NOT NULL CHECK (capacity_bytes > 0),
    used_bytes INTEGER NOT NULL CHECK (used_bytes >= 0),
    reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
    updated_clock_ms INTEGER NOT NULL,
    CHECK (used_bytes + reserved_bytes <= capacity_bytes)
);
CREATE TABLE IF NOT EXISTS capacity_ledger (
    entry_key TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    job_key TEXT,
    tier TEXT NOT NULL,
    delta_used INTEGER NOT NULL,
    delta_reserved INTEGER NOT NULL,
    reason TEXT NOT NULL,
    clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cost_ledgers (
    ledger_key TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    object_key TEXT NOT NULL,
    total_cost_scaled INTEGER NOT NULL,
    ledger_json TEXT NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    UNIQUE (round_id, policy_version, object_key)
);
CREATE TABLE IF NOT EXISTS watermarks (
    name TEXT PRIMARY KEY,
    value_integer INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL
);
"""


@contextmanager
def process_file_lock(lock_path: str | Path) -> Iterator[None]:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def connect_database(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def reset_database_files(db_path: str | Path) -> None:
    path = Path(db_path)
    for suffix in ["", "-wal", "-shm"]:
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()


def object_set_digest(objects: list[dict[str, Any]]) -> str:
    return sha256_json(
        [
            {
                "key": obj["key"],
                "size_bytes": int(obj["size_bytes"]),
                "content_digest": obj["content_digest"],
            }
            for obj in sorted(objects, key=lambda item: item["key"])
        ]
    )


class StateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.parent / "tierflow-file.lock"
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect_database(self.db_path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with process_file_lock(self.lock_path):
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value if isinstance(value, str) else json.dumps(value, sort_keys=True)),
        )

    def initialize_bundle(
        self,
        manifest: dict[str, Any],
        config: dict[str, Any],
        events: dict[str, Any],
        *,
        clock_ms: int,
        reset: bool = False,
    ) -> dict[str, Any]:
        if reset:
            self.close()
            reset_database_files(self.db_path)
        self.init_schema()
        digest = object_set_digest(manifest["objects"])
        with self.write() as conn:
            self.set_meta(conn, "object_set_digest", digest)
            self.set_meta(conn, "manifest_version", manifest.get("manifest_version", 1))
            conn.execute(
                "INSERT INTO app_config(id, config_json) VALUES(1, ?) "
                "ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json",
                (canonical_dumps(config, indent=None),),
            )
            for obj in manifest["objects"]:
                conn.execute(
                    """
                    INSERT INTO objects(
                        object_key, size_bytes, last_access_clock_ms, access_count,
                        heat_weight, declared_tier, current_tier, content_digest,
                        blob_path, manifest_version
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(object_key) DO UPDATE SET
                        size_bytes=excluded.size_bytes,
                        last_access_clock_ms=excluded.last_access_clock_ms,
                        access_count=excluded.access_count,
                        heat_weight=excluded.heat_weight,
                        declared_tier=excluded.declared_tier,
                        content_digest=excluded.content_digest,
                        blob_path=excluded.blob_path,
                        manifest_version=excluded.manifest_version
                    """,
                    (
                        obj["key"],
                        obj["size_bytes"],
                        obj["last_access_clock_ms"],
                        obj["access_count"],
                        obj["heat_weight"],
                        obj["declared_tier"],
                        obj["declared_tier"],
                        obj["content_digest"],
                        obj.get("blob_path"),
                        int(manifest.get("manifest_version", 1)),
                    ),
                )

            for tier, capacity_field in [
                ("hot", "hot_capacity_bytes"),
                ("cold", "cold_capacity_bytes"),
            ]:
                used = conn.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM objects WHERE current_tier = ?",
                    (tier,),
                ).fetchone()["total"]
                conn.execute(
                    """
                    INSERT INTO capacity_accounts(tier, capacity_bytes, used_bytes, reserved_bytes, updated_clock_ms)
                    VALUES(?, ?, ?, 0, ?)
                    ON CONFLICT(tier) DO UPDATE SET
                        capacity_bytes=excluded.capacity_bytes,
                        updated_clock_ms=excluded.updated_clock_ms
                    """,
                    (tier, config[capacity_field], used, clock_ms),
                )

            for event in events["events"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO events(seq, object_key, event_type, clock_ms, payload_json)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        int(event["seq"]),
                        event["object_key"],
                        event["event_type"],
                        int(event.get("clock_ms", event.get("clock", 0))),
                        canonical_dumps(event, indent=None),
                    ),
                )
            max_seq = conn.execute("SELECT COALESCE(MAX(seq), -1) AS seq FROM events").fetchone()["seq"]
            conn.execute(
                """
                INSERT INTO watermarks(name, value_integer, updated_clock_ms)
                VALUES('event_seq', ?, ?)
                ON CONFLICT(name) DO UPDATE SET value_integer=MAX(watermarks.value_integer, excluded.value_integer)
                """,
                (max_seq, clock_ms),
            )
            max_object_clock = max(int(obj["last_access_clock_ms"]) for obj in manifest["objects"])
            max_event_clock = max(
                [int(event.get("clock_ms", event.get("clock", 0))) for event in events["events"]] or [0]
            )
            initial_watermark = max(clock_ms, max_object_clock, max_event_clock)
            tolerance = int(config.get("clock_rollback_tolerance_ms", 0))
            conn.execute(
                """
                INSERT INTO clock_watermark(id, clock_ms, tolerance_ms)
                VALUES(1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    clock_ms=MAX(clock_watermark.clock_ms, excluded.clock_ms),
                    tolerance_ms=excluded.tolerance_ms
                """,
                (initial_watermark, tolerance),
            )
        return {"object_set_digest": digest, "clock_watermark_ms": initial_watermark}

    def get_config(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT config_json FROM app_config WHERE id = 1").fetchone()
        if row is None:
            raise StateConflictError("state database has no configuration; run load first")
        return json.loads(row["config_json"])

    def get_objects(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM objects ORDER BY object_key").fetchall()
        return [dict(row) for row in rows]

    def get_clock_watermark(self) -> tuple[int, int]:
        row = self.conn.execute("SELECT clock_ms, tolerance_ms FROM clock_watermark WHERE id = 1").fetchone()
        if row is None:
            config = self.get_config()
            return 0, int(config.get("clock_rollback_tolerance_ms", 0))
        return int(row["clock_ms"]), int(row["tolerance_ms"])

    def advance_clock(self, conn: sqlite3.Connection, clock_ms: int, round_id: str | None = None) -> None:
        conn.execute(
            """
            INSERT INTO clock_watermark(id, clock_ms, tolerance_ms, updated_round_id)
            VALUES(1, ?, 0, ?)
            ON CONFLICT(id) DO UPDATE SET
                clock_ms=MAX(clock_watermark.clock_ms, excluded.clock_ms),
                updated_round_id=excluded.updated_round_id
            """,
            (clock_ms, round_id),
        )

    def current_policy(self) -> dict[str, Any] | None:
        version = self.get_meta("policy_current_version")
        if version is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM policy_versions WHERE manifest_version = ?",
            (int(version),),
        ).fetchone()
        return None if row is None else dict(row)

    def current_policy_entries(self) -> dict[str, dict[str, Any]]:
        current = self.current_policy()
        if current is None:
            return {}
        rows = self.conn.execute(
            "SELECT policy_key, entry_json FROM policy_entries WHERE manifest_version = ? ORDER BY policy_key",
            (current["manifest_version"],),
        ).fetchall()
        return {row["policy_key"]: json.loads(row["entry_json"]) for row in rows}

    def apply_policy_package(
        self,
        package: dict[str, Any],
        *,
        applied_clock_ms: int,
        update_kind: str = "full",
    ) -> dict[str, Any]:
        required = ["manifest_version", "generation", "generation_floor", "entries", "snapshot_digest"]
        missing = [key for key in required if key not in package]
        if missing:
            raise PolicyIntegrityError("policy package missing fields", {"missing": missing})

        with self.write() as conn:
            current_row = conn.execute(
                "SELECT * FROM policy_versions WHERE manifest_version = (SELECT CAST(value AS INTEGER) FROM meta WHERE key='policy_current_version')"
            ).fetchone()
            current = dict(current_row) if current_row else None
            stored_object_digest = self.get_meta("object_set_digest")
            if package.get("object_set_digest") and stored_object_digest:
                if package["object_set_digest"] != stored_object_digest and update_kind != "full_force":
                    raise PolicyIntegrityError(
                        "policy object-set digest does not match loaded object set",
                        {
                            "package_object_set_digest": package["object_set_digest"],
                            "loaded_object_set_digest": stored_object_digest,
                        },
                    )

            if current is not None:
                incoming_generation_floor = int(package.get("generation_floor", package["generation"]))
                if (
                    int(package["manifest_version"]) < int(current["manifest_version"])
                    or int(package["generation"]) < int(current["generation"])
                    or incoming_generation_floor < int(current["generation_floor"])
                ):
                    raise PolicyRollbackError(
                        "policy manifest version, generation, or generation floor rollback is rejected",
                        {
                            "incoming_manifest_version": package["manifest_version"],
                            "current_manifest_version": current["manifest_version"],
                            "incoming_generation": package["generation"],
                            "current_generation": current["generation"],
                            "incoming_generation_floor": incoming_generation_floor,
                            "current_generation_floor": current["generation_floor"],
                        },
                    )
                if int(package["manifest_version"]) == int(current["manifest_version"]):
                    if package["snapshot_digest"] == current["snapshot_digest"]:
                        return {
                            "decision": "noop",
                            "manifest_version": current["manifest_version"],
                            "generation": current["generation"],
                            "reason": "identical manifest version and snapshot digest",
                        }
                    raise PolicyIntegrityError(
                        "same manifest version has a different snapshot digest",
                        {"manifest_version": package["manifest_version"]},
                    )

                if update_kind == "incremental":
                    decision = "incremental_append"
                elif update_kind == "full":
                    decision = "full_replace"
                else:
                    old_shards = json.loads(current["package_json"]).get("shard_hashes", {})
                    new_shards = package.get("shard_hashes", {})
                    changed = any(old_shards.get(key) != value for key, value in new_shards.items())
                    removed = set(old_shards) - set(new_shards)
                    decision = "full_replace" if changed or removed else "incremental_append"
            else:
                decision = "full_replace"

            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS policy_snapshot_stage ("
                "manifest_version INTEGER NOT NULL, policy_key TEXT NOT NULL, "
                "value_json TEXT, source_format TEXT, source_version TEXT, "
                "declared_clock_ms INTEGER, generation INTEGER, "
                "absolute_effective_ms INTEGER, entry_json TEXT)"
            )
            conn.execute("DELETE FROM policy_snapshot_stage")
            entries = package["entries"]
            conn.executemany(
                """
                INSERT INTO policy_snapshot_stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(package["manifest_version"]),
                        key,
                        canonical_dumps(entry.get("value"), indent=None),
                        entry.get("source_format", package.get("source_format", "package")),
                        str(entry.get("source_version", package.get("package_version", "unknown"))),
                        int(entry.get("declared_clock_ms", applied_clock_ms)),
                        int(entry.get("generation", package["generation"])),
                        int(entry.get("absolute_effective_ms", applied_clock_ms)),
                        canonical_dumps(entry, indent=None),
                    )
                    for key, entry in sorted(entries.items())
                ],
            )
            conn.execute(
                """
                INSERT INTO policy_versions(
                    manifest_version, generation, generation_floor, package_version,
                    snapshot_digest, object_set_digest, decision, package_json, applied_clock_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(package["manifest_version"]),
                    int(package["generation"]),
                    int(package.get("generation_floor", package["generation"])),
                    str(package.get("package_version", "package")),
                    package["snapshot_digest"],
                    package.get("object_set_digest"),
                    decision,
                    canonical_dumps(package, indent=None),
                    applied_clock_ms,
                ),
            )
            conn.execute(
                """
                INSERT INTO policy_entries
                SELECT manifest_version, policy_key, value_json, source_format, source_version,
                       declared_clock_ms, generation, absolute_effective_ms, entry_json
                FROM policy_snapshot_stage
                """
            )
            self.set_meta(conn, "policy_current_version", int(package["manifest_version"]))
            self.advance_clock(conn, applied_clock_ms)
            return {
                "decision": decision,
                "manifest_version": int(package["manifest_version"]),
                "generation": int(package["generation"]),
                "entry_count": len(entries),
                "snapshot_digest": package["snapshot_digest"],
            }

    def list_jobs(self, round_id: str | None = None) -> list[dict[str, Any]]:
        if round_id:
            rows = self.conn.execute(
                "SELECT * FROM migration_jobs WHERE round_id = ? ORDER BY job_key",
                (round_id,),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM migration_jobs ORDER BY job_key").fetchall()
        return [dict(row) for row in rows]

    def accounts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM capacity_accounts ORDER BY tier").fetchall()
        return [dict(row) for row in rows]

    def watermarks(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT name, value_integer FROM watermarks ORDER BY name").fetchall()
        return {row["name"]: int(row["value_integer"]) for row in rows}
