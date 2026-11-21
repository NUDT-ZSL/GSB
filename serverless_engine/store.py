"""SQLite ledger with versioned configuration/snapshot pointers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import bad
from .models import FunctionManifest, LayerManifest, Observation, SnapshotMerge
from .util import canonical_json, sha256_json
from .validation import parse_function_manifest, parse_layer_manifest


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pointers (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    config_version INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    call_watermark INTEGER NOT NULL,
    patch_version INTEGER NOT NULL,
    instance_seq INTEGER NOT NULL,
    warm_generation_seq INTEGER NOT NULL,
    revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS config_commits (
    version INTEGER PRIMARY KEY,
    manifest_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    global_concurrency_budget INTEGER NOT NULL,
    source TEXT NOT NULL,
    committed_clock_ms INTEGER NOT NULL,
    created_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS functions (
    config_version INTEGER NOT NULL,
    name TEXT NOT NULL,
    runtime TEXT NOT NULL,
    memory_mib INTEGER NOT NULL,
    max_concurrency INTEGER NOT NULL,
    warm_pool_target INTEGER NOT NULL,
    warm_ttl_ms INTEGER NOT NULL,
    init_base_ms INTEGER NOT NULL,
    layer_refs_json TEXT NOT NULL,
    layer_params_json TEXT NOT NULL,
    PRIMARY KEY (config_version, name)
);

CREATE TABLE IF NOT EXISTS layers (
    config_version INTEGER NOT NULL,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    version INTEGER NOT NULL,
    default_init_ms INTEGER NOT NULL,
    tombstone INTEGER NOT NULL,
    env_json TEXT NOT NULL,
    depends_json TEXT NOT NULL,
    referenced_by_json TEXT NOT NULL,
    PRIMARY KEY (config_version, name)
);

CREATE TABLE IF NOT EXISTS snapshot_commits (
    version INTEGER PRIMARY KEY,
    payload_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    committed_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_observations (
    snapshot_version INTEGER NOT NULL,
    function_name TEXT NOT NULL,
    source TEXT NOT NULL,
    absolute_init_ms INTEGER NOT NULL,
    observed_clock_ms INTEGER NOT NULL,
    warm_generation INTEGER NOT NULL,
    warm_hit INTEGER NOT NULL,
    hit_watermark INTEGER NOT NULL,
    scale_num INTEGER NOT NULL,
    scale_den INTEGER NOT NULL,
    rounding TEXT NOT NULL,
    conversion_error_bound_ms REAL NOT NULL,
    selection_reason TEXT NOT NULL,
    PRIMARY KEY (snapshot_version, function_name)
);

CREATE TABLE IF NOT EXISTS snapshot_migrations (
    snapshot_version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    function_name TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_version, ordinal)
);

CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    function_name TEXT NOT NULL,
    state TEXT NOT NULL,
    bound_versions_json TEXT NOT NULL,
    warm_generation INTEGER NOT NULL,
    active_occupancy INTEGER NOT NULL,
    capacity INTEGER NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    init_started_clock_ms INTEGER,
    ready_clock_ms INTEGER,
    expires_clock_ms INTEGER,
    invalidated_clock_ms INTEGER,
    invalidation_source TEXT,
    pending_invalidation INTEGER NOT NULL DEFAULT 0,
    config_version INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    init_cache_key TEXT,
    init_duration_ms INTEGER,
    init_error_bound_ms REAL NOT NULL DEFAULT 0,
    decision_basis_json TEXT NOT NULL,
    warm_pool INTEGER NOT NULL DEFAULT 0,
    last_event_seq INTEGER
);

CREATE TABLE IF NOT EXISTS init_cache (
    cache_key TEXT PRIMARY KEY,
    function_name TEXT NOT NULL,
    bound_versions_json TEXT NOT NULL,
    digest_signature TEXT NOT NULL,
    init_duration_ms INTEGER NOT NULL,
    error_bound_ms REAL NOT NULL,
    cache_state TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    invalidated_clock_ms INTEGER,
    invalidation_source TEXT,
    config_version INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    call_key TEXT PRIMARY KEY,
    call_seq INTEGER NOT NULL,
    function_name TEXT NOT NULL,
    occupancy INTEGER NOT NULL,
    arrival_clock_ms INTEGER NOT NULL,
    effective_arrival_clock_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    wait_budget_ms INTEGER NOT NULL,
    deadline_clock_ms INTEGER NOT NULL,
    cancel_clock_ms INTEGER,
    phase TEXT NOT NULL,
    start_mode TEXT,
    cache_hit INTEGER,
    outcome TEXT,
    reason_code TEXT,
    reason TEXT,
    instance_id TEXT,
    config_version INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    committed_clock_ms INTEGER NOT NULL,
    init_started_clock_ms INTEGER,
    started_clock_ms INTEGER,
    finish_clock_ms INTEGER,
    completed_clock_ms INTEGER,
    result_json TEXT,
    decision_json TEXT,
    UNIQUE (call_seq, config_version)
);

CREATE TABLE IF NOT EXISTS concurrency_leases (
    lease_id TEXT PRIMARY KEY,
    call_key TEXT NOT NULL,
    function_name TEXT NOT NULL,
    occupancy INTEGER NOT NULL,
    acquired_clock_ms INTEGER NOT NULL,
    expires_clock_ms INTEGER NOT NULL,
    released INTEGER NOT NULL DEFAULT 0,
    release_clock_ms INTEGER
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    patch_hash TEXT NOT NULL,
    base_config_version INTEGER NOT NULL,
    resulting_config_version INTEGER NOT NULL,
    applied_clock_ms INTEGER NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patch_id TEXT NOT NULL,
    trigger_source_json TEXT NOT NULL,
    affected_functions_json TEXT NOT NULL,
    affected_instances_json TEXT NOT NULL,
    affected_cache_json TEXT NOT NULL,
    first_instance_id TEXT,
    config_version INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    result_json TEXT
);

CREATE TABLE IF NOT EXISTS instance_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    call_key TEXT,
    clock_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precision_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    candidate_a TEXT NOT NULL,
    candidate_b TEXT NOT NULL,
    score_a REAL NOT NULL,
    score_b REAL NOT NULL,
    difference REAL NOT NULL,
    tolerance REAL NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    source_chain_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instances_function_state
    ON instances(function_name, state);
CREATE INDEX IF NOT EXISTS idx_calls_phase ON calls(phase);
CREATE INDEX IF NOT EXISTS idx_leases_active ON concurrency_leases(released, expires_clock_ms);
CREATE INDEX IF NOT EXISTS idx_invalidations_patch ON invalidations(patch_id);
"""


def snapshot_commit_payload(merge: SnapshotMerge) -> dict[str, Any]:
    return {
        "v1": merge.v1_payload,
        "v2": merge.v2_payload,
        "merged": [
            {
                "function": obs.function,
                "source": obs.source,
                "absolute_init_ms": obs.absolute_init_ms,
                "observed_clock_ms": obs.observed_clock_ms,
                "warm_generation": obs.warm_generation,
                "warm_hit": obs.warm_hit,
                "hit_watermark": obs.hit_watermark,
                "scale_num": obs.scale_num,
                "scale_den": obs.scale_den,
                "rounding": obs.rounding,
                "conversion_error_bound_ms": obs.conversion_error_bound_ms,
                "selection_reason": obs.selection_reason,
            }
            for obs in sorted(merge.observations.values(), key=lambda item: item.function)
        ],
        "migrations": list(merge.migrations),
    }


class Store:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "engine.sqlite3"
        self.artifacts_dir = self.state_dir / "artifacts"

    def exists(self) -> bool:
        return self.db_path.exists()

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            if not self.db_path.exists():
                raise bad("E_NOT_LOADED", "engine state does not exist", str(self.state_dir))
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        else:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if not readonly:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
            )
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect(readonly=True)
        try:
            connection.execute("BEGIN")
            yield connection
            connection.rollback()
        finally:
            connection.close()

    @staticmethod
    def pointer(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM pointers WHERE singleton = 1").fetchone()
        if row is None:
            raise bad("E_NOT_LOADED", "engine has no committed pointer", "$.pointers")
        return row

    @staticmethod
    def touch_pointer(connection: sqlite3.Connection, **updates: Any) -> None:
        allowed = {
            "config_version",
            "snapshot_version",
            "logical_clock_ms",
            "call_watermark",
            "patch_version",
            "instance_seq",
            "warm_generation_seq",
        }
        invalid = sorted(set(updates) - allowed)
        if invalid:
            raise AssertionError(f"invalid pointer fields: {invalid}")
        current = Store.pointer(connection)
        assignments = ["revision = revision + 1"]
        values: list[Any] = []
        for key in sorted(updates):
            assignments.append(f"{key} = ?")
            values.append(updates[key])
        connection.execute(
            f"UPDATE pointers SET {', '.join(assignments)} WHERE singleton = 1",
            values,
        )

    @staticmethod
    def insert_config(
        connection: sqlite3.Connection,
        *,
        version: int,
        function_manifest: FunctionManifest,
        layer_manifest: LayerManifest,
        payload: dict[str, Any],
        source: str,
        committed_clock_ms: int,
        revision: int,
    ) -> None:
        manifest_hash = sha256_json(payload)
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS stage_config (
                version INTEGER NOT NULL,
                manifest_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                global_concurrency_budget INTEGER NOT NULL,
                source TEXT NOT NULL,
                committed_clock_ms INTEGER NOT NULL,
                created_revision INTEGER NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM stage_config")
        connection.execute(
            """
            INSERT INTO stage_config VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                manifest_hash,
                canonical_json(payload),
                function_manifest.global_concurrency_budget,
                source,
                committed_clock_ms,
                revision,
            ),
        )
        staged = connection.execute("SELECT * FROM stage_config").fetchone()
        existing = connection.execute(
            "SELECT manifest_hash FROM config_commits WHERE version = ?", (version,)
        ).fetchone()
        if existing is not None:
            if existing["manifest_hash"] == manifest_hash:
                return
            raise bad(
                "E_VERSION_CONFLICT",
                f"config version {version} already exists with different content",
                "$.config_version",
            )
        connection.execute(
            """
            INSERT INTO config_commits(
                version, manifest_hash, payload_json, global_concurrency_budget,
                source, committed_clock_ms, created_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                staged["manifest_hash"],
                staged["payload_json"],
                staged["global_concurrency_budget"],
                staged["source"],
                staged["committed_clock_ms"],
                staged["created_revision"],
            ),
        )
        for name in sorted(layer_manifest.layers):
            layer = layer_manifest.layers[name]
            connection.execute(
                """
                INSERT INTO layers(
                    config_version, name, digest, version, default_init_ms,
                    tombstone, env_json, depends_json, referenced_by_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version,
                    name,
                    layer.digest,
                    layer.version,
                    layer.default_init_ms,
                    1 if layer.tombstone else 0,
                    canonical_json(layer.env),
                    canonical_json(list(layer.depends_on)),
                    canonical_json(list(layer.referenced_by)),
                ),
            )
        for name in sorted(function_manifest.functions):
            function = function_manifest.functions[name]
            connection.execute(
                """
                INSERT INTO functions(
                    config_version, name, runtime, memory_mib, max_concurrency,
                    warm_pool_target, warm_ttl_ms, init_base_ms,
                    layer_refs_json, layer_params_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version,
                    name,
                    function.runtime,
                    function.memory_mib,
                    function.max_concurrency,
                    function.warm_pool_target,
                    function.warm_ttl_ms,
                    function.init_base_ms,
                    canonical_json(list(function.layer_refs)),
                    canonical_json(function.layer_params),
                ),
            )

    @staticmethod
    def insert_snapshot(
        connection: sqlite3.Connection,
        *,
        version: int,
        merge: SnapshotMerge,
        committed_clock_ms: int,
    ) -> None:
        payload = snapshot_commit_payload(merge)
        payload_hash = sha256_json(payload)
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS stage_snapshot (
                version INTEGER NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                committed_clock_ms INTEGER NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM stage_snapshot")
        connection.execute(
            "INSERT INTO stage_snapshot VALUES (?, ?, ?, ?)",
            (version, payload_hash, canonical_json(payload), committed_clock_ms),
        )
        staged = connection.execute("SELECT * FROM stage_snapshot").fetchone()
        existing = connection.execute(
            "SELECT payload_hash FROM snapshot_commits WHERE version = ?", (version,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] == payload_hash:
                return
            raise bad(
                "E_VERSION_CONFLICT",
                f"snapshot version {version} already exists with different content",
                "$.snapshot_version",
            )
        connection.execute(
            """
            INSERT INTO snapshot_commits(
                version, payload_hash, payload_json, committed_clock_ms
            ) VALUES (?, ?, ?, ?)
            """,
            (
                version,
                staged["payload_hash"],
                staged["payload_json"],
                staged["committed_clock_ms"],
            ),
        )
        for obs in sorted(merge.observations.values(), key=lambda item: item.function):
            connection.execute(
                """
                INSERT INTO snapshot_observations(
                    snapshot_version, function_name, source, absolute_init_ms,
                    observed_clock_ms, warm_generation, warm_hit, hit_watermark,
                    scale_num, scale_den, rounding, conversion_error_bound_ms,
                    selection_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version,
                    obs.function,
                    obs.source,
                    obs.absolute_init_ms,
                    obs.observed_clock_ms,
                    obs.warm_generation,
                    1 if obs.warm_hit else 0,
                    obs.hit_watermark,
                    obs.scale_num,
                    obs.scale_den,
                    obs.rounding,
                    obs.conversion_error_bound_ms,
                    obs.selection_reason,
                ),
            )
        for ordinal, migration in enumerate(merge.migrations):
            connection.execute(
                """
                INSERT INTO snapshot_migrations(
                    snapshot_version, ordinal, function_name, detail_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    version,
                    ordinal,
                    migration["function"],
                    canonical_json(migration),
                ),
            )

    @staticmethod
    def load_config(
        connection: sqlite3.Connection, version: int
    ) -> tuple[FunctionManifest, LayerManifest, dict[str, Any]]:
        row = connection.execute(
            "SELECT payload_json FROM config_commits WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            raise bad("E_NOT_LOADED", f"config version {version} is not committed")
        payload = json.loads(row["payload_json"])
        layers = parse_layer_manifest({"layers": payload["layers"]})
        functions = parse_function_manifest(
            {
                "global_concurrency_budget": payload["global_concurrency_budget"],
                "functions": payload["functions"],
            },
            layers,
        )
        return functions, layers, payload

    @staticmethod
    def snapshot_observations(
        connection: sqlite3.Connection, version: int
    ) -> dict[str, Observation]:
        rows = connection.execute(
            """
            SELECT * FROM snapshot_observations
            WHERE snapshot_version = ?
            ORDER BY function_name
            """,
            (version,),
        ).fetchall()
        result: dict[str, Observation] = {}
        for row in rows:
            result[row["function_name"]] = Observation(
                function=row["function_name"],
                source=row["source"],
                absolute_init_ms=row["absolute_init_ms"],
                observed_clock_ms=row["observed_clock_ms"],
                warm_generation=row["warm_generation"],
                warm_hit=bool(row["warm_hit"]),
                hit_watermark=row["hit_watermark"],
                scale_num=row["scale_num"],
                scale_den=row["scale_den"],
                rounding=row["rounding"],
                conversion_error_bound_ms=row["conversion_error_bound_ms"],
                selection_reason=row["selection_reason"],
            )
        return result

