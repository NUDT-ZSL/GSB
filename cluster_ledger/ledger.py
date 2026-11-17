"""Durable SQLite ledger with version epochs and integrity checksums."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .clock import LATENCY_CALIBER_VERSION
from .config import Declaration
from .deterministic import canonical_json, digest_text
from .errors import E_LEDGER, E_VERSION, BusinessError


LEDGER_SCHEMA_VERSION = 1


def _bool_int(value: bool) -> int:
    return 1 if value else 0


def result_checksum(result: Mapping[str, Any]) -> str:
    components = result.get("components", {})
    if isinstance(components, str):
        components = json.loads(components)
    payload = {
        "task_id": result["task_id"],
        "cluster_id": result["cluster_id"],
        "config_version": int(result["config_version"]),
        "aggregation_mode": result["aggregation_mode"],
        "summary": result["summary"],
        "components": components,
        "entry_count": int(result.get("entry_count", 0)),
        "calculation_duration_ms": int(result["calculation_duration_ms"]),
        "dispatch_started_mono_ms": int(result["dispatch_started_mono_ms"]),
        "dispatch_finished_mono_ms": int(result["dispatch_finished_mono_ms"]),
        "dispatch_started_mono_ns": int(result["dispatch_started_mono_ns"]),
        "dispatch_finished_mono_ns": int(result["dispatch_finished_mono_ns"]),
        "declaration_version": result["declaration_version"],
    }
    return "sha256:" + digest_text(canonical_json(payload))


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                cluster_id TEXT NOT NULL,
                config_version INTEGER NOT NULL,
                aggregation_mode TEXT NOT NULL,
                summary TEXT NOT NULL,
                components_json TEXT NOT NULL,
                entry_count INTEGER NOT NULL,
                calculation_duration_ms INTEGER NOT NULL,
                dispatch_started_mono_ms INTEGER NOT NULL,
                dispatch_finished_mono_ms INTEGER NOT NULL,
                dispatch_started_mono_ns INTEGER NOT NULL,
                dispatch_finished_mono_ns INTEGER NOT NULL,
                declaration_version TEXT NOT NULL,
                is_stale INTEGER NOT NULL DEFAULT 0,
                ledger_checksum TEXT NOT NULL,
                created_revision INTEGER NOT NULL,
                UNIQUE(task_id, cluster_id, config_version)
            );
            CREATE INDEX IF NOT EXISTS idx_results_current
                ON results(is_stale, cluster_id, config_version, task_id);
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                task_id TEXT,
                cluster_id TEXT,
                config_version INTEGER,
                started_mono_ms INTEGER NOT NULL,
                ended_mono_ms INTEGER NOT NULL,
                started_mono_ns INTEGER NOT NULL,
                ended_mono_ns INTEGER NOT NULL,
                data_version TEXT NOT NULL,
                status TEXT NOT NULL,
                business_code TEXT,
                exceeded_budget INTEGER NOT NULL DEFAULT 0,
                is_stale INTEGER NOT NULL DEFAULT 0,
                detail_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_interactions_kind
                ON interactions(kind, started_mono_ms);
            CREATE TABLE IF NOT EXISTS declaration_state (
                cluster_id TEXT PRIMARY KEY,
                config_version INTEGER NOT NULL,
                declaration_version TEXT NOT NULL,
                updated_mono_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS version_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster_id TEXT NOT NULL,
                old_config_version INTEGER NOT NULL,
                new_config_version INTEGER NOT NULL,
                declaration_version TEXT NOT NULL,
                detected_mono_ms INTEGER NOT NULL,
                revision INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_transitions_revision
                ON version_transitions(revision, cluster_id);
            CREATE TABLE IF NOT EXISTS recovery_state (
                task_id TEXT NOT NULL,
                cluster_id TEXT NOT NULL,
                config_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                updated_mono_ms INTEGER NOT NULL,
                PRIMARY KEY(task_id, cluster_id, config_version)
            );
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('revision', '0')"
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(LEDGER_SCHEMA_VERSION),),
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('latency_caliber_version', ?)",
            (LATENCY_CALIBER_VERSION,),
        )
        interaction_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(interactions)")
        }
        if "config_version" not in interaction_columns:
            self._connection.execute("ALTER TABLE interactions ADD COLUMN config_version INTEGER")
        if "is_stale" not in interaction_columns:
            self._connection.execute(
                "ALTER TABLE interactions ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield self._connection
            finally:
                self._connection.commit()

    def _meta(self, connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def _set_meta(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _bump_revision(self, connection: sqlite3.Connection) -> int:
        current = int(self._meta(connection, "revision") or "0")
        revision = current + 1
        self._set_meta(connection, "revision", str(revision))
        return revision

    def revision(self) -> int:
        with self._lock:
            return int(self._meta(self._connection, "revision") or "0")

    def latency_caliber_version(self) -> str:
        with self._lock:
            return self._meta(self._connection, "latency_caliber_version") or LATENCY_CALIBER_VERSION

    def reconcile(self, declaration: Declaration) -> dict[str, Any]:
        """Apply a declaration epoch atomically.

        A lower cluster version is a hard E_VERSION rejection. A higher version
        marks every result from other versions stale before new results arrive,
        so the current view cannot mix epochs.
        """

        transitions: list[dict[str, Any]] = []
        with self._transaction() as connection:
            previous = {
                row["cluster_id"]: row
                for row in connection.execute("SELECT * FROM declaration_state").fetchall()
            }
            for cluster_index, cluster in enumerate(declaration.clusters):
                old = previous.get(cluster.cluster_id)
                if old is not None and cluster.config_version < int(old["config_version"]):
                    raise BusinessError(
                        E_VERSION,
                        "config version rollback is not allowed",
                        field_path=f"$.clusters[{cluster_index}].config_version",
                        details={
                            "cluster_id": cluster.cluster_id,
                            "ledger_version": int(old["config_version"]),
                            "declared_version": cluster.config_version,
                        },
                    )

            old_fingerprint = self._meta(connection, "declaration_fingerprint")
            new_fingerprint = declaration.fingerprint
            revision = int(self._meta(connection, "revision") or "0")
            needs_revision = old_fingerprint != new_fingerprint
            if needs_revision:
                revision += 1
                self._set_meta(connection, "revision", str(revision))
                self._set_meta(connection, "declaration_fingerprint", new_fingerprint)

            for cluster in declaration.clusters:
                old = previous.get(cluster.cluster_id)
                if old is None:
                    connection.execute(
                        "INSERT INTO declaration_state(cluster_id, config_version, declaration_version, updated_mono_ms) "
                        "VALUES(?, ?, ?, CAST(strftime('%s','now') AS INTEGER) * 1000)",
                        (cluster.cluster_id, cluster.config_version, declaration.declaration_version),
                    )
                    continue
                old_version = int(old["config_version"])
                if old_version == cluster.config_version:
                    connection.execute(
                        "UPDATE declaration_state SET declaration_version=?, updated_mono_ms="
                        "CAST(strftime('%s','now') AS INTEGER) * 1000 WHERE cluster_id=?",
                        (declaration.declaration_version, cluster.cluster_id),
                    )
                    continue
                # old_version < new_version was checked above.
                connection.execute(
                    "UPDATE results SET is_stale=1 WHERE cluster_id=? AND config_version<>?",
                    (cluster.cluster_id, cluster.config_version),
                )
                connection.execute(
                    "UPDATE interactions SET is_stale=1 WHERE cluster_id=? AND config_version IS NOT NULL "
                    "AND config_version<>?",
                    (cluster.cluster_id, cluster.config_version),
                )
                connection.execute(
                    "INSERT INTO version_transitions(cluster_id, old_config_version, new_config_version, "
                    "declaration_version, detected_mono_ms, revision) VALUES(?, ?, ?, ?, "
                    "CAST(strftime('%s','now') AS INTEGER) * 1000, ?)",
                    (
                        cluster.cluster_id,
                        old_version,
                        cluster.config_version,
                        declaration.declaration_version,
                        revision,
                    ),
                )
                connection.execute(
                    "UPDATE declaration_state SET config_version=?, declaration_version=?, updated_mono_ms="
                    "CAST(strftime('%s','now') AS INTEGER) * 1000 WHERE cluster_id=?",
                    (
                        cluster.config_version,
                        declaration.declaration_version,
                        cluster.cluster_id,
                    ),
                )
                transitions.append(
                    {
                        "cluster_id": cluster.cluster_id,
                        "old_config_version": old_version,
                        "new_config_version": cluster.config_version,
                    }
                )

            declared_ids = {cluster.cluster_id for cluster in declaration.clusters}
            for cluster_id in sorted(set(previous) - declared_ids):
                connection.execute(
                    "UPDATE results SET is_stale=1 WHERE cluster_id=?", (cluster_id,)
                )
                connection.execute(
                    "UPDATE interactions SET is_stale=1 WHERE cluster_id=? AND config_version IS NOT NULL",
                    (cluster_id,),
                )
                connection.execute(
                    "DELETE FROM declaration_state WHERE cluster_id=?", (cluster_id,)
                )

        return {
            "declaration_version": declaration.declaration_version,
            "fingerprint": declaration.fingerprint,
            "revision": revision,
            "transitions": sorted(transitions, key=lambda item: item["cluster_id"]),
        }

    def record_result(
        self,
        *,
        task_id: str,
        cluster_id: str,
        config_version: int,
        aggregation_mode: str,
        summary: str,
        components: Mapping[str, str],
        entry_count: int,
        calculation_duration_ms: int,
        dispatch_started_mono_ms: int,
        dispatch_finished_mono_ms: int,
        dispatch_started_mono_ns: int,
        dispatch_finished_mono_ns: int,
        declaration_version: str,
    ) -> dict[str, Any]:
        components_json = canonical_json(dict(components))
        fields = {
            "task_id": task_id,
            "cluster_id": cluster_id,
            "config_version": int(config_version),
            "aggregation_mode": aggregation_mode,
            "summary": summary,
            "components": dict(components),
            "entry_count": int(entry_count),
            "calculation_duration_ms": int(calculation_duration_ms),
            "dispatch_started_mono_ms": int(dispatch_started_mono_ms),
            "dispatch_finished_mono_ms": int(dispatch_finished_mono_ms),
            "dispatch_started_mono_ns": int(dispatch_started_mono_ns),
            "dispatch_finished_mono_ns": int(dispatch_finished_mono_ns),
            "declaration_version": declaration_version,
        }
        checksum = result_checksum(fields)
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT config_version FROM declaration_state WHERE cluster_id=?", (cluster_id,)
            ).fetchone()
            stale = 0
            if state is not None and int(state["config_version"]) != int(config_version):
                stale = 1
            revision = self._bump_revision(connection)
            connection.execute(
                "UPDATE results SET is_stale=1 WHERE task_id=? AND cluster_id=? AND config_version<>?",
                (task_id, cluster_id, config_version),
            )
            connection.execute(
                """
                INSERT INTO results(
                    task_id, cluster_id, config_version, aggregation_mode, summary,
                    components_json, entry_count, calculation_duration_ms,
                    dispatch_started_mono_ms, dispatch_finished_mono_ms,
                    dispatch_started_mono_ns, dispatch_finished_mono_ns,
                    declaration_version, is_stale, ledger_checksum, created_revision
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, cluster_id, config_version) DO UPDATE SET
                    aggregation_mode=excluded.aggregation_mode,
                    summary=excluded.summary,
                    components_json=excluded.components_json,
                    entry_count=excluded.entry_count,
                    calculation_duration_ms=excluded.calculation_duration_ms,
                    dispatch_started_mono_ms=excluded.dispatch_started_mono_ms,
                    dispatch_finished_mono_ms=excluded.dispatch_finished_mono_ms,
                    dispatch_started_mono_ns=excluded.dispatch_started_mono_ns,
                    dispatch_finished_mono_ns=excluded.dispatch_finished_mono_ns,
                    declaration_version=excluded.declaration_version,
                    is_stale=excluded.is_stale,
                    ledger_checksum=excluded.ledger_checksum,
                    created_revision=excluded.created_revision
                """,
                (
                    task_id,
                    cluster_id,
                    int(config_version),
                    aggregation_mode,
                    summary,
                    components_json,
                    int(entry_count),
                    int(calculation_duration_ms),
                    int(dispatch_started_mono_ms),
                    int(dispatch_finished_mono_ms),
                    int(dispatch_started_mono_ns),
                    int(dispatch_finished_mono_ns),
                    declaration_version,
                    stale,
                    checksum,
                    revision,
                ),
            )
            connection.execute(
                "DELETE FROM recovery_state WHERE task_id=? AND cluster_id=? AND config_version=?",
                (task_id, cluster_id, config_version),
            )
        return {**fields, "is_stale": bool(stale), "ledger_checksum": checksum, "created_revision": revision}

    def record_interaction(
        self,
        *,
        kind: str,
        task_id: str | None,
        cluster_id: str | None,
        config_version: int | None = None,
        started_mono_ms: int,
        ended_mono_ms: int,
        started_mono_ns: int,
        ended_mono_ns: int,
        data_version: str,
        status: str,
        business_code: str | None = None,
        exceeded_budget: bool = False,
        detail: Mapping[str, Any] | None = None,
    ) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO interactions(kind, task_id, cluster_id, config_version, started_mono_ms, ended_mono_ms, "
                "started_mono_ns, ended_mono_ns, data_version, status, business_code, "
                "exceeded_budget, is_stale, detail_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    task_id,
                    cluster_id,
                    config_version,
                    int(started_mono_ms),
                    int(ended_mono_ms),
                    int(started_mono_ns),
                    int(ended_mono_ns),
                    data_version,
                    status,
                    business_code,
                    _bool_int(exceeded_budget),
                    0,
                    canonical_json(dict(detail or {})),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> dict[str, Any]:
        components = json.loads(row["components_json"])
        return {
            "id": int(row["id"]),
            "task_id": row["task_id"],
            "cluster_id": row["cluster_id"],
            "config_version": int(row["config_version"]),
            "aggregation_mode": row["aggregation_mode"],
            "summary": row["summary"],
            "components": components,
            "components_json": row["components_json"],
            "entry_count": int(row["entry_count"]),
            "calculation_duration_ms": int(row["calculation_duration_ms"]),
            "dispatch_started_mono_ms": int(row["dispatch_started_mono_ms"]),
            "dispatch_finished_mono_ms": int(row["dispatch_finished_mono_ms"]),
            "dispatch_started_mono_ns": int(row["dispatch_started_mono_ns"]),
            "dispatch_finished_mono_ns": int(row["dispatch_finished_mono_ns"]),
            "declaration_version": row["declaration_version"],
            "is_stale": bool(row["is_stale"]),
            "ledger_checksum": row["ledger_checksum"],
            "created_revision": int(row["created_revision"]),
        }

    def fetch_results(self, *, include_stale: bool = False, task_id: str | None = None,
                      cluster_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if not include_stale:
            clauses.append("is_stale=0")
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(task_id)
        if cluster_id is not None:
            clauses.append("cluster_id=?")
            values.append(cluster_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM results" + where + " ORDER BY task_id, cluster_id, config_version",
                values,
            ).fetchall()
            return [self._row_to_result(row) for row in rows]

    def read_consistent_state(self) -> dict[str, Any]:
        """Read revision, results, transitions, and recovery state atomically."""

        with self._read_transaction() as connection:
            revision = int(self._meta(connection, "revision") or "0")
            results = [
                self._row_to_result(row)
                for row in connection.execute(
                    "SELECT * FROM results ORDER BY task_id, cluster_id, config_version"
                ).fetchall()
            ]
            transitions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM version_transitions ORDER BY revision, cluster_id, id"
                ).fetchall()
            ]
            recovery = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM recovery_state ORDER BY task_id, cluster_id"
                ).fetchall()
            ]
            declaration_state = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM declaration_state ORDER BY cluster_id"
                ).fetchall()
            ]
        return {
            "revision": revision,
            "results": results,
            "transitions": transitions,
            "recovery": recovery,
            "declaration_state": declaration_state,
        }

    def fetch_interactions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM interactions ORDER BY id"
            ).fetchall()
            return [
                {
                    **dict(row),
                    "detail": json.loads(row["detail_json"]),
                    "exceeded_budget": bool(row["exceeded_budget"]),
                    "is_stale": bool(row["is_stale"]),
                }
                for row in rows
            ]

    def mark_recovery_unknown(self, units: Iterable[tuple[str, str, int, str]]) -> int:
        count = 0
        with self._transaction() as connection:
            for task_id, cluster_id, config_version, reason in units:
                connection.execute(
                    "INSERT INTO recovery_state(task_id, cluster_id, config_version, status, reason, updated_mono_ms) "
                    "VALUES(?, ?, ?, 'unknown', ?, CAST(strftime('%s','now') AS INTEGER) * 1000) "
                    "ON CONFLICT(task_id, cluster_id, config_version) DO UPDATE SET "
                    "status='unknown', reason=excluded.reason, updated_mono_ms=excluded.updated_mono_ms",
                    (task_id, cluster_id, config_version, reason),
                )
                count += 1
            if count:
                self._bump_revision(connection)
        return count

    def corrupt_result_summary(self, task_id: str | None = None,
                               cluster_id: str | None = None) -> dict[str, Any]:
        clauses = []
        values: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            values.append(task_id)
        if cluster_id:
            clauses.append("cluster_id=?")
            values.append(cluster_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM results" + where + " ORDER BY id LIMIT 1", values
            ).fetchone()
            if row is None:
                raise BusinessError(
                    E_LEDGER,
                    "cannot inject ledger corruption because no matching result exists",
                    details={"task_id": task_id, "cluster_id": cluster_id},
                )
            connection.execute(
                "UPDATE results SET summary=? WHERE id=?",
                (str(row["summary"]) + "-CORRUPTED", int(row["id"])),
            )
            self._bump_revision(connection)
            return {
                "task_id": row["task_id"],
                "cluster_id": row["cluster_id"],
                "config_version": int(row["config_version"]),
                "original_summary": row["summary"],
                "corrupted_summary": str(row["summary"]) + "-CORRUPTED",
            }

    def replace_from_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Wholesale replacement; never merges snapshot and live results."""

        results = snapshot.get("results", [])
        interactions = snapshot.get("interactions", [])
        declaration_state = snapshot.get("declaration_state", [])
        transitions = snapshot.get("transitions", [])
        recovery = snapshot.get("recovery_state", [])
        revision = int(snapshot.get("ledger_revision", 0))
        with self._transaction() as connection:
            connection.execute("DELETE FROM results")
            connection.execute("DELETE FROM interactions")
            connection.execute("DELETE FROM declaration_state")
            connection.execute("DELETE FROM version_transitions")
            connection.execute("DELETE FROM recovery_state")
            for result in results:
                components = result.get("components", {})
                connection.execute(
                    """
                    INSERT INTO results(
                        task_id, cluster_id, config_version, aggregation_mode, summary,
                        components_json, entry_count, calculation_duration_ms,
                        dispatch_started_mono_ms, dispatch_finished_mono_ms,
                        dispatch_started_mono_ns, dispatch_finished_mono_ns,
                        declaration_version, is_stale, ledger_checksum, created_revision
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result["task_id"],
                        result["cluster_id"],
                        int(result["config_version"]),
                        result["aggregation_mode"],
                        result["summary"],
                        result.get("components_json", canonical_json(components)),
                        int(result.get("entry_count", 0)),
                        int(result["calculation_duration_ms"]),
                        int(result["dispatch_started_mono_ms"]),
                        int(result["dispatch_finished_mono_ms"]),
                        int(result["dispatch_started_mono_ns"]),
                        int(result["dispatch_finished_mono_ns"]),
                        result["declaration_version"],
                        _bool_int(bool(result.get("is_stale", False))),
                        result.get("ledger_checksum") or result_checksum(result),
                        int(result.get("created_revision", revision)),
                    ),
                )
            for interaction in interactions:
                connection.execute(
                    "INSERT INTO interactions(kind, task_id, cluster_id, config_version, started_mono_ms, ended_mono_ms, "
                    "started_mono_ns, ended_mono_ns, data_version, status, business_code, "
                    "exceeded_budget, is_stale, detail_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        interaction["kind"],
                        interaction.get("task_id"),
                        interaction.get("cluster_id"),
                        interaction.get("config_version"),
                        int(interaction["started_mono_ms"]),
                        int(interaction["ended_mono_ms"]),
                        int(interaction["started_mono_ns"]),
                        int(interaction["ended_mono_ns"]),
                        interaction["data_version"],
                        interaction["status"],
                        interaction.get("business_code"),
                        _bool_int(bool(interaction.get("exceeded_budget", False))),
                        _bool_int(bool(interaction.get("is_stale", False))),
                        interaction.get("detail_json", canonical_json(interaction.get("detail", {}))),
                    ),
                )
            for state in declaration_state:
                connection.execute(
                    "INSERT INTO declaration_state(cluster_id, config_version, declaration_version, updated_mono_ms) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        state["cluster_id"],
                        int(state["config_version"]),
                        state["declaration_version"],
                        int(state.get("updated_mono_ms", 0)),
                    ),
                )
            for transition in transitions:
                connection.execute(
                    "INSERT INTO version_transitions(cluster_id, old_config_version, new_config_version, "
                    "declaration_version, detected_mono_ms, revision) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        transition["cluster_id"],
                        int(transition["old_config_version"]),
                        int(transition["new_config_version"]),
                        transition["declaration_version"],
                        int(transition["detected_mono_ms"]),
                        int(transition["revision"]),
                    ),
                )
            for state in recovery:
                connection.execute(
                    "INSERT INTO recovery_state(task_id, cluster_id, config_version, status, reason, updated_mono_ms) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        state["task_id"],
                        state["cluster_id"],
                        int(state["config_version"]),
                        state["status"],
                        state["reason"],
                        int(state["updated_mono_ms"]),
                    ),
                )
            self._set_meta(connection, "revision", str(revision))
            if snapshot.get("declaration_fingerprint"):
                self._set_meta(
                    connection, "declaration_fingerprint", snapshot["declaration_fingerprint"]
                )
            self._set_meta(
                connection,
                "latency_caliber_version",
                snapshot.get("latency_caliber_version", LATENCY_CALIBER_VERSION),
            )
