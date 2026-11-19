from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import GatewayConfig, validate_against_watermarks, version_watermarks
from .errors import GatewayError

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS configs (
    version INTEGER PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    document TEXT NOT NULL,
    activated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS token_buckets (
    config_version INTEGER NOT NULL,
    bucket_kind TEXT NOT NULL CHECK (bucket_kind IN ('route', 'tenant')),
    bucket_key TEXT NOT NULL,
    tokens_units INTEGER NOT NULL,
    capacity_units INTEGER NOT NULL,
    refill_per_second INTEGER NOT NULL,
    last_refill_ms INTEGER NOT NULL,
    PRIMARY KEY (config_version, bucket_kind, bucket_key)
);

CREATE TABLE IF NOT EXISTS window_buckets (
    config_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    route_id TEXT NOT NULL,
    bucket_index INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    rejections INTEGER NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    PRIMARY KEY (config_version, tenant, route_id, bucket_index)
);

CREATE TABLE IF NOT EXISTS sliding_states (
    config_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    route_id TEXT NOT NULL,
    current_bucket INTEGER NOT NULL,
    window_digest TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (config_version, tenant, route_id)
);

CREATE TABLE IF NOT EXISTS requests (
    tenant TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    config_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    credential_fingerprint TEXT NOT NULL,
    logical_time_ms INTEGER NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query_string TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'committed', 'recovered')),
    admission_result TEXT NOT NULL,
    final_result TEXT,
    decision_chain TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    committed_at_ms INTEGER,
    PRIMARY KEY (tenant, request_seq),
    UNIQUE (tenant, request_id)
);

CREATE INDEX IF NOT EXISTS idx_requests_pending
ON requests (state, tenant, request_seq);

CREATE TABLE IF NOT EXISTS revocations (
    credential_type TEXT NOT NULL CHECK (credential_type IN ('api_key', 'jwt')),
    credential_id TEXT NOT NULL,
    revoked_at_ms INTEGER NOT NULL,
    source_version INTEGER NOT NULL,
    PRIMARY KEY (credential_type, credential_id)
);

CREATE TABLE IF NOT EXISTS auth_watermarks (
    credential_type TEXT PRIMARY KEY,
    max_version INTEGER NOT NULL,
    active_version INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    tenant TEXT,
    config_version INTEGER,
    logical_time_ms INTEGER NOT NULL,
    code TEXT NOT NULL,
    stage TEXT NOT NULL,
    decision_chain TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    finalized_at_ms INTEGER NOT NULL,
    detail TEXT NOT NULL
);
"""


def _safe_name(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]


class FileLock:
    """Cross-platform advisory file lock used to serialize one tenant."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        if os.name == "nt":
            assert msvcrt is not None
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            assert fcntl is not None
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                assert msvcrt is not None
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                assert fcntl is not None
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class Store:
    def __init__(self, database_path: str | os.PathLike[str], lock_dir: str | os.PathLike[str] | None = None):
        self.database_path = Path(database_path)
        self.lock_dir = Path(lock_dir) if lock_dir is not None else self.database_path.parent / "locks"

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.database_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def tenant_lock(self, tenant: str) -> FileLock:
        return FileLock(self.lock_dir / f"tenant-{_safe_name(tenant)}.lock")

    def config_lock(self) -> FileLock:
        return FileLock(self.lock_dir / "config.lock")

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM gateway_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @classmethod
    def _set_meta(cls, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO gateway_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def current_version(self) -> int | None:
        with self.read_connection() as conn:
            value = self._meta(conn, "current_config_version")
            return None if value is None else int(value)

    def load_active_config(self) -> tuple[int, str, dict[str, Any]] | None:
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT c.version, c.digest, c.document "
                "FROM configs c JOIN gateway_meta m "
                "ON m.key = 'current_config_version' AND CAST(m.value AS INTEGER) = c.version"
            ).fetchone()
            if row is None:
                return None
            return int(row["version"]), str(row["digest"]), json.loads(row["document"])

    def load_active_snapshot(
        self,
    ) -> tuple[int, str, dict[str, Any], dict[tuple[str, str], int]] | None:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT c.version, c.digest, c.document "
                "FROM configs c JOIN gateway_meta m "
                "ON m.key = 'current_config_version' AND CAST(m.value AS INTEGER) = c.version"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            revocations = self.get_revocations(conn)
            conn.execute("COMMIT")
            return (
                int(row["version"]),
                str(row["digest"]),
                json.loads(row["document"]),
                revocations,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def config_meta(self) -> dict[str, Any] | None:
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT c.version, c.digest, c.activated_at_ms "
                "FROM configs c JOIN gateway_meta m "
                "ON m.key = 'current_config_version' AND CAST(m.value AS INTEGER) = c.version"
            ).fetchone()
            if row is None:
                return None
            return {
                "version": int(row["version"]),
                "digest": str(row["digest"]),
                "activated_at_ms": int(row["activated_at_ms"]),
            }

    def activate_config(
        self,
        config: GatewayConfig,
        expected_current_version: int,
        logical_time_ms: int,
    ) -> dict[str, Any]:
        with self.config_lock():
            with self.transaction() as conn:
                current_value = self._meta(conn, "current_config_version")
                current = None if current_value is None else int(current_value)
                if expected_current_version != (current or 0):
                    raise GatewayError(
                        "E_CONFIG_VERSION_MISMATCH",
                        "expected current config version does not match the active snapshot",
                        409,
                        "config",
                        {"expected": expected_current_version, "current": current or 0},
                    )
                if current is not None and config.version <= current:
                    raise GatewayError(
                        "E_CONFIG_ROLLBACK",
                        "config version must increase",
                        409,
                        "config",
                        {"current": current, "proposed": config.version},
                    )
                persisted = self.get_watermarks(conn)
                validate_against_watermarks(config, persisted)
                conn.execute(
                    "INSERT INTO configs(version, digest, document, activated_at_ms) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        config.version,
                        config.digest,
                        json.dumps(config.raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        logical_time_ms,
                    ),
                )
                self._set_meta(conn, "current_config_version", str(config.version))
                for item in config.auth.revocations:
                    conn.execute(
                        "INSERT OR IGNORE INTO revocations"
                        "(credential_type, credential_id, revoked_at_ms, source_version) "
                        "VALUES(?, ?, ?, ?)",
                        (
                            item["type"],
                            item["id"],
                            item["revoked_at_ms"],
                            config.version,
                        ),
                    )
                for kind, values in version_watermarks(config).items():
                    conn.execute(
                        "INSERT INTO auth_watermarks"
                        "(credential_type, max_version, active_version, effective_at_ms) "
                        "VALUES(?, ?, ?, ?) "
                        "ON CONFLICT(credential_type) DO UPDATE SET "
                        "max_version = excluded.max_version, "
                        "active_version = excluded.active_version, "
                        "effective_at_ms = excluded.effective_at_ms",
                        (
                            kind,
                            values["max_version"],
                            values["active_version"],
                            values["effective_at_ms"],
                        ),
                    )
                return {
                    "version": config.version,
                    "digest": config.digest,
                    "previous_version": current or 0,
                }

    def get_watermarks(self, conn: sqlite3.Connection) -> dict[str, tuple[int, int, int]]:
        rows = conn.execute(
            "SELECT credential_type, max_version, active_version, effective_at_ms "
            "FROM auth_watermarks"
        ).fetchall()
        return {
            str(row["credential_type"]): (
                int(row["max_version"]),
                int(row["active_version"]),
                int(row["effective_at_ms"]),
            )
            for row in rows
        }

    def get_revocations(self, conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
        rows = conn.execute(
            "SELECT credential_type, credential_id, revoked_at_ms FROM revocations"
        ).fetchall()
        return {
            (str(row["credential_type"]), str(row["credential_id"])): int(row["revoked_at_ms"])
            for row in rows
        }

    def advance_logical_clock(
        self,
        conn: sqlite3.Connection,
        requested_ms: int,
        tolerance_ms: int,
    ) -> tuple[int, int]:
        previous_value = self._meta(conn, "logical_clock_ms")
        if previous_value is None:
            effective = requested_ms
            self._set_meta(conn, "logical_clock_ms", str(effective))
            return effective, 0
        previous = int(previous_value)
        if requested_ms < previous - tolerance_ms:
            raise GatewayError(
                "E_CLOCK_REWIND",
                "logical clock moved backwards beyond the configured tolerance",
                409,
                "clock",
                {
                    "previous_ms": previous,
                    "requested_ms": requested_ms,
                    "tolerance_ms": tolerance_ms,
                },
            )
        effective = max(previous, requested_ms)
        if effective != previous:
            self._set_meta(conn, "logical_clock_ms", str(effective))
        return effective, effective - requested_ms

    def get_logical_clock(self, conn: sqlite3.Connection) -> int:
        value = self._meta(conn, "logical_clock_ms")
        return 0 if value is None else int(value)

    def find_request(self, conn: sqlite3.Connection, tenant: str, request_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM requests WHERE tenant = ? AND request_id = ?",
            (tenant, request_id),
        ).fetchone()

    def find_sequence(self, conn: sqlite3.Connection, tenant: str, request_seq: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM requests WHERE tenant = ? AND request_seq = ?",
            (tenant, request_seq),
        ).fetchone()

    @staticmethod
    def insert_audit(
        conn: sqlite3.Connection,
        *,
        request_id: str | None,
        tenant: str | None,
        config_version: int | None,
        logical_time_ms: int,
        code: str,
        stage: str,
        decision_chain: list[dict[str, Any]],
        details: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events("
            "request_id, tenant, config_version, logical_time_ms, code, stage, "
            "decision_chain, details, created_at_ms"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                tenant,
                config_version,
                logical_time_ms,
                code,
                stage,
                json.dumps(decision_chain, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                logical_time_ms,
            ),
        )

    @staticmethod
    def canonical_result(result: dict[str, Any]) -> dict[str, Any]:
        body = result.get("body", b"")
        if isinstance(body, str):
            body = body.encode("utf-8")
        return {
            "http_status": int(result["http_status"]),
            "headers": [list(pair) for pair in result.get("headers", [])],
            "body_b64": base64.b64encode(body).decode("ascii"),
            "code": result.get("code"),
        }

    @staticmethod
    def decode_result(result_json: str) -> dict[str, Any]:
        result = json.loads(result_json)
        return {
            "http_status": int(result["http_status"]),
            "headers": [tuple(pair) for pair in result.get("headers", [])],
            "body": base64.b64decode(result.get("body_b64", "")),
            "code": result.get("code"),
        }

    def insert_pending_request(
        self,
        conn: sqlite3.Connection,
        *,
        tenant: str,
        request_seq: int,
        request_id: str,
        config: GatewayConfig,
        request_digest: str,
        credential_fingerprint: str,
        logical_time_ms: int,
        method: str,
        path: str,
        query_string: str,
        admission_result: dict[str, Any],
        decision_chain: list[dict[str, Any]],
    ) -> None:
        conn.execute(
            "INSERT INTO requests("
            "tenant, request_seq, request_id, config_version, config_digest, "
            "request_digest, credential_fingerprint, logical_time_ms, method, path, "
            "query_string, state, admission_result, final_result, decision_chain, "
            "created_at_ms, committed_at_ms"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, ?, NULL)",
            (
                tenant,
                request_seq,
                request_id,
                config.version,
                config.digest,
                request_digest,
                credential_fingerprint,
                logical_time_ms,
                method,
                path,
                query_string,
                json.dumps(
                    self.canonical_result(admission_result),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(decision_chain, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                logical_time_ms,
            ),
        )

    def finalize_request(
        self,
        conn: sqlite3.Connection,
        *,
        tenant: str,
        request_seq: int,
        result: dict[str, Any],
        decision_chain: list[dict[str, Any]],
        logical_time_ms: int,
        state: str = "committed",
    ) -> None:
        conn.execute(
            "UPDATE requests SET state = ?, final_result = ?, decision_chain = ?, "
            "committed_at_ms = ? WHERE tenant = ? AND request_seq = ? AND state = 'pending'",
            (
                state,
                json.dumps(
                    self.canonical_result(result),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(decision_chain, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                logical_time_ms,
                tenant,
                request_seq,
            ),
        )

    def commit_direct_request(
        self,
        conn: sqlite3.Connection,
        *,
        tenant: str,
        request_seq: int,
        request_id: str,
        config: GatewayConfig,
        request_digest: str,
        credential_fingerprint: str,
        logical_time_ms: int,
        method: str,
        path: str,
        query_string: str,
        result: dict[str, Any],
        decision_chain: list[dict[str, Any]],
    ) -> None:
        encoded = json.dumps(
            self.canonical_result(result),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO requests("
            "tenant, request_seq, request_id, config_version, config_digest, "
            "request_digest, credential_fingerprint, logical_time_ms, method, path, "
            "query_string, state, admission_result, final_result, decision_chain, "
            "created_at_ms, committed_at_ms"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?, ?, ?, ?)",
            (
                tenant,
                request_seq,
                request_id,
                config.version,
                config.digest,
                request_digest,
                credential_fingerprint,
                logical_time_ms,
                method,
                path,
                query_string,
                encoded,
                encoded,
                json.dumps(decision_chain, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                logical_time_ms,
                logical_time_ms,
            ),
        )

    def finalize_pending_as_recovered(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        result = {
            "http_status": 503,
            "headers": [("Content-Type", "application/json")],
            "body": json.dumps(
                {
                    "code": "E_RECOVERY_INCOMPLETE",
                    "message": "worker terminated after admission reservation before upstream commit",
                    "request_id": str(row["request_id"]),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            "code": "E_RECOVERY_INCOMPLETE",
        }
        chain = json.loads(row["decision_chain"])
        chain.append(
            {
                "stage": "recovery",
                "decision": "finalize",
                "code": "E_RECOVERY_INCOMPLETE",
                "detail": (
                    "admission reservation was already committed; "
                    "tokens and window counts are not reapplied or refunded"
                ),
                "logical_time_ms": int(row["logical_time_ms"]),
            }
        )
        conn.execute(
            "UPDATE requests SET state = 'recovered', final_result = ?, "
            "decision_chain = ?, committed_at_ms = ? "
            "WHERE tenant = ? AND request_seq = ? AND state = 'pending'",
            (
                json.dumps(
                    self.canonical_result(result),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    chain,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                int(row["logical_time_ms"]),
                str(row["tenant"]),
                int(row["request_seq"]),
            ),
        )
        conn.execute(
            "INSERT INTO recovery_events("
            "tenant, request_seq, request_id, config_version, finalized_at_ms, detail"
            ") VALUES(?, ?, ?, ?, ?, ?)",
            (
                str(row["tenant"]),
                int(row["request_seq"]),
                str(row["request_id"]),
                int(row["config_version"]),
                int(row["logical_time_ms"]),
                "pending admission finalized without duplicate token or window charge",
            ),
        )
        return {
            "result": result,
            "decision_chain": chain,
            "tenant": str(row["tenant"]),
            "request_seq": int(row["request_seq"]),
            "request_id": str(row["request_id"]),
            "config_version": int(row["config_version"]),
        }

    def recover_pending(self) -> dict[str, Any]:
        with self.read_connection() as conn:
            rows = conn.execute(
                "SELECT tenant, request_seq, request_id, config_version, "
                "logical_time_ms FROM requests WHERE state = 'pending' "
                "ORDER BY tenant, request_seq"
            ).fetchall()
        recovered: list[dict[str, Any]] = []
        for row in rows:
            tenant = str(row["tenant"])
            with self.tenant_lock(tenant):
                with self.transaction() as conn:
                    latest = conn.execute(
                        "SELECT * FROM requests WHERE tenant = ? AND request_seq = ? AND state = 'pending'",
                        (tenant, int(row["request_seq"])),
                    ).fetchone()
                    if latest is None:
                        continue
                    settled = self.finalize_pending_as_recovered(conn, latest)
                    recovered.append(
                        {
                            "tenant": settled["tenant"],
                            "request_seq": settled["request_seq"],
                            "request_id": settled["request_id"],
                            "config_version": settled["config_version"],
                        }
                    )
        return {"recovered_count": len(recovered), "recovered": recovered}

    def state_snapshot(self, tenant: str | None = None) -> dict[str, Any]:
        with self.read_connection() as conn:
            conn.execute("BEGIN")
            config_row = conn.execute(
                "SELECT c.version, c.digest, c.activated_at_ms "
                "FROM configs c JOIN gateway_meta m "
                "ON m.key = 'current_config_version' AND CAST(m.value AS INTEGER) = c.version"
            ).fetchone()
            config_meta = (
                None
                if config_row is None
                else {
                    "version": int(config_row["version"]),
                    "digest": str(config_row["digest"]),
                    "activated_at_ms": int(config_row["activated_at_ms"]),
                }
            )
            clock_value = self._meta(conn, "logical_clock_ms")
            state: dict[str, Any] = {
                "active_config": config_meta,
                "logical_clock_ms": None if clock_value is None else int(clock_value),
                "request_counts": {},
                "token_buckets": [],
                "window_buckets": [],
                "revocations": [
                    {"type": str(row["credential_type"]), "id": str(row["credential_id"])}
                    for row in conn.execute(
                        "SELECT credential_type, credential_id FROM revocations "
                        "ORDER BY credential_type, credential_id"
                    ).fetchall()
                ],
                "auth_watermarks": {
                    str(row["credential_type"]): {
                        "max_version": int(row["max_version"]),
                        "active_version": int(row["active_version"]),
                        "effective_at_ms": int(row["effective_at_ms"]),
                    }
                    for row in conn.execute("SELECT * FROM auth_watermarks").fetchall()
                },
                "recovery_events": [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM recovery_events ORDER BY id"
                    ).fetchall()
                ],
            }
            counts = conn.execute(
                "SELECT state, COUNT(*) AS count FROM requests GROUP BY state"
            ).fetchall()
            state["request_counts"] = {str(row["state"]): int(row["count"]) for row in counts}
            if config_meta is not None:
                version = config_meta["version"]
                token_sql = "SELECT * FROM token_buckets WHERE config_version = ?"
                window_sql = "SELECT * FROM window_buckets WHERE config_version = ?"
                params: list[Any] = [version]
                if tenant is not None:
                    token_sql += " AND (bucket_kind != 'tenant' OR bucket_key = ?)"
                    params.append(tenant)
                state["token_buckets"] = [
                    dict(row) for row in conn.execute(token_sql + " ORDER BY bucket_kind, bucket_key", params)
                ]
                window_params: list[Any] = [version]
                window_sql += " AND tenant = ?" if tenant is not None else ""
                if tenant is not None:
                    window_params.append(tenant)
                state["window_buckets"] = [
                    dict(row)
                    for row in conn.execute(
                        window_sql + " ORDER BY tenant, route_id, bucket_index", window_params
                    )
                ]
            return state
