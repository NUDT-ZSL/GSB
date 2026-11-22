from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError, fail
from .models import LoadedSample, MergedRule
from .util import (
    FileLock,
    canonical_json,
    digest,
)


SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logical_tables (
    name TEXT PRIMARY KEY,
    descriptor_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS physical_mappings (
    logical_table TEXT NOT NULL,
    shard_no INTEGER NOT NULL,
    database_ref TEXT NOT NULL,
    database_absolute TEXT NOT NULL,
    physical_table TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (logical_table, shard_no)
);

CREATE TABLE IF NOT EXISTS rule_versions (
    version TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    rollback_target TEXT,
    source TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    model_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    apply_seq INTEGER NOT NULL DEFAULT 0,
    created_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS current_rule (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    pointer_seq INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_decisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    at_clock_ms INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_runs (
    query_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    request_key TEXT NOT NULL,
    template_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    source_filter TEXT NOT NULL,
    bound_rule_version TEXT NOT NULL,
    bound_rule_digest TEXT NOT NULL,
    bound_rule_source TEXT NOT NULL,
    bound_rule_model_json TEXT NOT NULL,
    plan_json TEXT,
    plan_digest TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    aggregate_json TEXT,
    result_json TEXT,
    failures_json TEXT,
    error_json TEXT,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    resume_watermark INTEGER NOT NULL DEFAULT 0,
    commit_seq INTEGER
);

CREATE TABLE IF NOT EXISTS shard_runs (
    query_id TEXT NOT NULL,
    shard_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    rows_json TEXT,
    row_count INTEGER,
    sum_value INTEGER,
    result_digest TEXT,
    error_code TEXT,
    error_json TEXT,
    updated_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (query_id, shard_no),
    FOREIGN KEY (query_id) REFERENCES query_runs(query_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS query_commits (
    query_id TEXT PRIMARY KEY,
    rule_version TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    commit_seq INTEGER NOT NULL,
    FOREIGN KEY (query_id) REFERENCES query_runs(query_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT,
    event TEXT NOT NULL,
    stage TEXT NOT NULL,
    details_json TEXT NOT NULL,
    at_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applied_operations (
    op_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    version TEXT,
    digest TEXT,
    result_json TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_clock ON rule_versions(effective_clock_ms, generation);
CREATE INDEX IF NOT EXISTS idx_shard_status ON shard_runs(query_id, status, shard_no);
CREATE INDEX IF NOT EXISTS idx_query_status ON query_runs(status);
"""


def _json_load(value: str | None, *, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            code="E_STATE_CORRUPT",
            message="persisted state contains invalid JSON",
            details={"error": str(exc)},
        ) from exc


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.state_dir = self.data_dir / "state"
        self.path = self.state_dir / "meta.sqlite"
        self.lock_path = self.data_dir / ".shard-router.lock"

    def lock(self) -> FileLock:
        return FileLock(self.lock_path)

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            if not self.path.exists():
                fail(
                    "E_STATE_NOT_LOADED",
                    "state database does not exist; run the load command first",
                    path=str(self.path),
                )
            uri = f"file:{self.path.as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        else:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(readonly=True)
        try:
            connection.execute("BEGIN")
            yield connection
            connection.rollback()
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        with self.lock():
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(SCHEMA_SQL)
                connection.commit()
            finally:
                connection.close()

    def reset_state(self) -> None:
        with self.lock():
            self.state_dir.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(SCHEMA_SQL)
                for table in (
                    "audit_log",
                    "query_commits",
                    "shard_runs",
                    "query_runs",
                    "rule_decisions",
                    "current_rule",
                    "rule_versions",
                    "physical_mappings",
                    "logical_tables",
                    "applied_operations",
                    "meta_kv",
                ):
                    connection.execute(f'DELETE FROM "{table}"')
                connection.commit()
            finally:
                connection.close()

    def initialize_from_sample(self, sample: LoadedSample, *, initial_clock_ms: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(SCHEMA_SQL)
            for table in (
                "audit_log",
                "query_commits",
                "shard_runs",
                "query_runs",
                "rule_decisions",
                "current_rule",
                "rule_versions",
                "physical_mappings",
                "logical_tables",
                "applied_operations",
                "meta_kv",
            ):
                connection.execute(f'DELETE FROM "{table}"')
            connection.execute(
                "INSERT INTO meta_kv(key, value) VALUES(?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )
            connection.execute(
                "INSERT INTO meta_kv(key, value) VALUES(?, ?)",
                ("logical_clock_ms", str(initial_clock_ms)),
            )
            connection.execute(
                "INSERT INTO meta_kv(key, value) VALUES(?, ?)",
                ("sample_data_dir", str(sample.data_dir)),
            )
            for table in sample.tables.values():
                connection.execute(
                    "INSERT INTO logical_tables(name, descriptor_json) VALUES(?, ?)",
                    (table.name, canonical_json(table.to_dict())),
                )
            for table_name, mappings in sample.mappings.items():
                for mapping in mappings:
                    connection.execute(
                        """
                        INSERT INTO physical_mappings(
                            logical_table, shard_no, database_ref,
                            database_absolute, physical_table, role
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            table_name,
                            mapping.shard_no,
                            mapping.database_ref,
                            str(mapping.database),
                            mapping.physical_table,
                            mapping.role,
                        ),
                    )

            eligible = [
                rule
                for rule in sample.merged_rules
                if rule.effective_clock_ms <= initial_clock_ms
                and not rule.rollback_target
            ]
            if not eligible:
                fail(
                    "E_RULE_NOT_FOUND",
                    "no rule version is effective at the initial logical clock",
                    initial_clock_ms=initial_clock_ms,
                )
            current = max(eligible, key=lambda item: item.sort_key())
            for rule in sample.merged_rules:
                if rule.rollback_target:
                    lifecycle = "rollback_blocked"
                elif rule.version == current.version:
                    lifecycle = "active"
                elif rule.effective_clock_ms > initial_clock_ms:
                    lifecycle = "queued"
                else:
                    lifecycle = "historical"
                connection.execute(
                    """
                    INSERT INTO rule_versions(
                        version, digest, effective_clock_ms, generation,
                        rollback_target, source, source_priority, source_file,
                        model_json, decision_json, lifecycle, apply_seq,
                        created_clock_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule.version,
                        rule.digest,
                        rule.effective_clock_ms,
                        rule.generation,
                        rule.rollback_target,
                        rule.source,
                        rule.source_priority,
                        str(
                            next(
                                (
                                    candidate.get("source_file", "")
                                    for candidate in rule.decision.get(
                                        "candidates", []
                                    )
                                    if candidate.get("source") == rule.source
                                ),
                                "",
                            )
                        ),
                        canonical_json(rule.to_dict()),
                        canonical_json(rule.decision),
                        lifecycle,
                        1 if lifecycle == "active" else 0,
                        initial_clock_ms,
                    ),
                )
                decision_id = digest(
                    {
                        "kind": "load",
                        "version": rule.version,
                        "digest": rule.digest,
                        "clock": initial_clock_ms,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO rule_decisions(
                        decision_id, version, digest, action, reason,
                        effective_clock_ms, generation, at_clock_ms,
                        details_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        rule.version,
                        rule.digest,
                        "loaded",
                        f"initial lifecycle {lifecycle}",
                        rule.effective_clock_ms,
                        rule.generation,
                        initial_clock_ms,
                        canonical_json(
                            {
                                "lifecycle": lifecycle,
                                "current": rule.version == current.version,
                            }
                        ),
                    ),
                )
            connection.execute(
                """
                INSERT INTO current_rule(
                    singleton, version, digest, pointer_seq, updated_clock_ms
                ) VALUES(1, ?, ?, 1, ?)
                """,
                (current.version, current.digest, initial_clock_ms),
            )
            connection.execute(
                """
                INSERT INTO audit_log(
                    query_id, event, stage, details_json, at_clock_ms
                ) VALUES(NULL, 'load_committed', 'load', ?, ?)
                """,
                (
                    canonical_json(
                        {
                            "current_rule": current.version,
                            "rule_count": len(sample.merged_rules),
                            "shard_databases": sum(
                                len(items) for items in sample.mappings.values()
                            ),
                        }
                    ),
                    initial_clock_ms,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _state_clock(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM meta_kv WHERE key='logical_clock_ms'"
        ).fetchone()
        if row is None:
            fail("E_STATE_CORRUPT", "logical clock is missing from state")
        return int(row["value"])

    def _advance_clock(self, connection: sqlite3.Connection, clock_ms: int) -> int:
        current = self._state_clock(connection)
        advanced = max(current, int(clock_ms))
        connection.execute(
            "UPDATE meta_kv SET value=? WHERE key='logical_clock_ms'",
            (str(advanced),),
        )
        return advanced

    def _rule_model(self, row: sqlite3.Row) -> dict[str, Any]:
        return _json_load(row["model_json"], default={})

    def _current_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT rv.*, cr.pointer_seq AS pointer_seq,
                   cr.updated_clock_ms AS current_updated_clock_ms
            FROM current_rule cr
            JOIN rule_versions rv ON rv.version = cr.version
            WHERE cr.singleton = 1
            """
        ).fetchone()
        if row is None:
            fail("E_STATE_NOT_LOADED", "current rule pointer is missing")
        return row

    def _promote_queued(
        self,
        connection: sqlite3.Connection,
        *,
        clock_ms: int,
    ) -> list[dict[str, Any]]:
        current = self._current_row(connection)
        current_key = (
            int(current["effective_clock_ms"]),
            int(current["generation"]),
            int(current["source_priority"]),
            str(current["version"]),
        )
        rows = connection.execute(
            """
            SELECT * FROM rule_versions
            WHERE lifecycle = 'queued'
              AND rollback_target IS NULL
              AND effective_clock_ms <= ?
            ORDER BY effective_clock_ms, generation, source_priority, version
            """,
            (clock_ms,),
        ).fetchall()
        promoted: list[dict[str, Any]] = []
        for row in rows:
            key = (
                int(row["effective_clock_ms"]),
                int(row["generation"]),
                int(row["source_priority"]),
                str(row["version"]),
            )
            if key <= current_key:
                continue
            pointer_seq = int(
                connection.execute(
                    "SELECT pointer_seq FROM current_rule WHERE singleton=1"
                ).fetchone()["pointer_seq"]
            )
            connection.execute(
                "UPDATE rule_versions SET lifecycle='historical' WHERE lifecycle='active'"
            )
            connection.execute(
                "UPDATE rule_versions SET lifecycle='active', apply_seq=? WHERE version=?",
                (pointer_seq + 1, row["version"]),
            )
            connection.execute(
                """
                UPDATE current_rule
                SET version=?, digest=?, pointer_seq=?, updated_clock_ms=?
                WHERE singleton=1
                """,
                (
                    row["version"],
                    row["digest"],
                    pointer_seq + 1,
                    clock_ms,
                ),
            )
            decision_id = digest(
                {
                    "kind": "promote",
                    "version": row["version"],
                    "digest": row["digest"],
                    "pointer_seq": pointer_seq + 1,
                }
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO rule_decisions(
                    decision_id, version, digest, action, reason,
                    effective_clock_ms, generation, at_clock_ms, details_json
                ) VALUES(?, ?, ?, 'promoted', ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    row["version"],
                    row["digest"],
                    "queued rule became effective at the observed logical clock",
                    row["effective_clock_ms"],
                    row["generation"],
                    clock_ms,
                    canonical_json({"previous_pointer_seq": pointer_seq}),
                ),
            )
            promoted.append(
                {
                    "version": row["version"],
                    "digest": row["digest"],
                    "effective_clock_ms": row["effective_clock_ms"],
                    "pointer_seq": pointer_seq + 1,
                }
            )
            current_key = key
        return promoted

    def begin_query(
        self,
        *,
        template_id: str,
        params: dict[str, Any],
        source_filter: str,
        request_json: dict[str, Any],
        requested_query_id: str | None,
        clock_ms: int,
    ) -> dict[str, Any]:
        request_key = digest(
            {
                "template_id": template_id,
                "params": params,
                "source_filter": source_filter,
            }
        )
        with self.transaction(immediate=True) as connection:
            clock = self._advance_clock(connection, clock_ms)
            promoted = self._promote_queued(connection, clock_ms=clock)
            current = self._current_row(connection)
            model = self._rule_model(current)
            if requested_query_id is None:
                query_id = "q-" + digest(
                    {
                        "request_key": request_key,
                        "rule_version": current["version"],
                        "rule_digest": current["digest"],
                    }
                )[:24]
            else:
                query_id = requested_query_id
                if not query_id or any(character.isspace() for character in query_id):
                    fail(
                        "E_VALIDATION",
                        "query id must be a non-empty token without whitespace",
                    )
            existing = connection.execute(
                "SELECT * FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            if existing is not None:
                if existing["request_key"] != request_key:
                    fail(
                        "E_QUERY_ID_CONFLICT",
                        "query id is already bound to a different request",
                        query_id=query_id,
                    )
                if existing["bound_rule_version"] != current["version"]:
                    return {
                        "created": False,
                        "query_id": query_id,
                        "query": dict(existing),
                        "rule": _json_load(existing["bound_rule_model_json"], default={}),
                        "clock_ms": clock,
                        "promoted_rules": promoted,
                        "idempotent": True,
                    }
                return {
                    "created": False,
                    "query_id": query_id,
                    "query": dict(existing),
                    "rule": _json_load(existing["bound_rule_model_json"], default={}),
                    "clock_ms": clock,
                    "promoted_rules": promoted,
                    "idempotent": True,
                }
            connection.execute(
                """
                INSERT INTO query_runs(
                    query_id, request_json, request_key, template_id, params_json,
                    source_filter, bound_rule_version, bound_rule_digest,
                    bound_rule_source, bound_rule_model_json, status,
                    created_clock_ms, updated_clock_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'routing', ?, ?)
                """,
                (
                    query_id,
                    canonical_json(request_json),
                    request_key,
                    template_id,
                    canonical_json(params),
                    source_filter,
                    current["version"],
                    current["digest"],
                    current["source"],
                    current["model_json"],
                    clock,
                    clock,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_log(
                    query_id, event, stage, details_json, at_clock_ms
                ) VALUES(?, 'query_bound', 'route', ?, ?)
                """,
                (
                    query_id,
                    canonical_json(
                        {
                            "rule_version": current["version"],
                            "rule_digest": current["digest"],
                            "template_id": template_id,
                            "promoted_rules": promoted,
                        }
                    ),
                    clock,
                ),
            )
            row = connection.execute(
                "SELECT * FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            return {
                "created": True,
                "query_id": query_id,
                "query": dict(row),
                "rule": model,
                "clock_ms": clock,
                "promoted_rules": promoted,
                "idempotent": False,
            }

    def get_query(self, query_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            return _row_dict(row)

    def get_query_with_shards(self, query_id: str) -> dict[str, Any] | None:
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["shards"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM shard_runs WHERE query_id=? ORDER BY shard_no",
                    (query_id,),
                ).fetchall()
            ]
            commit = connection.execute(
                "SELECT * FROM query_commits WHERE query_id=?", (query_id,)
            ).fetchone()
            result["commit"] = _row_dict(commit)
            return result

    def save_plan(
        self,
        *,
        query_id: str,
        plan: dict[str, Any],
        clock_ms: int,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            if row is None:
                fail("E_QUERY_NOT_FOUND", "query does not exist", query_id=query_id)
            if row["status"] == "committed":
                commit = connection.execute(
                    "SELECT * FROM query_commits WHERE query_id=?", (query_id,)
                ).fetchone()
                return {
                    "saved": False,
                    "already_committed": True,
                    "commit": _row_dict(commit),
                }
            if row["status"] == "failed" and row["plan_json"] is None:
                fail(
                    "E_QUERY_INCOMPLETE",
                    "query has already failed before a plan was persisted",
                    query_id=query_id,
                    error=_json_load(row["error_json"], default={}),
                )
            if plan.get("rule_version") != row["bound_rule_version"]:
                fail(
                    "E_VERSION_CONFLICT",
                    "rewritten plan rule version differs from the bound version",
                    query_id=query_id,
                    bound=row["bound_rule_version"],
                    plan=plan.get("rule_version"),
                )
            if plan.get("rule_digest") != row["bound_rule_digest"]:
                fail(
                    "E_VERSION_CONFLICT",
                    "rewritten plan digest differs from the bound rule digest",
                    query_id=query_id,
                )
            existing_plan = _json_load(row["plan_json"], default=None) if row["plan_json"] else None
            if existing_plan is not None:
                if existing_plan.get("plan_digest") != plan.get("plan_digest"):
                    fail(
                        "E_VERSION_CONFLICT",
                        "a different deterministic plan is already persisted for this query",
                        query_id=query_id,
                    )
                return {
                    "saved": False,
                    "already_committed": False,
                    "plan": existing_plan,
                }
            connection.execute(
                """
                UPDATE query_runs
                SET plan_json=?, plan_digest=?, status='ready',
                    updated_clock_ms=?
                WHERE query_id=?
                """,
                (
                    canonical_json(plan),
                    plan["plan_digest"],
                    clock_ms,
                    query_id,
                ),
            )
            for shard in plan["shards"]:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO shard_runs(
                        query_id, shard_no, status, attempt, updated_clock_ms
                    ) VALUES(?, ?, 'pending', 0, ?)
                    """,
                    (query_id, shard["shard_no"], clock_ms),
                )
            connection.execute(
                """
                INSERT INTO audit_log(
                    query_id, event, stage, details_json, at_clock_ms
                ) VALUES(?, 'plan_saved', 'rewrite', ?, ?)
                """,
                (
                    query_id,
                    canonical_json(
                        {
                            "plan_digest": plan["plan_digest"],
                            "target_shards": plan["target_shards"],
                            "all_route": plan["all_route"],
                        }
                    ),
                    clock_ms,
                ),
            )
            return {
                "saved": True,
                "already_committed": False,
                "plan": plan,
            }

    def _watermark(self, connection: sqlite3.Connection, query_id: str) -> int:
        rows = connection.execute(
            "SELECT shard_no, status FROM shard_runs WHERE query_id=? ORDER BY shard_no",
            (query_id,),
        ).fetchall()
        terminal = {"succeeded", "failed", "abandoned"}
        watermark = 0
        for row in rows:
            if row["status"] not in terminal:
                break
            watermark += 1
        return watermark

    def record_shard_result(
        self,
        *,
        query_id: str,
        shard_no: int,
        status: str,
        rows: list[dict[str, Any]] | None,
        row_count: int | None,
        sum_value: int | None,
        result_digest: str | None,
        error_code: str | None,
        error_json: dict[str, Any] | None,
        clock_ms: int,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "abandoned"}:
            fail("E_INTERNAL", "invalid shard terminal status", status=status)
        with self.transaction(immediate=True) as connection:
            query_state = connection.execute(
                "SELECT status FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            if query_state is None:
                fail("E_QUERY_NOT_FOUND", "query does not exist", query_id=query_id)
            if query_state["status"] == "committed":
                existing_commit = connection.execute(
                    "SELECT * FROM shard_runs WHERE query_id=? AND shard_no=?",
                    (query_id, shard_no),
                ).fetchone()
                return {
                    "stored": False,
                    "idempotent": True,
                    "shard": _row_dict(existing_commit),
                }
            existing = connection.execute(
                "SELECT * FROM shard_runs WHERE query_id=? AND shard_no=?",
                (query_id, shard_no),
            ).fetchone()
            if existing is None:
                fail(
                    "E_QUERY_NOT_FOUND",
                    "shard task is not part of the query plan",
                    query_id=query_id,
                    shard_no=shard_no,
                )
            if existing["status"] in {"succeeded", "failed", "abandoned"}:
                return {
                    "stored": False,
                    "idempotent": True,
                    "shard": dict(existing),
                }
            attempt = int(existing["attempt"]) + 1
            connection.execute(
                """
                UPDATE shard_runs
                SET status=?, attempt=?, rows_json=?, row_count=?, sum_value=?,
                    result_digest=?, error_code=?, error_json=?, updated_clock_ms=?
                WHERE query_id=? AND shard_no=?
                """,
                (
                    status,
                    attempt,
                    canonical_json(rows) if rows is not None else None,
                    row_count,
                    sum_value,
                    result_digest,
                    error_code,
                    canonical_json(error_json) if error_json is not None else None,
                    clock_ms,
                    query_id,
                    shard_no,
                ),
            )
            watermark = self._watermark(connection, query_id)
            connection.execute(
                """
                UPDATE query_runs
                SET status='running', resume_watermark=?, updated_clock_ms=?
                WHERE query_id=? AND status NOT IN ('committed', 'failed')
                """,
                (watermark, clock_ms, query_id),
            )
            connection.execute(
                """
                INSERT INTO audit_log(
                    query_id, event, stage, details_json, at_clock_ms
                ) VALUES(?, 'shard_terminal', 'execute', ?, ?)
                """,
                (
                    query_id,
                    canonical_json(
                        {
                            "shard_no": shard_no,
                            "status": status,
                            "attempt": attempt,
                            "error_code": error_code,
                        }
                    ),
                    clock_ms,
                ),
            )
            row = connection.execute(
                "SELECT * FROM shard_runs WHERE query_id=? AND shard_no=?",
                (query_id, shard_no),
            ).fetchone()
            return {"stored": True, "idempotent": False, "shard": dict(row)}

    def mark_query_error(
        self,
        *,
        query_id: str,
        stage: str,
        error: BusinessError,
        clock_ms: int,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            if row is None:
                fail("E_QUERY_NOT_FOUND", "query does not exist", query_id=query_id)
            if row["status"] == "committed":
                return
            connection.execute(
                """
                UPDATE query_runs
                SET status='failed', outcome='failed', error_json=?,
                    failures_json=?, updated_clock_ms=?
                WHERE query_id=?
                """,
                (
                    canonical_json(error.to_dict()),
                    canonical_json([error.to_dict()]),
                    clock_ms,
                    query_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_log(
                    query_id, event, stage, details_json, at_clock_ms
                ) VALUES(?, 'query_failed', ?, ?, ?)
                """,
                (
                    query_id,
                    stage,
                    canonical_json(error.to_dict()),
                    clock_ms,
                ),
            )

    def commit_query(
        self,
        *,
        query_id: str,
        outcome: str,
        aggregate: dict[str, Any],
        result: dict[str, Any],
        clock_ms: int,
    ) -> dict[str, Any]:
        result_digest = digest(result)
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM query_commits WHERE query_id=?", (query_id,)
            ).fetchone()
            if existing is not None:
                if existing["result_digest"] != result_digest:
                    fail(
                        "E_QUERY_ID_CONFLICT",
                        "query already committed a different result",
                        query_id=query_id,
                    )
                return {
                    "committed": False,
                    "idempotent": True,
                    "commit": dict(existing),
                    "result": _json_load(existing["result_json"], default={}),
                }
            query = connection.execute(
                "SELECT * FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            if query is None:
                fail("E_QUERY_NOT_FOUND", "query does not exist", query_id=query_id)
            shard_rows = connection.execute(
                "SELECT * FROM shard_runs WHERE query_id=? ORDER BY shard_no",
                (query_id,),
            ).fetchall()
            if not shard_rows:
                fail("E_QUERY_INCOMPLETE", "query has no shard tasks", query_id=query_id)
            unfinished = [
                int(row["shard_no"])
                for row in shard_rows
                if row["status"] not in {"succeeded", "failed", "abandoned"}
            ]
            if unfinished:
                fail(
                    "E_QUERY_INCOMPLETE",
                    "cannot commit a query with unfinished shards",
                    query_id=query_id,
                    unfinished_shards=unfinished,
                )
            commit_seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(commit_seq), 0) + 1 AS value FROM query_commits"
                ).fetchone()["value"]
            )
            connection.execute(
                """
                INSERT INTO query_commits(
                    query_id, rule_version, result_digest, result_json, commit_seq
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    query_id,
                    query["bound_rule_version"],
                    result_digest,
                    canonical_json(result),
                    commit_seq,
                ),
            )
            watermark = self._watermark(connection, query_id)
            connection.execute(
                """
                UPDATE query_runs
                SET status='committed', outcome=?, aggregate_json=?,
                    result_json=?, failures_json=?, error_json=NULL,
                    resume_watermark=?, commit_seq=?, updated_clock_ms=?
                WHERE query_id=?
                """,
                (
                    outcome,
                    canonical_json(aggregate),
                    canonical_json(result),
                    canonical_json(result.get("failures", [])),
                    watermark,
                    commit_seq,
                    clock_ms,
                    query_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_log(
                    query_id, event, stage, details_json, at_clock_ms
                ) VALUES(?, 'query_committed', 'merge', ?, ?)
                """,
                (
                    query_id,
                    canonical_json(
                        {
                            "outcome": outcome,
                            "result_digest": result_digest,
                            "commit_seq": commit_seq,
                            "resume_watermark": watermark,
                        }
                    ),
                    clock_ms,
                ),
            )
            return {
                "committed": True,
                "idempotent": False,
                "commit_seq": commit_seq,
                "result_digest": result_digest,
                "result": result,
            }

    def mark_failed_terminal(
        self,
        *,
        query_id: str,
        outcome: str,
        aggregate: dict[str, Any],
        result: dict[str, Any],
        error: BusinessError,
        clock_ms: int,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM query_runs WHERE query_id=?", (query_id,)
            ).fetchone()
            if row is None:
                fail("E_QUERY_NOT_FOUND", "query does not exist", query_id=query_id)
            if row["status"] == "committed":
                return
            watermark = self._watermark(connection, query_id)
            connection.execute(
                """
                UPDATE query_runs
                SET status='failed', outcome=?, aggregate_json=?, result_json=?,
                    failures_json=?, error_json=?, resume_watermark=?,
                    updated_clock_ms=?
                WHERE query_id=?
                """,
                (
                    outcome,
                    canonical_json(aggregate),
                    canonical_json(result),
                    canonical_json(result.get("failures", [])),
                    canonical_json(error.to_dict()),
                    watermark,
                    clock_ms,
                    query_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_log(
                    query_id, event, stage, details_json, at_clock_ms
                ) VALUES(?, 'query_failed', 'merge', ?, ?)
                """,
                (query_id, canonical_json(error.to_dict()), clock_ms),
            )

    def apply_rule(
        self,
        *,
        rule: MergedRule,
        now_clock_ms: int,
    ) -> dict[str, Any]:
        with self.lock():
            with self.transaction(immediate=True) as connection:
                clock = self._advance_clock(connection, now_clock_ms)
                promoted: list[dict[str, Any]] = []
                current = self._current_row(connection)
                existing = connection.execute(
                    "SELECT * FROM rule_versions WHERE version=?", (rule.version,)
                ).fetchone()
                if existing is not None and existing["digest"] != rule.digest:
                    return self._record_decision_error(
                        connection,
                        rule=rule,
                        action="rejected",
                        reason="same rule version already exists with a different digest",
                        clock_ms=clock,
                        code="E_VERSION_CONFLICT",
                        message="rule version conflict",
                        extra={"existing_digest": existing["digest"]},
                    )
                current_key = (
                    int(current["effective_clock_ms"]),
                    int(current["generation"]),
                    int(current["source_priority"]),
                    str(current["version"]),
                )
                rule_key = rule.sort_key()
                if current["version"] == rule.version:
                    return {
                        "ok": True,
                        "action": "idempotent",
                        "reason": "rule version is already current",
                        "version": rule.version,
                        "digest": rule.digest,
                        "current_rule": current["version"],
                        "clock_ms": clock,
                        "promoted_rules": promoted,
                    }
                if rule.rollback_target:
                    return self._record_decision_error(
                        connection,
                        rule=rule,
                        action="rejected",
                        reason="rule snapshot declares a rollback target",
                        clock_ms=clock,
                        code="E_ROLLBACK_REJECTED",
                        message="rollback rule versions are rejected",
                        extra={"rollback_target": rule.rollback_target},
                    )
                if (
                    rule_key[0] >= current_key[0]
                    and rule.generation <= int(current["generation"])
                    and rule.version != current["version"]
                ):
                    return self._record_decision_error(
                        connection,
                        rule=rule,
                        action="rejected",
                        reason="candidate shard generation is not monotonic",
                        clock_ms=clock,
                        code="E_ROLLBACK_REJECTED",
                        message="rule shard generation regression is rejected",
                        extra={
                            "current_rule": current["version"],
                            "current_generation": current["generation"],
                            "candidate_generation": rule.generation,
                        },
                    )
                if rule_key < current_key:
                    return self._record_decision_error(
                        connection,
                        rule=rule,
                        action="rejected",
                        reason="candidate sorts before the current rule version",
                        clock_ms=clock,
                        code="E_ROLLBACK_REJECTED",
                        message="rule version rollback is rejected",
                        extra={
                            "current_rule": current["version"],
                            "current_effective_clock_ms": current["effective_clock_ms"],
                            "candidate_effective_clock_ms": rule.effective_clock_ms,
                        },
                    )
                if rule.effective_clock_ms < clock:
                    return self._record_decision_error(
                        connection,
                        rule=rule,
                        action="rejected",
                        reason="effective clock is earlier than the observed logical clock",
                        clock_ms=clock,
                        code="E_CLOCK_REGRESSION",
                        message="rule effective clock is earlier than the current clock",
                        extra={"effective_clock_ms": rule.effective_clock_ms},
                    )
                if rule.effective_clock_ms > clock:
                    lifecycle = "queued"
                    action = "queued"
                    reason = "effective clock is in the future; rule was queued"
                    current_version = current["version"]
                else:
                    lifecycle = "active"
                    action = "applied"
                    reason = "rule snapshot atomically replaced the current pointer"
                    current_version = rule.version
                    pointer_seq = int(current["pointer_seq"]) + 1
                    connection.execute(
                        "UPDATE rule_versions SET lifecycle='historical' WHERE lifecycle='active'"
                    )
                    connection.execute(
                        """
                        INSERT INTO rule_versions(
                            version, digest, effective_clock_ms, generation,
                            rollback_target, source, source_priority, source_file,
                            model_json, decision_json, lifecycle, apply_seq,
                            created_clock_ms
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(version) DO UPDATE SET
                            digest=excluded.digest,
                            effective_clock_ms=excluded.effective_clock_ms,
                            generation=excluded.generation,
                            source=excluded.source,
                            source_priority=excluded.source_priority,
                            source_file=excluded.source_file,
                            model_json=excluded.model_json,
                            decision_json=excluded.decision_json,
                            lifecycle=excluded.lifecycle,
                            apply_seq=excluded.apply_seq,
                            created_clock_ms=excluded.created_clock_ms
                        """,
                        (
                            rule.version,
                            rule.digest,
                            rule.effective_clock_ms,
                            rule.generation,
                            rule.rollback_target,
                            rule.source,
                            rule.source_priority,
                            str(
                                next(
                                    (
                                        candidate.get("source_file", "")
                                        for candidate in rule.decision.get(
                                            "candidates", []
                                        )
                                        if candidate.get("source") == rule.source
                                    ),
                                    "",
                                )
                            ),
                            canonical_json(rule.to_dict()),
                            canonical_json(rule.decision),
                            lifecycle,
                            pointer_seq,
                            clock,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE current_rule
                        SET version=?, digest=?, pointer_seq=?, updated_clock_ms=?
                        WHERE singleton=1
                        """,
                        (rule.version, rule.digest, pointer_seq, clock),
                    )
                if lifecycle == "queued":
                    connection.execute(
                        """
                        INSERT INTO rule_versions(
                            version, digest, effective_clock_ms, generation,
                            rollback_target, source, source_priority, source_file,
                            model_json, decision_json, lifecycle, apply_seq,
                            created_clock_ms
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?)
                        ON CONFLICT(version) DO UPDATE SET
                            digest=excluded.digest,
                            model_json=excluded.model_json,
                            decision_json=excluded.decision_json,
                            lifecycle='queued'
                        """,
                        (
                            rule.version,
                            rule.digest,
                            rule.effective_clock_ms,
                            rule.generation,
                            rule.rollback_target,
                            rule.source,
                            rule.source_priority,
                            "",
                            canonical_json(rule.to_dict()),
                            canonical_json(rule.decision),
                            clock,
                        ),
                    )
                decision_id = digest(
                    {
                        "kind": "apply",
                        "version": rule.version,
                        "digest": rule.digest,
                        "clock": clock,
                        "action": action,
                    }
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO rule_decisions(
                        decision_id, version, digest, action, reason,
                        effective_clock_ms, generation, at_clock_ms, details_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        rule.version,
                        rule.digest,
                        action,
                        reason,
                        rule.effective_clock_ms,
                        rule.generation,
                        clock,
                        canonical_json(
                            {
                                "current_rule": current_version,
                                "promoted_rules": promoted,
                                "candidate_source": rule.source,
                                "candidate_generation": rule.generation,
                            }
                        ),
                    ),
                )
                return {
                    "ok": True,
                    "action": action,
                    "reason": reason,
                    "version": rule.version,
                    "digest": rule.digest,
                    "current_rule": current_version,
                    "clock_ms": clock,
                    "promoted_rules": promoted,
                }

    def _record_decision_error(
        self,
        connection: sqlite3.Connection,
        *,
        rule: MergedRule,
        action: str,
        reason: str,
        clock_ms: int,
        code: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = connection.execute(
            "SELECT lifecycle FROM rule_versions WHERE version=?", (rule.version,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO rule_versions(
                    version, digest, effective_clock_ms, generation,
                    rollback_target, source, source_priority, source_file,
                    model_json, decision_json, lifecycle, apply_seq, created_clock_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, '', ?, ?, 'rejected', 0, ?)
                """,
                (
                    rule.version,
                    rule.digest,
                    rule.effective_clock_ms,
                    rule.generation,
                    rule.rollback_target,
                    rule.source,
                    rule.source_priority,
                    canonical_json(rule.to_dict()),
                    canonical_json(rule.decision),
                    clock_ms,
                ),
            )
        decision_id = digest(
            {
                "kind": "reject",
                "version": rule.version,
                "digest": rule.digest,
                "clock": clock_ms,
                "code": code,
                "reason": reason,
            }
        )
        details = {
            "code": code,
            "message": message,
            "reason": reason,
        }
        if extra:
            details.update(extra)
        connection.execute(
            """
            INSERT OR IGNORE INTO rule_decisions(
                decision_id, version, digest, action, reason,
                effective_clock_ms, generation, at_clock_ms, details_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                rule.version,
                rule.digest,
                action,
                reason,
                rule.effective_clock_ms,
                rule.generation,
                clock_ms,
                canonical_json(details),
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_log(
                query_id, event, stage, details_json, at_clock_ms
            ) VALUES(NULL, 'rule_rejected', 'apply', ?, ?)
            """,
            (canonical_json(details), clock_ms),
        )
        return {
            "ok": False,
            "action": action,
            "reason": reason,
            "code": code,
            "message": message,
            "details": details,
            "version": rule.version,
            "digest": rule.digest,
            "clock_ms": clock_ms,
        }

    def uncommitted_queries(self) -> list[dict[str, Any]]:
        with self.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM query_runs
                WHERE status IN ('routing', 'ready', 'running')
                ORDER BY query_id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def status(self, *, query_id: str | None = None) -> dict[str, Any]:
        with self.read() as connection:
            clock = self._state_clock(connection)
            current = connection.execute(
                """
                SELECT cr.pointer_seq, cr.updated_clock_ms, rv.*
                FROM current_rule cr
                JOIN rule_versions rv ON rv.version = cr.version
                WHERE cr.singleton = 1
                """
            ).fetchone()
            if current is None:
                fail("E_STATE_NOT_LOADED", "current rule pointer is missing")
            rules = [
                {
                    "version": row["version"],
                    "digest": row["digest"],
                    "source": row["source"],
                    "source_priority": row["source_priority"],
                    "effective_clock_ms": row["effective_clock_ms"],
                    "generation": row["generation"],
                    "rollback_target": row["rollback_target"],
                    "lifecycle": row["lifecycle"],
                    "apply_seq": row["apply_seq"],
                    "decision": _json_load(row["decision_json"], default={}),
                }
                for row in connection.execute(
                    """
                    SELECT * FROM rule_versions
                    ORDER BY effective_clock_ms, generation, source_priority, version
                    """
                ).fetchall()
            ]
            decisions = [
                {
                    "version": row["version"],
                    "digest": row["digest"],
                    "action": row["action"],
                    "reason": row["reason"],
                    "effective_clock_ms": row["effective_clock_ms"],
                    "generation": row["generation"],
                    "at_clock_ms": row["at_clock_ms"],
                    "details": _json_load(row["details_json"], default={}),
                }
                for row in connection.execute(
                    "SELECT * FROM rule_decisions ORDER BY seq"
                ).fetchall()
            ]
            if query_id is None:
                query_rows = connection.execute(
                    "SELECT * FROM query_runs ORDER BY query_id"
                ).fetchall()
            else:
                query_rows = connection.execute(
                    "SELECT * FROM query_runs WHERE query_id=?", (query_id,)
                ).fetchall()
                if not query_rows:
                    fail("E_QUERY_NOT_FOUND", "query does not exist", query_id=query_id)
            queries: list[dict[str, Any]] = []
            for query in query_rows:
                booleans = {
                    "status": query["status"],
                    "outcome": query["outcome"],
                    "template_id": query["template_id"],
                    "params": _json_load(query["params_json"], default={}),
                    "rule_version": query["bound_rule_version"],
                    "rule_digest": query["bound_rule_digest"],
                    "rule_source": query["bound_rule_source"],
                    "created_clock_ms": query["created_clock_ms"],
                    "updated_clock_ms": query["updated_clock_ms"],
                    "resume_watermark": query["resume_watermark"],
                    "commit_seq": query["commit_seq"],
                    "aggregate": _json_load(query["aggregate_json"], default=None),
                    "failures": _json_load(query["failures_json"], default=[]),
                    "error": _json_load(query["error_json"], default=None),
                }
                shard_rows = connection.execute(
                    "SELECT * FROM shard_runs WHERE query_id=? ORDER BY shard_no",
                    (query["query_id"],),
                ).fetchall()
                booleans["shards"] = [
                    {
                        "shard_no": shard["shard_no"],
                        "status": shard["status"],
                        "attempt": shard["attempt"],
                        "row_count": shard["row_count"],
                        "sum_value": shard["sum_value"],
                        "result_digest": shard["result_digest"],
                        "error_code": shard["error_code"],
                        "updated_clock_ms": shard["updated_clock_ms"],
                    }
                    for shard in shard_rows
                ]
                commit = connection.execute(
                    "SELECT * FROM query_commits WHERE query_id=?",
                    (query["query_id"],),
                ).fetchone()
                booleans["commit"] = (
                    {
                        "result_digest": commit["result_digest"],
                        "commit_seq": commit["commit_seq"],
                    }
                    if commit is not None
                    else None
                )
                booleans["query_id"] = query["query_id"]
                queries.append(booleans)
            counts = {
                "queries": len(queries),
                "committed": sum(1 for query in queries if query["status"] == "committed"),
                "uncommitted": sum(
                    1
                    for query in queries
                    if query["status"] in {"routing", "ready", "running"}
                ),
                "failed": sum(1 for query in queries if query["status"] == "failed"),
                "rules": len(rules),
            }
            return {
                "ok": True,
                "component": "shard_router",
                "schema_version": SCHEMA_VERSION,
                "logical_clock_ms": clock,
                "current_rule": {
                    "version": current["version"],
                    "digest": current["digest"],
                    "effective_clock_ms": current["effective_clock_ms"],
                    "generation": current["generation"],
                    "source": current["source"],
                    "source_priority": current["source_priority"],
                    "pointer_seq": current["pointer_seq"],
                    "updated_clock_ms": current["updated_clock_ms"],
                },
                "rules": rules,
                "rule_decisions": decisions,
                "queries": queries,
                "recovery_watermark": {
                    "logical_clock_ms": clock,
                    "query_prefixes": {
                        query["query_id"]: query["resume_watermark"]
                        for query in sorted(queries, key=lambda item: item["query_id"])
                    },
                },
                "counts": counts,
                "generated_from": "persistent_state",
            }
