from __future__ import annotations

import fcntl
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .records import ConfigBundle, StoredConfig


SCHEMA = """
CREATE TABLE IF NOT EXISTS current_pointer (
    namespace TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    namespace TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    digest TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    patch_digest TEXT,
    logical_clock_ms INTEGER NOT NULL,
    change_kind TEXT NOT NULL,
    change_detail_json TEXT NOT NULL,
    config_count INTEGER NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_ns_digest
    ON versions(namespace, digest);

CREATE TABLE IF NOT EXISTS conversion_ledger (
    conversion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    version INTEGER NOT NULL,
    config_key TEXT NOT NULL,
    value_type TEXT NOT NULL,
    scale INTEGER NOT NULL,
    value_text TEXT NOT NULL,
    raw_scaled_text TEXT NOT NULL,
    carried_in_text TEXT NOT NULL,
    adjusted_scaled_text TEXT NOT NULL,
    fixed_int_text TEXT NOT NULL,
    discarded_text TEXT NOT NULL,
    carried_out_text TEXT NOT NULL,
    rounding_direction TEXT NOT NULL,
    conversion_error_text TEXT NOT NULL,
    residual_value_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(namespace, version, config_key)
);

CREATE TABLE IF NOT EXISTS version_configs (
    namespace TEXT NOT NULL,
    version INTEGER NOT NULL,
    config_key TEXT NOT NULL,
    value_type TEXT NOT NULL,
    value_text TEXT NOT NULL,
    unit_scale INTEGER NOT NULL,
    role TEXT,
    fixed_int_text TEXT,
    residual_units_text TEXT NOT NULL,
    residual_value_text TEXT NOT NULL,
    conversion_id INTEGER,
    PRIMARY KEY (namespace, version, config_key)
);
CREATE INDEX IF NOT EXISTS idx_version_configs_conversion
    ON version_configs(conversion_id);

CREATE TABLE IF NOT EXISTS compute_jobs (
    namespace TEXT NOT NULL,
    client_id TEXT NOT NULL,
    round_key TEXT NOT NULL,
    bound_version INTEGER NOT NULL,
    bound_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    input_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'committed')),
    attempt_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT,
    PRIMARY KEY (namespace, client_id, round_key)
);
CREATE INDEX IF NOT EXISTS idx_compute_jobs_status
    ON compute_jobs(status, namespace, client_id, round_key);

CREATE TABLE IF NOT EXISTS client_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    client_id TEXT NOT NULL,
    round_key TEXT NOT NULL,
    bound_version INTEGER NOT NULL,
    bound_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    error_ledger_json TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(namespace, client_id, round_key)
);
CREATE INDEX IF NOT EXISTS idx_client_results_version
    ON client_results(namespace, bound_version, client_id, round_key);

CREATE TABLE IF NOT EXISTS error_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL,
    metric TEXT NOT NULL,
    config_key TEXT NOT NULL,
    source_version INTEGER,
    conversion_id INTEGER,
    scale INTEGER,
    residual_units_text TEXT,
    residual_value_text TEXT,
    contribution_text TEXT,
    formula TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_error_ledger_result
    ON error_ledger(result_id, metric, config_key);

CREATE TABLE IF NOT EXISTS clock_watermarks (
    namespace TEXT NOT NULL,
    client_id TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, client_id)
);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class CenterStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "config_center.db"
        self.lock_path = self.state_dir / "center.lock"
        self._thread_lock = threading.RLock()
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        with self._thread_lock:
            with self.lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _initialize_schema(self) -> None:
        with self._file_lock():
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript(SCHEMA)
            finally:
                conn.close()

    @contextmanager
    def write_session(self) -> Iterator[sqlite3.Connection]:
        with self._file_lock():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @contextmanager
    def read_session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_pointer(
        self, conn: sqlite3.Connection, namespace: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM current_pointer WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        return row_to_dict(row)

    def list_pointers(self, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if conn is None:
            with self.read_session() as read_conn:
                return self.list_pointers(read_conn)
        rows = conn.execute(
            "SELECT * FROM current_pointer ORDER BY namespace"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_version_meta(
        self,
        conn: sqlite3.Connection,
        namespace: str,
        version: int,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM versions WHERE namespace = ? AND version = ?",
            (namespace, version),
        ).fetchone()
        return row_to_dict(row)

    def read_bundle(
        self,
        conn: sqlite3.Connection,
        namespace: str,
        version: int,
    ) -> ConfigBundle | None:
        meta = self.get_version_meta(conn, namespace, version)
        if meta is None:
            return None
        rows = conn.execute(
            """
            SELECT * FROM version_configs
            WHERE namespace = ? AND version = ?
            ORDER BY config_key
            """,
            (namespace, version),
        ).fetchall()
        configs: dict[str, StoredConfig] = {}
        for row in rows:
            value_type = row["value_type"]
            value_decimal = None
            if value_type in {"integer", "decimal"}:
                value_decimal = Decimal(row["value_text"])
            fixed_int = (
                int(row["fixed_int_text"])
                if row["fixed_int_text"] is not None
                else None
            )
            conversion_version = None
            carried_in_units = Decimal(0)
            discarded_units = Decimal(0)
            if row["conversion_id"] is not None:
                ledger_row = conn.execute(
                    """
                    SELECT version, carried_in_text, discarded_text
                    FROM conversion_ledger
                    WHERE conversion_id = ?
                    """,
                    (row["conversion_id"],),
                ).fetchone()
                if ledger_row is not None:
                    conversion_version = int(ledger_row["version"])
                    carried_in_units = Decimal(ledger_row["carried_in_text"])
                    discarded_units = Decimal(ledger_row["discarded_text"])
            cfg = StoredConfig(
                key=row["config_key"],
                value_type=value_type,
                value_text=row["value_text"],
                value_decimal=value_decimal,
                scale=int(row["unit_scale"]),
                role=row["role"],
                fixed_int=fixed_int,
                carried_in_units=carried_in_units,
                discarded_units=discarded_units,
                residual_units=Decimal(row["residual_units_text"]),
                residual_value=Decimal(row["residual_value_text"]),
                conversion_id=row["conversion_id"],
                conversion_version=conversion_version,
            )
            configs[cfg.key] = cfg
        return ConfigBundle(
            namespace=namespace,
            version=version,
            digest=meta["digest"],
            content_digest=meta["content_digest"],
            logical_clock_ms=int(meta["logical_clock_ms"]),
            configs=configs,
        )

    def read_current_bundle(self, namespace: str) -> ConfigBundle | None:
        with self.read_session() as conn:
            pointer = self.get_pointer(conn, namespace)
            if pointer is None:
                return None
            return self.read_bundle(conn, namespace, int(pointer["version"]))

    def read_version_bundle(self, namespace: str, version: int) -> ConfigBundle | None:
        with self.read_session() as conn:
            return self.read_bundle(conn, namespace, version)

    def insert_conversion(
        self,
        conn: sqlite3.Connection,
        *,
        namespace: str,
        version: int,
        config_key: str,
        value_type: str,
        scale: int,
        value_text: str,
        raw_scaled_text: str,
        carried_in_text: str,
        adjusted_scaled_text: str,
        fixed_int_text: str,
        discarded_text: str,
        carried_out_text: str,
        rounding_direction: str,
        conversion_error_text: str,
        residual_value_text: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO conversion_ledger (
                namespace, version, config_key, value_type, scale, value_text,
                raw_scaled_text, carried_in_text, adjusted_scaled_text,
                fixed_int_text, discarded_text, carried_out_text,
                rounding_direction, conversion_error_text, residual_value_text,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                namespace,
                version,
                config_key,
                value_type,
                scale,
                value_text,
                raw_scaled_text,
                carried_in_text,
                adjusted_scaled_text,
                fixed_int_text,
                discarded_text,
                carried_out_text,
                rounding_direction,
                conversion_error_text,
                residual_value_text,
                now_utc(),
            ),
        )
        return int(cursor.lastrowid)

    def insert_version_configs(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        *,
        full_replace: bool,
    ) -> None:
        if not full_replace:
            conn.executemany(
                """
                INSERT INTO version_configs (
                    namespace, version, config_key, value_type, value_text,
                    unit_scale, role, fixed_int_text, residual_units_text,
                    residual_value_text, conversion_id
                ) VALUES (
                    :namespace, :version, :config_key, :value_type, :value_text,
                    :unit_scale, :role, :fixed_int_text, :residual_units_text,
                    :residual_value_text, :conversion_id
                )
                """,
                rows,
            )
            return

        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS version_stage (
                namespace TEXT NOT NULL,
                version INTEGER NOT NULL,
                config_key TEXT NOT NULL,
                value_type TEXT NOT NULL,
                value_text TEXT NOT NULL,
                unit_scale INTEGER NOT NULL,
                role TEXT,
                fixed_int_text TEXT,
                residual_units_text TEXT NOT NULL,
                residual_value_text TEXT NOT NULL,
                conversion_id INTEGER,
                PRIMARY KEY (namespace, version, config_key)
            )
            """
        )
        conn.execute("DELETE FROM temp.version_stage")
        conn.executemany(
            """
            INSERT INTO temp.version_stage (
                namespace, version, config_key, value_type, value_text,
                unit_scale, role, fixed_int_text, residual_units_text,
                residual_value_text, conversion_id
            ) VALUES (
                :namespace, :version, :config_key, :value_type, :value_text,
                :unit_scale, :role, :fixed_int_text, :residual_units_text,
                :residual_value_text, :conversion_id
            )
            """,
            rows,
        )
        conn.execute(
            """
            INSERT INTO version_configs (
                namespace, version, config_key, value_type, value_text,
                unit_scale, role, fixed_int_text, residual_units_text,
                residual_value_text, conversion_id
            )
            SELECT namespace, version, config_key, value_type, value_text,
                   unit_scale, role, fixed_int_text, residual_units_text,
                   residual_value_text, conversion_id
            FROM temp.version_stage
            """
        )
        conn.execute("DELETE FROM temp.version_stage")

    def update_pointer(
        self,
        conn: sqlite3.Connection,
        *,
        namespace: str,
        version: int,
        digest: str,
        logical_clock_ms: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO current_pointer (
                namespace, version, digest, logical_clock_ms, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace) DO UPDATE SET
                version = excluded.version,
                digest = excluded.digest,
                logical_clock_ms = excluded.logical_clock_ms,
                updated_at = excluded.updated_at
            """,
            (namespace, version, digest, logical_clock_ms, now_utc()),
        )

    def list_versions(self, namespace: str | None = None) -> list[dict[str, Any]]:
        with self.read_session() as conn:
            if namespace is None:
                rows = conn.execute(
                    """
                    SELECT v.*, p.version AS pointer_version
                    FROM versions v
                    LEFT JOIN current_pointer p
                      ON p.namespace = v.namespace
                    ORDER BY v.namespace, v.version
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT v.*, p.version AS pointer_version
                    FROM versions v
                    LEFT JOIN current_pointer p
                      ON p.namespace = v.namespace
                    WHERE v.namespace = ?
                    ORDER BY v.version
                    """,
                    (namespace,),
                ).fetchall()
            return [
                {
                    **dict(row),
                    "is_current": row["pointer_version"] == row["version"],
                }
                for row in rows
            ]

    def list_configs(self, namespace: str, version: int) -> list[dict[str, Any]]:
        bundle = self.read_version_bundle(namespace, version)
        if bundle is None:
            return []
        return [cfg.to_dict() for _, cfg in sorted(bundle.configs.items())]

    def get_job(
        self,
        conn: sqlite3.Connection,
        namespace: str,
        client_id: str,
        round_key: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT * FROM compute_jobs
            WHERE namespace = ? AND client_id = ? AND round_key = ?
            """,
            (namespace, client_id, round_key),
        ).fetchone()
        return row_to_dict(row)

    def get_result(
        self,
        conn: sqlite3.Connection,
        namespace: str,
        client_id: str,
        round_key: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT * FROM client_results
            WHERE namespace = ? AND client_id = ? AND round_key = ?
            """,
            (namespace, client_id, round_key),
        ).fetchone()
        return row_to_dict(row)

    def list_pending_jobs(
        self,
        namespace: str | None = None,
        *,
        client_id: str | None = None,
        round_key: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["status = 'pending'"]
        params: list[Any] = []
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        if client_id is not None:
            clauses.append("client_id = ?")
            params.append(client_id)
        if round_key is not None:
            clauses.append("round_key = ?")
            params.append(round_key)
        where = " AND ".join(clauses)
        with self.read_session() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM compute_jobs
                WHERE {where}
                ORDER BY namespace, client_id, round_key
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_results(
        self,
        namespace: str | None = None,
        *,
        client_id: str | None = None,
        round_key: str | None = None,
        full: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        if client_id is not None:
            clauses.append("client_id = ?")
            params.append(client_id)
        if round_key is not None:
            clauses.append("round_key = ?")
            params.append(round_key)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.read_session() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM client_results
                {where}
                ORDER BY namespace, client_id, round_key
                """,
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        import json

        for row in rows:
            item = dict(row)
            if full:
                item["result"] = json.loads(item.pop("result_json"))
                item["error_ledger"] = json.loads(item.pop("error_ledger_json"))
            else:
                payload = json.loads(item.pop("result_json"))
                item.pop("error_ledger_json", None)
                item["result_summary"] = {
                    "bound_version": payload.get("bound_version"),
                    "result_digest": payload.get("result_digest"),
                    "precision_sensitive": payload.get("sensitivity", {}).get(
                        "precision_sensitive", False
                    ),
                    "derived": {
                        key: value.get("fixed")
                        for key, value in payload.get("metrics", {}).items()
                    },
                }
            item["precision_sensitive"] = bool(item["precision_sensitive"])
            result.append(item)
        return result

    def read_result_full(
        self,
        namespace: str,
        client_id: str,
        round_key: str,
    ) -> dict[str, Any] | None:
        with self.read_session() as conn:
            row = self.get_result(conn, namespace, client_id, round_key)
            if row is None:
                return None
            import json

            row["result"] = json.loads(row.pop("result_json"))
            row["error_ledger"] = json.loads(row.pop("error_ledger_json"))
            row["precision_sensitive"] = bool(row["precision_sensitive"])
            return row

    def get_job_input(
        self,
        namespace: str,
        client_id: str,
        round_key: str,
    ) -> dict[str, Any] | None:
        with self.read_session() as conn:
            row = self.get_job(conn, namespace, client_id, round_key)
            if row is None:
                return None
            import json

            row["input"] = json.loads(row.pop("input_json"))
            return row

    def status(self, namespace: str | None = None) -> dict[str, Any]:
        pointers = self.list_pointers()
        if namespace is not None:
            pointers = [p for p in pointers if p["namespace"] == namespace]
        versions = self.list_versions(namespace)
        results = self.list_results(namespace)
        pending = self.list_pending_jobs(namespace)
        with self.read_session() as conn:
            if namespace is None:
                watermarks = conn.execute(
                    """
                    SELECT * FROM clock_watermarks
                    ORDER BY namespace, client_id
                    """
                ).fetchall()
            else:
                watermarks = conn.execute(
                    """
                    SELECT * FROM clock_watermarks
                    WHERE namespace = ?
                    ORDER BY namespace, client_id
                    """,
                    (namespace,),
                ).fetchall()
        clients: dict[tuple[str, str], dict[str, Any]] = {}
        for row in results:
            key = (row["namespace"], row["client_id"])
            bucket = clients.setdefault(
                key,
                {
                    "namespace": row["namespace"],
                    "client_id": row["client_id"],
                    "rounds": [],
                    "precision_sensitive_count": 0,
                },
            )
            bucket["rounds"].append(
                {
                    "round_key": row["round_key"],
                    "bound_version": row["bound_version"],
                    "bound_digest": row["bound_digest"],
                    "result_digest": row["result_digest"],
                    "logical_clock_ms": row["logical_clock_ms"],
                    "precision_sensitive": row["precision_sensitive"],
                    "derived": row["result_summary"]["derived"],
                    "created_at": row["created_at"],
                }
            )
            if row["precision_sensitive"]:
                bucket["precision_sensitive_count"] += 1
        for bucket in clients.values():
            bucket["rounds"].sort(key=lambda item: item["round_key"])
        client_list = sorted(
            clients.values(),
            key=lambda item: (item["namespace"], item["client_id"]),
        )
        pointers = sorted(pointers, key=lambda item: item["namespace"])
        versions_payload = [
            {
                "namespace": version["namespace"],
                "version": version["version"],
                "parent_version": version["parent_version"],
                "digest": version["digest"],
                "content_digest": version["content_digest"],
                "patch_digest": version["patch_digest"],
                "logical_clock_ms": version["logical_clock_ms"],
                "change_kind": version["change_kind"],
                "config_count": version["config_count"],
                "is_current": version["is_current"],
                "created_at": version["created_at"],
            }
            for version in versions
        ]
        return {
            "pointers": pointers,
            "versions": versions_payload,
            "clients": client_list,
            "pending_jobs": [
                {
                    "namespace": job["namespace"],
                    "client_id": job["client_id"],
                    "round_key": job["round_key"],
                    "bound_version": job["bound_version"],
                    "input_digest": job["input_digest"],
                    "logical_clock_ms": job["logical_clock_ms"],
                    "attempt_count": job["attempt_count"],
                    "created_at": job["created_at"],
                }
                for job in pending
            ],
            "client_watermarks": [dict(row) for row in watermarks],
            "precision_sensitive_count": sum(
                bucket["precision_sensitive_count"] for bucket in client_list
            ),
        }

    def reset_business_state(self) -> None:
        with self.write_session() as conn:
            for table in (
                "error_ledger",
                "client_results",
                "compute_jobs",
                "clock_watermarks",
                "version_configs",
                "conversion_ledger",
                "versions",
                "current_pointer",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('conversion_ledger', 'client_results', 'error_ledger')"
            )

