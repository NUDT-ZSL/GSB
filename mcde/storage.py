from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    E_APPROVAL_ALREADY_CONSUMED,
    E_APPROVAL_EXPIRED,
    E_NOT_LOADED,
    E_NO_ACTIVE_VERSION,
    E_QUOTA_REPLICAS,
    E_QUOTA_RESOURCES,
    E_STATE_CONFLICT,
    E_VERSION_CONFLICT,
    E_VERSION_ROLLBACK,
    EngineError,
    StateError,
)
from .locking import FileLock, cluster_lock_name, keyed_lock_name
from .model import Bundle, ClockScript
from .util import canonical_json, pretty_json, sha256_json, wall_ms

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    digest TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    loaded_wall_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS active_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    digest TEXT NOT NULL REFERENCES snapshots(digest)
);

CREATE TABLE IF NOT EXISTS config_versions (
    version INTEGER PRIMARY KEY,
    request_digest TEXT NOT NULL UNIQUE,
    plan_digest TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL REFERENCES snapshots(digest),
    payload_json TEXT NOT NULL,
    created_wall_ms INTEGER NOT NULL,
    created_logical_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS active_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL REFERENCES config_versions(version),
    request_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    cluster_id TEXT PRIMARY KEY,
    replicas_used INTEGER NOT NULL DEFAULT 0,
    resources_used INTEGER NOT NULL DEFAULT 0,
    replicas_capacity INTEGER NOT NULL,
    resources_capacity INTEGER NOT NULL,
    version INTEGER,
    updated_logical_ms INTEGER,
    CHECK (replicas_used >= 0),
    CHECK (resources_used >= 0),
    CHECK (replicas_used <= replicas_capacity),
    CHECK (resources_used <= resources_capacity)
);

CREATE TABLE IF NOT EXISTS cluster_commits (
    cluster_id TEXT NOT NULL,
    version INTEGER NOT NULL REFERENCES config_versions(version),
    effective_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    replicas_total INTEGER NOT NULL,
    resources_total INTEGER NOT NULL,
    committed_logical_ms INTEGER NOT NULL,
    PRIMARY KEY (cluster_id, version)
);

CREATE TABLE IF NOT EXISTS cluster_state (
    cluster_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    readback_json TEXT NOT NULL,
    updated_logical_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_uses (
    approval_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    cluster_id TEXT NOT NULL,
    key_path TEXT NOT NULL,
    consumed_logical_ms INTEGER NOT NULL,
    UNIQUE (version, cluster_id, key_path, approval_id)
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL REFERENCES config_versions(version),
    round_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    max_parallel INTEGER NOT NULL,
    budget_ms INTEGER NOT NULL,
    earliest_ms INTEGER,
    started_logical_ms INTEGER,
    finished_logical_ms INTEGER,
    reason_code TEXT,
    detail_json TEXT NOT NULL,
    UNIQUE (version, round_no)
);

CREATE TABLE IF NOT EXISTS dispatches (
    version INTEGER NOT NULL REFERENCES config_versions(version),
    cluster_id TEXT NOT NULL,
    status TEXT NOT NULL,
    round_id TEXT,
    slot INTEGER,
    started_logical_ms INTEGER,
    finished_logical_ms INTEGER,
    reason_code TEXT,
    reason TEXT,
    detail_json TEXT NOT NULL,
    updated_wall_ms INTEGER NOT NULL,
    PRIMARY KEY (version, cluster_id)
);

CREATE TABLE IF NOT EXISTS dispatch_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    cluster_id TEXT,
    round_id TEXT,
    status TEXT NOT NULL,
    logical_ms INTEGER NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    now_ms INTEGER NOT NULL,
    cursor INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_ms INTEGER NOT NULL,
    wall_ms INTEGER NOT NULL,
    event TEXT NOT NULL,
    version INTEGER,
    cluster_id TEXT,
    detail_json TEXT NOT NULL
);
"""


def _crash(point: str) -> None:
    if os.environ.get("MCDE_CRASH_AT") == point:
        os._exit(137)


def _capacity_for(
    bundle: Bundle, cluster_id: str, field_name: str
) -> int:
    member = bundle.inventory[cluster_id]
    overlay = bundle.cluster_overlays.get(cluster_id)
    if field_name == "replicas":
        inventory_value = member.max_replicas
        overlay_value = overlay.replicas_limit if overlay else None
    else:
        inventory_value = member.max_resources
        overlay_value = overlay.resource_quota if overlay else None
    if overlay_value is not None:
        return min(inventory_value, overlay_value)
    return inventory_value


def _totals(config: dict[str, Any]) -> tuple[int, int]:
    replicas = 0
    resources = 0
    for app in config.get("apps", {}).values():
        replicas += int(app["replicas"])
        resources += int(app["resources"])
    return replicas, resources


class Repository:
    def __init__(self, db_path: str | Path, lock_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.lock_dir = (
            Path(lock_dir)
            if lock_dir is not None
            else self.db_path.parent / f"{self.db_path.name}.locks"
        )

    @property
    def exists(self) -> bool:
        return self.db_path.is_file()

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        if not create and not self.exists:
            raise StateError(
                E_NOT_LOADED,
                "state database does not exist; run load first",
                db=str(self.db_path),
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def transaction(
        self, conn: sqlite3.Connection, *, immediate: bool = True
    ) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _initialize_schema(self, conn: sqlite3.Connection) -> None:
        # executescript() may issue its own COMMIT, so all DDL is idempotent
        # and runs before the metadata transaction.
        conn.executescript(SCHEMA_SQL)
        with self.transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def initialize(self) -> None:
        conn = self._connect(create=True)
        try:
            self._initialize_schema(conn)
        finally:
            conn.close()

    def global_lock(self) -> FileLock:
        return FileLock(self.lock_dir / "global.lock")

    def cluster_lock(self, cluster_id: str) -> FileLock:
        return FileLock(self.lock_dir / cluster_lock_name(cluster_id))

    def request_lock(self, request_digest: str) -> FileLock:
        return FileLock(
            self.lock_dir / keyed_lock_name("request", request_digest),
            timeout_s=120.0,
        )

    def version_lock(self, version: int) -> FileLock:
        return FileLock(
            self.lock_dir / keyed_lock_name("version", str(version)),
            timeout_s=120.0,
        )

    def audit(
        self,
        conn: sqlite3.Connection,
        *,
        logical_ms: int,
        event: str,
        version: int | None = None,
        cluster_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_log(
                logical_ms, wall_ms, event, version, cluster_id, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                logical_ms,
                wall_ms(),
                event,
                version,
                cluster_id,
                canonical_json(detail or {}),
            ),
        )

    def load_snapshot(self, bundle: Bundle) -> dict[str, Any]:
        digest = bundle.digest()
        conn = self._connect(create=True)
        try:
            with self.global_lock():
                self._initialize_schema(conn)
                with self.transaction(conn):
                    active_row = conn.execute(
                        "SELECT digest FROM active_snapshot WHERE id = 1"
                    ).fetchone()
                    previous_digest = (
                        active_row["digest"] if active_row is not None else None
                    )
                    clock_reset = previous_digest != digest
                    conn.execute(
                        """
                        INSERT INTO snapshots(digest, payload_json, loaded_wall_ms)
                        VALUES (?, ?, ?)
                        ON CONFLICT(digest) DO NOTHING
                        """,
                        (
                            digest,
                            canonical_json(bundle.to_dict()),
                            wall_ms(),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO active_snapshot(id, digest) VALUES(1, ?)
                        ON CONFLICT(id) DO UPDATE SET digest = excluded.digest
                        """,
                        (digest,),
                    )
                    for cluster_id in sorted(bundle.inventory):
                        conn.execute(
                            """
                            INSERT INTO ledger(
                                cluster_id, replicas_used, resources_used,
                                replicas_capacity, resources_capacity
                            ) VALUES (?, 0, 0, ?, ?)
                            ON CONFLICT(cluster_id) DO UPDATE SET
                                replicas_capacity = excluded.replicas_capacity,
                                resources_capacity = excluded.resources_capacity
                            """,
                            (
                                cluster_id,
                                _capacity_for(bundle, cluster_id, "replicas"),
                                _capacity_for(bundle, cluster_id, "resources"),
                            ),
                        )
                    if clock_reset:
                        conn.execute(
                            """
                            INSERT INTO clock_state(id, now_ms, cursor)
                            VALUES(1, ?, 0)
                            ON CONFLICT(id) DO UPDATE SET
                                now_ms = MAX(clock_state.now_ms, excluded.now_ms),
                                cursor = 0
                            """,
                            (bundle.clock.start_ms,),
                        )
                    self.audit(
                        conn,
                        logical_ms=self.clock_now_conn(conn),
                        event="snapshot_loaded",
                        detail={
                            "snapshot_digest": digest,
                            "clock_reset": clock_reset,
                        },
                    )
            return {
                "snapshot_digest": digest,
                "clusters": len(bundle.inventory),
                "applications": len(bundle.apps),
                "regions": len({item.region for item in bundle.inventory.values()}),
                "clock_reset": clock_reset,
                "logical_clock_ms": self.get_clock()[0],
            }
        finally:
            conn.close()

    def clock_now_conn(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT now_ms FROM clock_state WHERE id = 1"
        ).fetchone()
        if row is None:
            raise StateError(E_NOT_LOADED, "logical clock has not been loaded")
        return int(row["now_ms"])

    def get_clock(self) -> tuple[int, int]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT now_ms, cursor FROM clock_state WHERE id = 1"
            ).fetchone()
            if row is None:
                raise StateError(
                    E_NOT_LOADED, "logical clock has not been loaded"
                )
            return int(row["now_ms"]), int(row["cursor"])
        finally:
            conn.close()

    def get_active_snapshot_digest(self) -> str | None:
        if not self.exists:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT digest FROM active_snapshot WHERE id = 1"
            ).fetchone()
            return row["digest"] if row else None
        finally:
            conn.close()

    def get_active_bundle(self) -> Bundle:
        if not self.exists:
            raise StateError(
                E_NOT_LOADED,
                "state database does not exist; run load first",
                db=str(self.db_path),
            )
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT s.payload_json
                FROM active_snapshot a
                JOIN snapshots s ON s.digest = a.digest
                WHERE a.id = 1
                """
            ).fetchone()
            if row is None:
                raise StateError(
                    E_NOT_LOADED, "no active snapshot; run load first"
                )
            return Bundle.from_dict(json.loads(row["payload_json"]))
        finally:
            conn.close()

    def get_consumed_approvals(self) -> frozenset[str]:
        if not self.exists:
            return frozenset()
        conn = self._connect()
        try:
            rows = conn.execute("SELECT approval_id FROM approval_uses").fetchall()
            return frozenset(row["approval_id"] for row in rows)
        finally:
            conn.close()

    def find_version_by_request(
        self, request_digest: str
    ) -> dict[str, Any] | None:
        if not self.exists:
            return None
        conn = self._connect()
        try:
            row = self._version_by_request(conn, request_digest)
            if row is None:
                return None
            return json.loads(row["payload_json"])
        finally:
            conn.close()

    def get_bundle_for_snapshot(self, snapshot_digest: str) -> Bundle:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload_json FROM snapshots WHERE digest = ?",
                (snapshot_digest,),
            ).fetchone()
            if row is None:
                raise StateError(
                    E_NOT_LOADED,
                    "snapshot referenced by version is not stored",
                    snapshot_digest=snapshot_digest,
                )
            return Bundle.from_dict(json.loads(row["payload_json"]))
        finally:
            conn.close()

    def set_clock_watermark(
        self,
        *,
        now_ms: int,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn = self._connect()
        try:
            with self.transaction(conn):
                row = conn.execute(
                    "SELECT now_ms, cursor FROM clock_state WHERE id = 1"
                ).fetchone()
                if row is None:
                    raise StateError(
                        E_NOT_LOADED, "logical clock has not been loaded"
                    )
                old_now = int(row["now_ms"])
                if now_ms < old_now:
                    raise StateError(
                        E_VERSION_ROLLBACK,
                        "logical clock cannot move backwards",
                        current_ms=old_now,
                        requested_ms=now_ms,
                    )
                conn.execute(
                    "UPDATE clock_state SET now_ms = ? WHERE id = 1",
                    (now_ms,),
                )
                self.audit(
                    conn,
                    logical_ms=now_ms,
                    event=reason,
                    detail={
                        "from_ms": old_now,
                        "to_ms": now_ms,
                        **(detail or {}),
                    },
                )
        finally:
            conn.close()

    def _version_by_request(
        self, conn: sqlite3.Connection, request_digest: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM config_versions WHERE request_digest = ?",
            (request_digest,),
        ).fetchone()

    def register_version(
        self,
        *,
        bundle: Bundle,
        plan: dict[str, Any],
        request_digest: str,
        plan_digest: str,
        requested_version: int | None = None,
        abandoned: list[str] | None = None,
    ) -> dict[str, Any]:
        abandoned_set = set(abandoned or [])
        conn = self._connect(create=True)
        try:
            with self.global_lock():
                self._initialize_schema(conn)
                with self.transaction(conn):
                    existing = self._version_by_request(conn, request_digest)
                    if existing is not None:
                        return {
                            "version": int(existing["version"]),
                            "existing": True,
                            "plan": json.loads(existing["payload_json"]),
                        }

                    active_row = conn.execute(
                        "SELECT version FROM active_version WHERE id = 1"
                    ).fetchone()
                    active_version = (
                        int(active_row["version"])
                        if active_row is not None
                        else 0
                    )
                    max_row = conn.execute(
                        "SELECT COALESCE(MAX(version), 0) AS v FROM config_versions"
                    ).fetchone()
                    max_version = int(max_row["v"])
                    if requested_version is not None:
                        if type(requested_version) is not int or requested_version <= 0:
                            raise EngineError(
                                "E_LIMIT",
                                "requested version must be a positive integer",
                                details={"version": requested_version},
                            )
                        if requested_version <= active_version:
                            raise StateError(
                                E_VERSION_ROLLBACK,
                                "version rollback is forbidden",
                                requested_version=requested_version,
                                active_version=active_version,
                            )
                        if requested_version <= max_version and requested_version != active_version:
                            raise StateError(
                                E_VERSION_CONFLICT,
                                "requested version already exists",
                                requested_version=requested_version,
                                max_version=max_version,
                            )
                        version = requested_version
                    else:
                        version = max_version + 1

                    conn.execute(
                        "DROP TABLE IF EXISTS temp._version_stage"
                    )
                    conn.execute(
                        """
                        CREATE TEMP TABLE _version_stage (
                            cluster_id TEXT PRIMARY KEY,
                            decision_status TEXT NOT NULL,
                            effective_json TEXT NOT NULL,
                            candidate_json TEXT NOT NULL,
                            decision_json TEXT NOT NULL
                        )
                        """
                    )
                    for cluster_id, decision in sorted(plan["clusters"].items()):
                        conn.execute(
                            """
                            INSERT INTO _version_stage(
                                cluster_id, decision_status, effective_json,
                                candidate_json, decision_json
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                cluster_id,
                                decision["status"],
                                canonical_json(decision["effective_config"]),
                                canonical_json(decision["candidate_config"]),
                                canonical_json(decision),
                            ),
                        )
                    staged = conn.execute(
                        "SELECT COUNT(*) AS n FROM _version_stage"
                    ).fetchone()["n"]
                    if int(staged) != len(plan["clusters"]):
                        raise StateError(
                            E_STATE_CONFLICT,
                            "version staging count does not match plan",
                            staged=int(staged),
                            expected=len(plan["clusters"]),
                        )
                    stage_digest = sha256_json(
                        [
                            dict(row)
                            for row in conn.execute(
                                """
                                SELECT cluster_id, decision_status,
                                       effective_json, candidate_json
                                FROM _version_stage ORDER BY cluster_id
                                """
                            ).fetchall()
                        ]
                    )

                    now_ms = self.clock_now_conn(conn)
                    payload = {
                        "version": version,
                        "request_digest": request_digest,
                        "plan_digest": plan_digest,
                        "stage_digest": stage_digest,
                        "snapshot_digest": bundle.digest(),
                        "created_logical_ms": now_ms,
                        "abandoned": sorted(abandoned_set),
                        "plan": plan,
                    }
                    conn.execute(
                        """
                        INSERT INTO config_versions(
                            version, request_digest, plan_digest,
                            snapshot_digest, payload_json,
                            created_wall_ms, created_logical_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            version,
                            request_digest,
                            plan_digest,
                            bundle.digest(),
                            canonical_json(payload),
                            wall_ms(),
                            now_ms,
                        ),
                    )
                    for cluster_id, decision in sorted(plan["clusters"].items()):
                        if decision["status"] == "rejected":
                            status = "rejected"
                            reason_code = decision["rejection_code"]
                            reason = (
                                decision["first_violation"].get(
                                    "message",
                                    "configuration rejected by admission rules",
                                )
                                if decision["first_violation"]
                                else "configuration rejected"
                            )
                            detail = {
                                "first_violation": decision["first_violation"],
                                "violations": decision["violations"],
                            }
                        elif cluster_id in abandoned_set:
                            status = "abandoned"
                            reason_code = "OPERATOR_ABANDONED"
                            reason = "operator explicitly abandoned this cluster"
                            detail = {"abandoned": True}
                        else:
                            status = "queued"
                            reason_code = None
                            reason = None
                            detail = {}
                        conn.execute(
                            """
                            INSERT INTO dispatches(
                                version, cluster_id, status, reason_code,
                                reason, detail_json, updated_wall_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                version,
                                cluster_id,
                                status,
                                reason_code,
                                reason,
                                canonical_json(detail),
                                wall_ms(),
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO dispatch_events(
                                version, cluster_id, status, logical_ms,
                                detail_json
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                version,
                                cluster_id,
                                status,
                                now_ms,
                                canonical_json(detail),
                            ),
                        )

                    # The pointer is never patched key-by-key: it is removed and
                    # recreated as one row from the fully staged version.
                    conn.execute("DELETE FROM active_version")
                    conn.execute(
                        """
                        INSERT INTO active_version(id, version, request_digest)
                        VALUES(1, ?, ?)
                        """,
                        (version, request_digest),
                    )
                    self.audit(
                        conn,
                        logical_ms=now_ms,
                        event="version_committed",
                        version=version,
                        detail={
                            "request_digest": request_digest,
                            "plan_digest": plan_digest,
                            "stage_digest": stage_digest,
                        },
                    )
                    _crash("after_version_stage")
                _crash("after_version_commit")
                return {
                    "version": version,
                    "existing": False,
                    "plan": payload,
                }
        finally:
            conn.close()

    def get_plan_for_version(self, version: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload_json FROM config_versions WHERE version = ?",
                (version,),
            ).fetchone()
            if row is None:
                raise StateError(
                    E_NO_ACTIVE_VERSION,
                    "configuration version does not exist",
                    version=version,
                )
            return json.loads(row["payload_json"])
        finally:
            conn.close()

    def get_active_version(self) -> dict[str, Any] | None:
        if not self.exists:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT v.* FROM active_version a
                JOIN config_versions v ON v.version = a.version
                WHERE a.id = 1
                """
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["payload_json"])
        finally:
            conn.close()

    def get_dispatch_states(self, version: int) -> dict[str, dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM dispatches WHERE version = ? ORDER BY cluster_id",
                (version,),
            ).fetchall()
            result: dict[str, dict[str, Any]] = {}
            for row in rows:
                result[row["cluster_id"]] = {
                    "cluster": row["cluster_id"],
                    "version": int(row["version"]),
                    "status": row["status"],
                    "round_id": row["round_id"],
                    "slot": row["slot"],
                    "started_logical_ms": row["started_logical_ms"],
                    "finished_logical_ms": row["finished_logical_ms"],
                    "reason_code": row["reason_code"],
                    "reason": row["reason"],
                    "detail": json.loads(row["detail_json"]),
                }
            return result
        finally:
            conn.close()

    def get_rounds(self, version: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM rounds WHERE version = ? ORDER BY round_no",
                (version,),
            ).fetchall()
            return [
                {
                    "round_id": row["round_id"],
                    "round_no": int(row["round_no"]),
                    "status": row["status"],
                    "max_parallel": int(row["max_parallel"]),
                    "budget_ms": int(row["budget_ms"]),
                    "earliest_ms": row["earliest_ms"],
                    "started_logical_ms": row["started_logical_ms"],
                    "finished_logical_ms": row["finished_logical_ms"],
                    "reason_code": row["reason_code"],
                    "detail": json.loads(row["detail_json"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def create_round(
        self,
        *,
        version: int,
        round_no: int,
        status: str,
        max_parallel: int,
        budget_ms: int,
        earliest_ms: int | None = None,
        now_ms: int | None = None,
        reason_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        round_id = f"v{version}-r{round_no}"
        conn = self._connect()
        try:
            with self.global_lock():
                with self.transaction(conn):
                    now = now_ms if now_ms is not None else self.clock_now_conn(conn)
                    conn.execute(
                        """
                        INSERT INTO rounds(
                            round_id, version, round_no, status, max_parallel,
                            budget_ms, earliest_ms, started_logical_ms,
                            reason_code, detail_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            round_id,
                            version,
                            round_no,
                            status,
                            max_parallel,
                            budget_ms,
                            earliest_ms,
                            now,
                            reason_code,
                            canonical_json(detail or {}),
                        ),
                    )
                    self.audit(
                        conn,
                        logical_ms=now,
                        event="round_created",
                        version=version,
                        detail={
                            "round_id": round_id,
                            "status": status,
                            "max_parallel": max_parallel,
                            "budget_ms": budget_ms,
                            "earliest_ms": earliest_ms,
                            "reason_code": reason_code,
                        },
                    )
        finally:
            conn.close()
        return round_id

    def finish_round(
        self,
        *,
        round_id: str,
        status: str,
        reason_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn = self._connect()
        try:
            with self.transaction(conn):
                now = self.clock_now_conn(conn)
                row = conn.execute(
                    "SELECT version FROM rounds WHERE round_id = ?",
                    (round_id,),
                ).fetchone()
                if row is None:
                    raise StateError(
                        E_STATE_CONFLICT,
                        "round does not exist",
                        round_id=round_id,
                    )
                conn.execute(
                    """
                    UPDATE rounds SET status = ?, finished_logical_ms = ?,
                        reason_code = ?, detail_json = ?
                    WHERE round_id = ?
                    """,
                    (
                        status,
                        now,
                        reason_code,
                        canonical_json(detail or {}),
                        round_id,
                    ),
                )
                self.audit(
                    conn,
                    logical_ms=now,
                    event="round_finished",
                    version=int(row["version"]),
                    detail={
                        "round_id": round_id,
                        "status": status,
                        "reason_code": reason_code,
                    },
                )
        finally:
            conn.close()

    def queue_outside_window(
        self,
        *,
        version: int,
        earliest_ms: int | None,
        reason_code: str,
        now_ms: int,
    ) -> None:
        conn = self._connect()
        try:
            with self.transaction(conn):
                rows = conn.execute(
                    """
                    SELECT cluster_id FROM dispatches
                    WHERE version = ? AND status IN ('queued', 'not_started_budget')
                    ORDER BY cluster_id
                    """,
                    (version,),
                ).fetchall()
                detail = {
                    "earliest_ms": earliest_ms,
                    "reason_code": reason_code,
                    "logical_now_ms": now_ms,
                }
                for row in rows:
                    conn.execute(
                        """
                        UPDATE dispatches SET status = 'queued',
                            reason_code = ?, reason = ?, detail_json = ?,
                            updated_wall_ms = ?
                        WHERE version = ? AND cluster_id = ?
                        """,
                        (
                            reason_code,
                            "outside change window; queued for earliest logical time",
                            canonical_json(detail),
                            wall_ms(),
                            version,
                            row["cluster_id"],
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO dispatch_events(
                            version, cluster_id, status, logical_ms, detail_json
                        ) VALUES (?, ?, 'queued_window', ?, ?)
                        """,
                        (
                            version,
                            row["cluster_id"],
                            now_ms,
                            canonical_json(detail),
                        ),
                    )
                self.audit(
                    conn,
                    logical_ms=now_ms,
                    event="dispatch_queued_window",
                    version=version,
                    detail=detail,
                )
        finally:
            conn.close()

    def reopen_for_resume(self, version: int, *, now_ms: int) -> list[str]:
        conn = self._connect()
        try:
            with self.transaction(conn):
                rows = conn.execute(
                    """
                    SELECT cluster_id FROM dispatches
                    WHERE version = ? AND status IN ('not_started_budget')
                    ORDER BY cluster_id
                    """,
                    (version,),
                ).fetchall()
                reopened: list[str] = []
                for row in rows:
                    cluster_id = row["cluster_id"]
                    conn.execute(
                        """
                        UPDATE dispatches SET status = 'queued',
                            reason_code = NULL, reason = NULL,
                            detail_json = '{}', updated_wall_ms = ?
                        WHERE version = ? AND cluster_id = ?
                        """,
                        (wall_ms(), version, cluster_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO dispatch_events(
                            version, cluster_id, status, logical_ms, detail_json
                        ) VALUES (?, ?, 'resume_requeued', ?, '{}')
                        """,
                        (version, cluster_id, now_ms),
                    )
                    reopened.append(cluster_id)
                return reopened
        finally:
            conn.close()

    def peek_dispatch_cost(
        self, script: ClockScript
    ) -> tuple[int, int, int, int]:
        now_ms, cursor = self.get_clock()
        return self.peek_cost_from(now_ms, cursor, script)

    @staticmethod
    def peek_cost_from(
        now_ms: int, cursor: int, script: ClockScript
    ) -> tuple[int, int, int, int]:
        if cursor + 1 >= len(script.steps):
            raise StateError(
                "E_CLOCK",
                "logical clock step table exhausted",
                cursor=cursor,
                step_count=len(script.steps),
            )
        start_step = script.steps[cursor]
        finish_step = script.steps[cursor + 1]
        if (
            start_step.event != "dispatch_start"
            or finish_step.event != "dispatch_finish"
        ):
            raise StateError(
                "E_CLOCK",
                "logical clock cursor is not at a start/finish pair",
                cursor=cursor,
                events=[start_step.event, finish_step.event],
            )
        start_ms = now_ms + start_step.advance_ms
        finish_ms = start_ms + finish_step.advance_ms
        return finish_ms - now_ms, start_ms, finish_ms, cursor + 2

    def _advance_clock_event(
        self,
        conn: sqlite3.Connection,
        script: ClockScript,
        event: str,
    ) -> tuple[int, int]:
        row = conn.execute(
            "SELECT now_ms, cursor FROM clock_state WHERE id = 1"
        ).fetchone()
        if row is None:
            raise StateError(E_NOT_LOADED, "logical clock has not been loaded")
        now_ms = int(row["now_ms"])
        cursor = int(row["cursor"])
        if cursor >= len(script.steps):
            raise StateError(
                "E_CLOCK",
                "logical clock step table exhausted",
                cursor=cursor,
                step_count=len(script.steps),
            )
        step = script.steps[cursor]
        if step.event != event:
            raise StateError(
                "E_CLOCK",
                "logical clock step event does not match requested event",
                cursor=cursor,
                expected=step.event,
                requested=event,
            )
        now_ms += step.advance_ms
        cursor += 1
        conn.execute(
            "UPDATE clock_state SET now_ms = ?, cursor = ? WHERE id = 1",
            (now_ms, cursor),
        )
        return now_ms, cursor

    def mark_started(
        self,
        *,
        version: int,
        cluster_id: str,
        script: ClockScript,
        round_id: str,
        slot: int,
    ) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            with self.cluster_lock(cluster_id):
                with self.transaction(conn):
                    row = conn.execute(
                        """
                        SELECT status FROM dispatches
                        WHERE version = ? AND cluster_id = ?
                        """,
                        (version, cluster_id),
                    ).fetchone()
                    if row is None:
                        raise StateError(
                            E_STATE_CONFLICT,
                            "dispatch row does not exist",
                            version=version,
                            cluster=cluster_id,
                        )
                    if row["status"] not in {"queued", "not_started_budget"}:
                        return None
                    state_row = conn.execute(
                        "SELECT version FROM cluster_state WHERE cluster_id = ?",
                        (cluster_id,),
                    ).fetchone()
                    if (
                        state_row is not None
                        and int(state_row["version"]) >= version
                    ):
                        raise StateError(
                            E_VERSION_ROLLBACK,
                            "cluster already has the same or a newer version",
                            cluster=cluster_id,
                            current_version=int(state_row["version"]),
                            requested_version=version,
                        )
                    now_ms, _ = self._advance_clock_event(
                        conn, script, "dispatch_start"
                    )
                    conn.execute(
                        """
                        UPDATE dispatches SET status = 'started',
                            round_id = ?, slot = ?, started_logical_ms = ?,
                            reason_code = NULL, reason = NULL,
                            detail_json = '{}', updated_wall_ms = ?
                        WHERE version = ? AND cluster_id = ?
                        """,
                        (
                            round_id,
                            slot,
                            now_ms,
                            wall_ms(),
                            version,
                            cluster_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO dispatch_events(
                            version, cluster_id, round_id, status,
                            logical_ms, detail_json
                        ) VALUES (?, ?, ?, 'started', ?, '{}')
                        """,
                        (version, cluster_id, round_id, now_ms),
                    )
                    self.audit(
                        conn,
                        logical_ms=now_ms,
                        event="dispatch_started",
                        version=version,
                        cluster_id=cluster_id,
                        detail={"round_id": round_id, "slot": slot},
                    )
                _crash("after_dispatch_start")
            return {
                "started_logical_ms": now_ms,
                "round_id": round_id,
                "slot": slot,
            }
        finally:
            conn.close()

    def _insert_approval_uses(
        self,
        conn: sqlite3.Connection,
        *,
        version: int,
        cluster_id: str,
        decision: dict[str, Any],
        now_ms: int,
    ) -> None:
        for key_record in decision["keys"]:
            approval = key_record["policy"].get("approval")
            if not approval or approval.get("status") != "approved":
                continue
            approval_id = approval["approval_id"]
            if now_ms >= int(approval["expires_ms"]):
                raise EngineError(
                    E_APPROVAL_EXPIRED,
                    "approval expired before the cluster commit",
                    details={
                        "approval": approval_id,
                        "key": key_record["key"],
                        "expires_ms": approval["expires_ms"],
                        "logical_ms": now_ms,
                    },
                )
            existing = conn.execute(
                "SELECT version, cluster_id FROM approval_uses WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if existing is not None:
                raise EngineError(
                    E_APPROVAL_ALREADY_CONSUMED,
                    "approval record has already been consumed",
                    details={
                        "approval": approval_id,
                        "consumed_by_version": int(existing["version"]),
                        "consumed_by_cluster": existing["cluster_id"],
                    },
                )
            conn.execute(
                """
                INSERT INTO approval_uses(
                    approval_id, version, cluster_id, key_path,
                    consumed_logical_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (approval_id, version, cluster_id, key_record["key"], now_ms),
            )
        _crash("after_approval_consume")

    def commit_cluster(
        self,
        *,
        version: int,
        cluster_id: str,
        decision: dict[str, Any],
        script: ClockScript,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with self.cluster_lock(cluster_id):
                with self.transaction(conn):
                    existing = conn.execute(
                        """
                        SELECT * FROM cluster_commits
                        WHERE cluster_id = ? AND version = ?
                        """,
                        (cluster_id, version),
                    ).fetchone()
                    if existing is not None:
                        return {
                            "applied": True,
                            "idempotent": True,
                            "replicas_total": int(existing["replicas_total"]),
                            "resources_total": int(existing["resources_total"]),
                        }

                    dispatch_row = conn.execute(
                        """
                        SELECT status FROM dispatches
                        WHERE version = ? AND cluster_id = ?
                        """,
                        (version, cluster_id),
                    ).fetchone()
                    if dispatch_row is None or dispatch_row["status"] != "started":
                        raise StateError(
                            E_STATE_CONFLICT,
                            "cluster commit requires a started dispatch",
                            version=version,
                            cluster=cluster_id,
                            dispatch_status=(
                                dispatch_row["status"]
                                if dispatch_row is not None
                                else None
                            ),
                        )

                    now_ms = self.clock_now_conn(conn)
                    self._insert_approval_uses(
                        conn,
                        version=version,
                        cluster_id=cluster_id,
                        decision=decision,
                        now_ms=now_ms,
                    )
                    replicas_total, resources_total = _totals(
                        decision["effective_config"]
                    )
                    ledger = conn.execute(
                        "SELECT * FROM ledger WHERE cluster_id = ?",
                        (cluster_id,),
                    ).fetchone()
                    if ledger is None:
                        raise StateError(
                            E_STATE_CONFLICT,
                            "cluster ledger does not exist",
                            cluster=cluster_id,
                        )
                    replicas_capacity = int(ledger["replicas_capacity"])
                    resources_capacity = int(ledger["resources_capacity"])
                    if replicas_total > replicas_capacity:
                        raise EngineError(
                            E_QUOTA_REPLICAS,
                            "replica quota would be exceeded at commit",
                            details={
                                "cluster": cluster_id,
                                "requested": replicas_total,
                                "capacity": replicas_capacity,
                                "remaining": replicas_capacity
                                - int(ledger["replicas_used"]),
                            },
                        )
                    if resources_total > resources_capacity:
                        raise EngineError(
                            E_QUOTA_RESOURCES,
                            "resource quota would be exceeded at commit",
                            details={
                                "cluster": cluster_id,
                                "requested": resources_total,
                                "capacity": resources_capacity,
                                "remaining": resources_capacity
                                - int(ledger["resources_used"]),
                            },
                        )
                    # A cluster owns one active configuration at a time. The
                    # old reservation is released and the full new reservation
                    # is acquired in this same transaction.
                    previous_replicas = int(ledger["replicas_used"])
                    previous_resources = int(ledger["resources_used"])
                    conn.execute(
                        """
                        UPDATE ledger SET
                            replicas_used = ?, resources_used = ?,
                            version = ?, updated_logical_ms = ?
                        WHERE cluster_id = ?
                        """,
                        (
                            replicas_total,
                            resources_total,
                            version,
                            now_ms,
                            cluster_id,
                        ),
                    )
                    _crash("after_ledger_update")
                    config_json = canonical_json(decision["effective_config"])
                    effective_hash = sha256_json(decision["effective_config"])
                    conn.execute(
                        """
                        INSERT INTO cluster_commits(
                            cluster_id, version, effective_hash, config_json,
                            replicas_total, resources_total,
                            committed_logical_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cluster_id,
                            version,
                            effective_hash,
                            config_json,
                            replicas_total,
                            resources_total,
                            now_ms,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO cluster_state(
                            cluster_id, version, config_json,
                            readback_json, updated_logical_ms
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(cluster_id) DO UPDATE SET
                            version = excluded.version,
                            config_json = excluded.config_json,
                            readback_json = excluded.readback_json,
                            updated_logical_ms = excluded.updated_logical_ms
                        """,
                        (
                            cluster_id,
                            version,
                            config_json,
                            config_json,
                            now_ms,
                        ),
                    )
                    finish_ms, _ = self._advance_clock_event(
                        conn, script, "dispatch_finish"
                    )
                    conn.execute(
                        """
                        UPDATE dispatches SET status = 'applied',
                            finished_logical_ms = ?, reason_code = NULL,
                            reason = NULL, detail_json = ?,
                            updated_wall_ms = ?
                        WHERE version = ? AND cluster_id = ?
                        """,
                        (
                            finish_ms,
                            canonical_json(
                                {
                                    "replicas_total": replicas_total,
                                    "resources_total": resources_total,
                                    "effective_hash": effective_hash,
                                }
                            ),
                            wall_ms(),
                            version,
                            cluster_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO dispatch_events(
                            version, cluster_id, status, logical_ms,
                            detail_json
                        ) VALUES (?, ?, 'applied', ?, ?)
                        """,
                        (
                            version,
                            cluster_id,
                            finish_ms,
                            canonical_json(
                                {
                                    "replicas_total": replicas_total,
                                    "resources_total": resources_total,
                                }
                            ),
                        ),
                    )
                    self.audit(
                        conn,
                        logical_ms=finish_ms,
                        event="cluster_config_committed",
                        version=version,
                        cluster_id=cluster_id,
                        detail={
                            "replicas_total": replicas_total,
                            "resources_total": resources_total,
                            "previous_replicas": previous_replicas,
                            "previous_resources": previous_resources,
                            "effective_hash": effective_hash,
                        },
                    )
                _crash("after_cluster_commit")
                return {
                    "applied": True,
                    "idempotent": False,
                    "replicas_total": replicas_total,
                    "resources_total": resources_total,
                    "committed_logical_ms": finish_ms,
                }
        finally:
            conn.close()

    def mark_failed_after_start(
        self,
        *,
        version: int,
        cluster_id: str,
        reason_code: str,
        reason: str,
        detail: dict[str, Any] | None = None,
        script: ClockScript,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with self.cluster_lock(cluster_id):
                with self.transaction(conn):
                    row = conn.execute(
                        """
                        SELECT status FROM dispatches
                        WHERE version = ? AND cluster_id = ?
                        """,
                        (version, cluster_id),
                    ).fetchone()
                    if row is None:
                        raise StateError(
                            E_STATE_CONFLICT,
                            "dispatch row does not exist",
                            version=version,
                            cluster=cluster_id,
                        )
                    if row["status"] in {"failed_after_start", "applied"}:
                        return {"status": row["status"], "idempotent": True}
                    if row["status"] != "started":
                        raise StateError(
                            E_STATE_CONFLICT,
                            "only a started dispatch can fail after start",
                            cluster=cluster_id,
                            status=row["status"],
                        )
                    finish_ms, _ = self._advance_clock_event(
                        conn, script, "dispatch_finish"
                    )
                    detail_json = canonical_json(detail or {})
                    conn.execute(
                        """
                        UPDATE dispatches SET status = 'failed_after_start',
                            finished_logical_ms = ?, reason_code = ?,
                            reason = ?, detail_json = ?, updated_wall_ms = ?
                        WHERE version = ? AND cluster_id = ?
                        """,
                        (
                            finish_ms,
                            reason_code,
                            reason,
                            detail_json,
                            wall_ms(),
                            version,
                            cluster_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO dispatch_events(
                            version, cluster_id, status, logical_ms, detail_json
                        ) VALUES (?, ?, 'failed_after_start', ?, ?)
                        """,
                        (version, cluster_id, finish_ms, detail_json),
                    )
                    self.audit(
                        conn,
                        logical_ms=finish_ms,
                        event="dispatch_failed_after_start",
                        version=version,
                        cluster_id=cluster_id,
                        detail={
                            "reason_code": reason_code,
                            "reason": reason,
                            **(detail or {}),
                        },
                    )
                    return {
                        "status": "failed_after_start",
                        "reason_code": reason_code,
                        "finished_logical_ms": finish_ms,
                    }
        finally:
            conn.close()

    def mark_not_started_budget(
        self,
        *,
        version: int,
        cluster_ids: list[str],
        round_id: str,
        reason_code: str,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        if not cluster_ids:
            return
        conn = self._connect()
        try:
            with self.transaction(conn):
                now = self.clock_now_conn(conn)
                detail_json = canonical_json(detail)
                for cluster_id in cluster_ids:
                    conn.execute(
                        """
                        UPDATE dispatches SET status = 'not_started_budget',
                            round_id = ?, reason_code = ?, reason = ?,
                            detail_json = ?, updated_wall_ms = ?
                        WHERE version = ? AND cluster_id = ?
                          AND status IN ('queued', 'not_started_budget')
                        """,
                        (
                            round_id,
                            reason_code,
                            reason,
                            detail_json,
                            wall_ms(),
                            version,
                            cluster_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO dispatch_events(
                            version, cluster_id, round_id, status,
                            logical_ms, detail_json
                        ) VALUES (?, ?, ?, 'not_started_budget', ?, ?)
                        """,
                        (
                            version,
                            cluster_id,
                            round_id,
                            now,
                            detail_json,
                        ),
                    )
                self.audit(
                    conn,
                    logical_ms=now,
                    event="round_budget_exhausted",
                    version=version,
                    detail={
                        "round_id": round_id,
                        "reason_code": reason_code,
                        "clusters": sorted(cluster_ids),
                        **detail,
                    },
                )
        finally:
            conn.close()

    def mark_rejected_at_dispatch(
        self,
        *,
        version: int,
        cluster_id: str,
        reason_code: str,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        conn = self._connect()
        try:
            with self.cluster_lock(cluster_id):
                with self.transaction(conn):
                    now = self.clock_now_conn(conn)
                    conn.execute(
                        """
                        UPDATE dispatches SET status = 'rejected',
                            reason_code = ?, reason = ?, detail_json = ?,
                            updated_wall_ms = ?
                        WHERE version = ? AND cluster_id = ?
                          AND status IN ('queued', 'not_started_budget')
                        """,
                        (
                            reason_code,
                            reason,
                            canonical_json(detail),
                            wall_ms(),
                            version,
                            cluster_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO dispatch_events(
                            version, cluster_id, status, logical_ms,
                            detail_json
                        ) VALUES (?, ?, 'rejected', ?, ?)
                        """,
                        (version, cluster_id, now, canonical_json(detail)),
                    )
        finally:
            conn.close()

    def status_snapshot(self) -> dict[str, Any]:
        if not self.exists:
            return {
                "active_version": None,
                "active_snapshot_digest": None,
                "logical_clock_ms": None,
                "clusters": [],
            }
        conn = self._connect()
        try:
            clock = conn.execute(
                "SELECT now_ms, cursor FROM clock_state WHERE id = 1"
            ).fetchone()
            active_version = conn.execute(
                "SELECT version FROM active_version WHERE id = 1"
            ).fetchone()
            snapshot = conn.execute(
                "SELECT digest FROM active_snapshot WHERE id = 1"
            ).fetchone()
            bundle: Bundle | None = None
            if snapshot is not None:
                payload = conn.execute(
                    "SELECT payload_json FROM snapshots WHERE digest = ?",
                    (snapshot["digest"],),
                ).fetchone()
                if payload is not None:
                    bundle = Bundle.from_dict(json.loads(payload["payload_json"]))
            version_int = (
                int(active_version["version"])
                if active_version is not None
                else None
            )
            plan = None
            version_snapshot_digest: str | None = None
            dispatch_states: dict[str, dict[str, Any]] = {}
            if version_int is not None:
                version_row = conn.execute(
                    "SELECT payload_json FROM config_versions WHERE version = ?",
                    (version_int,),
                ).fetchone()
                if version_row is not None:
                    version_payload = json.loads(version_row["payload_json"])
                    plan = version_payload["plan"]
                    version_snapshot_digest = version_payload["snapshot_digest"]
                    v_bundle = Bundle.from_dict(
                        json.loads(
                            conn.execute(
                                "SELECT payload_json FROM snapshots WHERE digest = ?",
                                (version_snapshot_digest,),
                            ).fetchone()["payload_json"]
                        )
                    )
                    bundle = v_bundle
                    dispatch_states = self.get_dispatch_states(version_int)
            clusters = []
            if bundle is not None:
                ledger_rows = {
                    row["cluster_id"]: row
                    for row in conn.execute(
                        "SELECT * FROM ledger ORDER BY cluster_id"
                    ).fetchall()
                }
                state_rows = {
                    row["cluster_id"]: row
                    for row in conn.execute(
                        "SELECT * FROM cluster_state ORDER BY cluster_id"
                    ).fetchall()
                }
                for cluster_id in sorted(bundle.inventory):
                    member = bundle.inventory[cluster_id]
                    ledger = ledger_rows.get(cluster_id)
                    state = state_rows.get(cluster_id)
                    decision = (
                        plan["clusters"].get(cluster_id) if plan else None
                    )
                    dispatch = dispatch_states.get(cluster_id)
                    clusters.append(
                        {
                            "cluster": cluster_id,
                            "region": member.region,
                            "healthy": member.healthy,
                            "current_version": (
                                int(state["version"]) if state else None
                            ),
                            "readback_version": (
                                int(state["version"]) if state else None
                            ),
                            "replicas": {
                                "used": int(ledger["replicas_used"])
                                if ledger
                                else 0,
                                "capacity": int(ledger["replicas_capacity"])
                                if ledger
                                else 0,
                                "remaining": (
                                    int(ledger["replicas_capacity"])
                                    - int(ledger["replicas_used"])
                                )
                                if ledger
                                else 0,
                            },
                            "resources": {
                                "used": int(ledger["resources_used"])
                                if ledger
                                else 0,
                                "capacity": int(ledger["resources_capacity"])
                                if ledger
                                else 0,
                                "remaining": (
                                    int(ledger["resources_capacity"])
                                    - int(ledger["resources_used"])
                                )
                                if ledger
                                else 0,
                            },
                            "conflict_count": (
                                int(decision["conflict_count"])
                                if decision
                                else 0
                            ),
                            "decision_status": (
                                decision["status"] if decision else None
                            ),
                            "last_dispatch": (
                                {
                                    "version": dispatch["version"],
                                    "status": dispatch["status"],
                                    "reason_code": dispatch["reason_code"],
                                    "reason": dispatch["reason"],
                                    "round_id": dispatch["round_id"],
                                    "started_logical_ms": dispatch[
                                        "started_logical_ms"
                                    ],
                                    "finished_logical_ms": dispatch[
                                        "finished_logical_ms"
                                    ],
                                    "detail": dispatch["detail"],
                                }
                                if dispatch
                                else None
                            ),
                        }
                    )
            return {
                "active_version": version_int,
                "active_snapshot_digest": (
                    version_snapshot_digest
                    if version_snapshot_digest is not None
                    else (snapshot["digest"] if snapshot is not None else None)
                ),
                "loaded_snapshot_digest": (
                    snapshot["digest"] if snapshot is not None else None
                ),
                "version_snapshot_digest": version_snapshot_digest,
                "logical_clock_ms": (
                    int(clock["now_ms"]) if clock is not None else None
                ),
                "logical_clock_cursor": (
                    int(clock["cursor"]) if clock is not None else None
                ),
                "clusters": clusters,
                "summary": {
                    "clusters": len(clusters),
                    "conflicts": sum(
                        item["conflict_count"] for item in clusters
                    ),
                    "applied": sum(
                        1
                        for item in clusters
                        if item["last_dispatch"]
                        and item["last_dispatch"]["status"] == "applied"
                    ),
                },
            }
        finally:
            conn.close()

    def cluster_readback(self, cluster_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM cluster_state WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "version": int(row["version"]),
                "config": json.loads(row["readback_json"]),
            }
        finally:
            conn.close()

    def audit_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "seq": int(row["seq"]),
                    "logical_ms": int(row["logical_ms"]),
                    "event": row["event"],
                    "version": row["version"],
                    "cluster": row["cluster_id"],
                    "detail": json.loads(row["detail_json"]),
                }
                for row in reversed(rows)
            ]
        finally:
            conn.close()

    def debug_set_readback(
        self, cluster_id: str, config: dict[str, Any]
    ) -> None:
        conn = self._connect()
        try:
            with self.transaction(conn):
                conn.execute(
                    """
                    UPDATE cluster_state SET readback_json = ?
                    WHERE cluster_id = ?
                    """,
                    (pretty_json(config), cluster_id),
                )
        finally:
            conn.close()

