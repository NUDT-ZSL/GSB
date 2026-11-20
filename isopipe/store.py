from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_bytes, canonical_text, sha256_bytes, sha256_json
from .errors import error


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    manifest_path TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    latency_budget_ms INTEGER NOT NULL,
    clock_start_ms INTEGER NOT NULL,
    logical_watermark INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated_watermark INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_states (
    run_id TEXT NOT NULL,
    task_name TEXT NOT NULL,
    version TEXT,
    cache_key TEXT,
    permission_digest TEXT,
    components_json TEXT,
    phase TEXT NOT NULL,
    outcome TEXT,
    reason TEXT,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    recovered INTEGER NOT NULL DEFAULT 0,
    logical_start_ms INTEGER,
    logical_end_ms INTEGER,
    latency_ms INTEGER,
    output_path TEXT,
    output_digest TEXT,
    result_digest TEXT,
    staging_path TEXT,
    environment_json TEXT,
    stdout TEXT,
    stderr TEXT,
    error_code TEXT,
    error_message TEXT,
    updated_watermark INTEGER NOT NULL,
    PRIMARY KEY (run_id, task_name)
);
CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    task_name TEXT NOT NULL,
    version TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    output_digest TEXT NOT NULL,
    output_path TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state TEXT NOT NULL,
    invalidated_by TEXT,
    invalidation_reason TEXT,
    created_watermark INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_name TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    old_cache_key TEXT,
    new_cache_key TEXT,
    reason TEXT NOT NULL,
    watermark INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outputs (
    run_id TEXT NOT NULL,
    task_name TEXT NOT NULL,
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (run_id, task_name, path)
);
CREATE INDEX IF NOT EXISTS idx_cache_task ON cache_entries(task_name, state);
"""


def run_id_for(manifest_identity: str) -> str:
    return "run-" + sha256_bytes(manifest_identity.encode("utf-8"))[:24]


class Store:
    def __init__(self, db_path: Path, cache_dir: Path) -> None:
        self.db_path = db_path
        self.cache_dir = cache_dir
        self.connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30.0
        while True:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    self.db_path,
                    timeout=30.0,
                    isolation_level=None,
                    check_same_thread=True,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(SCHEMA_SQL)
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
                self.connection = connection
                return
            except sqlite3.OperationalError as exc:
                if connection is not None:
                    connection.close()
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    if "locked" in str(exc).lower():
                        raise error(
                            "E_LOCK_TIMEOUT",
                            "timed out initializing SQLite state",
                            path=str(self.db_path),
                        )
                    raise error(
                        "E_INTERNAL",
                        "failed to initialize SQLite state",
                        path=str(self.db_path),
                        reason=str(exc),
                    )
                time.sleep(0.05)

    def initialize_readonly(self) -> None:
        if not self.db_path.is_file():
            raise error(
                "E_STATE_NOT_FOUND",
                "state database does not exist",
                state_db=str(self.db_path),
            )
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_states'"
        ).fetchone()
        if row is None:
            connection.close()
            raise error(
                "E_STATE_NOT_FOUND",
                "SQLite database has no pipeline schema",
                state_db=str(self.db_path),
            )
        self.connection = connection

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise error("E_INTERNAL", "store is not initialized")
        return self.connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._conn()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise error(
                    "E_LOCK_TIMEOUT",
                    "timed out acquiring SQLite write transaction",
                    path=str(self.db_path),
                )
            raise
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def cache_path(self, cache_key: str) -> Path:
        return self.cache_dir / f"{cache_key}.json"

    def lock_path(self, cache_key: str) -> Path:
        return self.db_path.parent / "locks" / f"{cache_key}.lock"

    def ensure_run(
        self,
        *,
        run_id: str,
        manifest_path: str,
        manifest_digest: str,
        latency_budget_ms: int,
        clock_start_ms: int,
    ) -> str:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO pipeline_runs("
                    "run_id, manifest_path, manifest_digest, latency_budget_ms, "
                    "clock_start_ms, logical_watermark, status, updated_watermark"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        manifest_path,
                        manifest_digest,
                        latency_budget_ms,
                        clock_start_ms,
                        clock_start_ms,
                        "running",
                        clock_start_ms,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE pipeline_runs SET manifest_path=?, latency_budget_ms=?, "
                    "clock_start_ms=?, status='running' WHERE run_id=?",
                    (manifest_path, latency_budget_ms, clock_start_ms, run_id),
                )
        return run_id

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
        ).fetchone()

    def get_watermark(self, run_id: str) -> int:
        row = self.get_run(run_id)
        return int(row["logical_watermark"]) if row else 0

    def advance_watermark(self, run_id: str, value: int) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT logical_watermark FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise error("E_INTERNAL", "pipeline run does not exist", run_id=run_id)
            current = int(row["logical_watermark"])
            if value < current:
                raise error(
                    "E_CLOCK_REGRESSION",
                    "logical clock moved backwards",
                    previous=current,
                    requested=value,
                )
            connection.execute(
                "UPDATE pipeline_runs SET logical_watermark=?, updated_watermark=? "
                "WHERE run_id=?",
                (value, value, run_id),
            )

    def finish_run(self, run_id: str, status: str, watermark: int) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT logical_watermark FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            current = int(row["logical_watermark"]) if row else watermark
            if watermark < current:
                raise error(
                    "E_CLOCK_REGRESSION",
                    "logical clock moved backwards",
                    previous=current,
                    requested=watermark,
                )
            connection.execute(
                "UPDATE pipeline_runs SET status=?, logical_watermark=?, updated_watermark=? "
                "WHERE run_id=?",
                (status, watermark, watermark, run_id),
            )

    def get_task(self, run_id: str, task_name: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM task_states WHERE run_id=? AND task_name=?",
            (run_id, task_name),
        ).fetchone()

    def tasks_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn()
            .execute(
                "SELECT * FROM task_states WHERE run_id=? ORDER BY task_name", (run_id,)
            )
            .fetchall()
        )

    def current_cache_entries(self, run_id: str) -> dict[str, sqlite3.Row]:
        rows = self._conn().execute(
            "SELECT c.* FROM cache_entries c "
            "JOIN task_states t ON t.cache_key=c.cache_key "
            "WHERE t.run_id=?",
            (run_id,),
        ).fetchall()
        return {str(row["cache_key"]): row for row in rows}

    def get_cache(self, cache_key: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM cache_entries WHERE cache_key=?", (cache_key,)
        ).fetchone()

    def mark_task_running(
        self,
        *,
        run_id: str,
        task_name: str,
        version: str,
        cache_key: str,
        permission_digest: str,
        components: dict[str, Any],
        logical_start_ms: int,
        logical_end_ms: int,
        output_path: str,
        environment: dict[str, str],
        watermark: int,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT logical_watermark FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            current = int(row["logical_watermark"])
            effective_watermark = max(watermark, current)
            connection.execute(
                "INSERT INTO task_states("
                "run_id, task_name, version, cache_key, permission_digest, components_json, "
                "phase, outcome, reason, cache_hit, recovered, logical_start_ms, logical_end_ms, "
                "latency_ms, output_path, environment_json, updated_watermark"
                ") VALUES(?,?,?,?,?,?,'running','running',NULL,0,0,?,?,?,?,?,?) "
                "ON CONFLICT(run_id, task_name) DO UPDATE SET "
                "version=excluded.version, cache_key=excluded.cache_key, "
                "permission_digest=excluded.permission_digest, "
                "components_json=excluded.components_json, phase='running', "
                "outcome='running', reason=NULL, cache_hit=0, recovered=0, "
                "logical_start_ms=excluded.logical_start_ms, "
                "logical_end_ms=excluded.logical_end_ms, latency_ms=excluded.latency_ms, "
                "output_path=excluded.output_path, output_digest=NULL, result_digest=NULL, "
                "staging_path=NULL, environment_json=excluded.environment_json, "
                "stdout=NULL, stderr=NULL, error_code=NULL, error_message=NULL, "
                "updated_watermark=excluded.updated_watermark",
                (
                    run_id,
                    task_name,
                    version,
                    cache_key,
                    permission_digest,
                    canonical_text(components),
                    logical_start_ms,
                    logical_end_ms,
                    logical_end_ms - logical_start_ms,
                    output_path,
                    canonical_text(environment),
                    effective_watermark,
                ),
            )
            connection.execute(
                "UPDATE pipeline_runs SET logical_watermark=?, updated_watermark=? "
                "WHERE run_id=?",
                (effective_watermark, effective_watermark, run_id),
            )

    def mark_task_status(
        self,
        *,
        run_id: str,
        task_name: str,
        phase: str,
        outcome: str,
        reason: str,
        logical_start_ms: int | None,
        logical_end_ms: int | None,
        latency_ms: int | None,
        error_code: str | None,
        error_message: str | None,
        watermark: int,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT logical_watermark FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            current = int(row["logical_watermark"])
            effective_watermark = max(watermark, current)
            connection.execute(
                "INSERT INTO task_states("
                "run_id, task_name, phase, outcome, reason, logical_start_ms, "
                "logical_end_ms, latency_ms, error_code, error_message, updated_watermark"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id, task_name) DO UPDATE SET "
                "phase=excluded.phase, outcome=excluded.outcome, reason=excluded.reason, "
                "logical_start_ms=excluded.logical_start_ms, "
                "logical_end_ms=excluded.logical_end_ms, latency_ms=excluded.latency_ms, "
                "error_code=excluded.error_code, error_message=excluded.error_message, "
                "updated_watermark=excluded.updated_watermark",
                (
                    run_id,
                    task_name,
                    phase,
                    outcome,
                    reason,
                    logical_start_ms,
                    logical_end_ms,
                    latency_ms,
                    error_code,
                    error_message,
                    effective_watermark,
                ),
            )
            connection.execute(
                "UPDATE pipeline_runs SET logical_watermark=?, updated_watermark=? "
                "WHERE run_id=?",
                (effective_watermark, effective_watermark, run_id),
            )

    def prepare_commit(
        self,
        *,
        run_id: str,
        task_name: str,
        output_path: str,
        staging_path: str,
        output_digest: str,
        result_digest: str,
        stdout: str,
        stderr: str,
        watermark: int,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT logical_watermark FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            current = int(row["logical_watermark"])
            effective_watermark = max(watermark, current)
            updated = connection.execute(
                "UPDATE task_states SET phase='prepared', output_path=?, staging_path=?, "
                "output_digest=?, result_digest=?, stdout=?, stderr=?, updated_watermark=? "
                "WHERE run_id=? AND task_name=? AND phase='running'",
                (
                    output_path,
                    staging_path,
                    output_digest,
                    result_digest,
                    stdout,
                    stderr,
                    effective_watermark,
                    run_id,
                    task_name,
                ),
            )
            if updated.rowcount != 1:
                raise error(
                    "E_INTERNAL",
                    "task was not in running phase during prepare",
                    task=task_name,
                )
            connection.execute(
                "UPDATE pipeline_runs SET logical_watermark=?, updated_watermark=? "
                "WHERE run_id=?",
                (effective_watermark, effective_watermark, run_id),
            )

    def commit_task(
        self,
        *,
        run_id: str,
        task_name: str,
        version: str,
        cache_key: str,
        cache_path: Path,
        output_path: str,
        output_digest: str,
        result_digest: str,
        result: dict[str, Any],
        cache_hit: bool,
        recovered: bool,
        stdout: str,
        stderr: str,
        watermark: int,
    ) -> None:
        if not cache_path.is_file():
            raise error(
                "E_CACHE_CORRUPT",
                "cache file is missing at commit",
                cache_key=cache_key,
                path=str(cache_path),
            )
        cache_data = cache_path.read_bytes()
        if sha256_bytes(cache_data) != result_digest:
            raise error(
                "E_DIGEST_MISMATCH",
                "cache digest does not match result digest",
                cache_key=cache_key,
                expected=result_digest,
                actual=sha256_bytes(cache_data),
            )
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT logical_watermark FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            current = int(row["logical_watermark"])
            effective_watermark = max(watermark, current)
            existing = connection.execute(
                "SELECT * FROM cache_entries WHERE cache_key=?", (cache_key,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO cache_entries("
                    "cache_key, task_name, version, result_digest, output_digest, "
                    "output_path, result_json, state, created_watermark"
                    ") VALUES(?,?,?,?,?,?,?,'valid',?)",
                    (
                        cache_key,
                        task_name,
                        version,
                        result_digest,
                        output_digest,
                        output_path,
                        canonical_text(result),
                        effective_watermark,
                    ),
                )
            elif (
                existing["result_digest"] != result_digest
                or existing["output_digest"] != output_digest
            ):
                raise error(
                    "E_DIGEST_MISMATCH",
                    "cache key was materialized with different content",
                    cache_key=cache_key,
                    existing_result_digest=existing["result_digest"],
                    new_result_digest=result_digest,
                )
            else:
                connection.execute(
                    "UPDATE cache_entries SET state='valid', invalidated_by=NULL, "
                    "invalidation_reason=NULL WHERE cache_key=?",
                    (cache_key,),
                )
            task_row = connection.execute(
                "SELECT version, cache_key FROM task_states WHERE run_id=? AND task_name=?",
                (run_id, task_name),
            ).fetchone()
            if task_row is None or task_row["version"] != version:
                raise error(
                    "E_INTERNAL",
                    "task version changed before commit",
                    task=task_name,
                )
            connection.execute(
                "UPDATE task_states SET phase='committed', outcome='succeeded', "
                "reason=?, cache_hit=?, recovered=?, output_path=?, output_digest=?, "
                "result_digest=?, stdout=?, stderr=?, error_code=NULL, error_message=NULL, "
                "updated_watermark=? WHERE run_id=? AND task_name=?",
                (
                    "recovered_half_commit" if recovered else None,
                    1 if cache_hit else 0,
                    1 if recovered else 0,
                    output_path,
                    output_digest,
                    result_digest,
                    stdout,
                    stderr,
                    effective_watermark,
                    run_id,
                    task_name,
                ),
            )
            connection.execute(
                "INSERT INTO outputs(run_id, task_name, path, digest, size) "
                "VALUES(?,?,?,?,?) ON CONFLICT(run_id, task_name, path) DO UPDATE SET "
                "digest=excluded.digest, size=excluded.size",
                (
                    run_id,
                    task_name,
                    output_path,
                    output_digest,
                    len(canonical_bytes(result)) + 1,
                ),
            )
            connection.execute(
                "UPDATE pipeline_runs SET logical_watermark=?, updated_watermark=? "
                "WHERE run_id=?",
                (effective_watermark, effective_watermark, run_id),
            )

    def invalidate_stale_cache(
        self,
        *,
        run_id: str,
        task_name: str,
        old_version: str | None,
        old_cache_key: str | None,
        old_components: dict[str, Any] | None,
        new_version: str,
        new_cache_key: str,
        new_components: dict[str, Any],
        watermark: int,
    ) -> dict[str, Any] | None:
        if not old_cache_key or old_cache_key == new_cache_key:
            return None
        trigger, reason = _invalidation_trigger(old_components, new_components)
        with self.transaction() as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM cache_invalidations WHERE run_id=? AND task_name=? "
                "AND old_cache_key=? AND new_cache_key=?",
                (run_id, task_name, old_cache_key, new_cache_key),
            ).fetchone()
            if duplicate is not None:
                return {
                    "task": task_name,
                    "trigger_source": trigger,
                    "reason": reason,
                    "old_cache_key": old_cache_key,
                    "new_cache_key": new_cache_key,
                }
            entry = connection.execute(
                "SELECT * FROM cache_entries WHERE cache_key=?", (old_cache_key,)
            ).fetchone()
            if entry is not None and entry["state"] == "valid":
                connection.execute(
                    "UPDATE cache_entries SET state='invalidated', invalidated_by=?, "
                    "invalidation_reason=? WHERE cache_key=?",
                    (new_cache_key, reason, old_cache_key),
                )
            connection.execute(
                "INSERT INTO cache_invalidations("
                "run_id, task_name, trigger_source, old_cache_key, new_cache_key, "
                "reason, watermark) VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    task_name,
                    trigger,
                    old_cache_key,
                    new_cache_key,
                    reason,
                    watermark,
                ),
            )
        return {
            "task": task_name,
            "trigger_source": trigger,
            "reason": reason,
            "old_cache_key": old_cache_key,
            "new_cache_key": new_cache_key,
        }

    def invalidations(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM cache_invalidations WHERE run_id=? ORDER BY task_name, id",
            (run_id,),
        ).fetchall()
        return [
            {
                "task": row["task_name"],
                "trigger_source": row["trigger_source"],
                "reason": row["reason"],
                "old_cache_key": row["old_cache_key"],
                "new_cache_key": row["new_cache_key"],
            }
            for row in rows
        ]

    def mark_cache_invalid(self, cache_key: str, reason: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE cache_entries SET state='invalidated', invalidation_reason=? "
                "WHERE cache_key=?",
                (reason, cache_key),
            )

    def integrity_check(self) -> str:
        row = self._conn().execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"


def components_digest(components: dict[str, Any]) -> str:
    return sha256_json(components)


def build_cache_components(
    *,
    kernel_version: str,
    fixed_scale: int,
    task: Any,
    input_digests: list[dict[str, Any]],
    dependencies: dict[str, dict[str, Any]],
    environment: dict[str, str],
) -> dict[str, Any]:
    return {
        "cache_format": "isopipe-cache-v1",
        "kernel_version": kernel_version,
        "fixed_scale": fixed_scale,
        "task": task.name,
        "permission_declaration": task.permission_declaration,
        "inputs": input_digests,
        "dependencies": {
            name: {
                "version": value.get("version"),
                "cache_key": value.get("cache_key"),
            }
            for name, value in sorted(dependencies.items())
        },
        "environment": {
            key: environment[key] for key in sorted(environment)
        },
    }


def cache_identity(components: dict[str, Any]) -> tuple[str, str]:
    version = components_digest(components)
    cache_key = "cache-" + sha256_bytes(("cache-v1:" + version).encode("utf-8"))
    return version, cache_key


def _first_input_difference(
    old: dict[str, Any] | None, new: dict[str, Any]
) -> str:
    if not old:
        return "input_digest"
    old_inputs = {item.get("path"): item for item in old.get("inputs", [])}
    for item in new.get("inputs", []):
        path = item.get("path")
        if old_inputs.get(path, {}).get("sha256") != item.get("sha256"):
            return f"input:{path}"
    if set(old_inputs) != {item.get("path") for item in new.get("inputs", [])}:
        return "input_set"
    return "input_digest"


def _invalidation_trigger(
    old: dict[str, Any] | None, new: dict[str, Any]
) -> tuple[str, str]:
    if not old:
        return "cache_identity", "new cache identity"
    if old.get("kernel_version") != new.get("kernel_version"):
        return "kernel_version", "kernel version changed"
    if old.get("fixed_scale") != new.get("fixed_scale"):
        return "fixed_scale", "fixed point scale changed"
    if old.get("permission_declaration") != new.get("permission_declaration"):
        return (
            f"permission_declaration:{new.get('task')}",
            "permission declaration changed",
        )
    old_deps = old.get("dependencies", {})
    new_deps = new.get("dependencies", {})
    for name in sorted(set(old_deps) | set(new_deps)):
        if old_deps.get(name, {}).get("cache_key") != new_deps.get(name, {}).get("cache_key"):
            return f"upstream:{name}", f"upstream cache identity changed for {name}"
        if old_deps.get(name, {}).get("result_digest") != new_deps.get(
            name, {}
        ).get("result_digest"):
            return f"upstream_result:{name}", f"upstream result changed for {name}"
    if old.get("environment") != new.get("environment"):
        return "environment", "injected environment changed"
    return _first_input_difference(old, new), "input digest changed"
