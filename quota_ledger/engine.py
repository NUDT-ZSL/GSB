from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence

from .database import (
    DELETE_ORDER,
    FileLock,
    connect,
    initialize_schema,
    process_lock,
    transaction,
)
from .errors import BusinessError, error
from .fixedpoint import exact_scaled, quantize, tolerance_micro
from .models import (
    ApplicationBundle,
    ServiceBundle,
    TenantBundle,
    TenantSpec,
    canonical_json,
    digest,
    parse_bundles,
)


REASON_TEXT = {
    "QUOTA_EXHAUSTED": "基础配额不足",
    "OVERDRAFT_DISABLED": "基础配额不足且未允许超额借调",
    "TENANT_BORROW_LIMIT": "租户借调上限不足",
    "POOL_BORROW_EXHAUSTED": "资源池可借调余量不足",
    "SERVICE_CONCURRENCY_LIMIT": "服务并发预留达到上限",
    "ROUND_BUDGET_EXHAUSTED": "本轮逻辑时间预算耗尽，申请未启动",
    "SERVICE_ROUND_BATCH_LIMIT": "服务每轮申请批次已满，申请未启动",
    "APPLICATION_EXPIRED": "申请在获准准入前已到期",
    "MANUAL_ABANDON": "人工放弃",
}


def _reason(code: str) -> str:
    return REASON_TEXT.get(code, code)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _json_load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class LedgerEngine:
    def __init__(
        self,
        db_path: str | Path,
        *,
        lock_path: str | Path | None = None,
        crashpoint: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.db_path.with_suffix(self.db_path.suffix + ".lock")
        )
        self.crashpoint = crashpoint
        self._thread_lock = process_lock(self.lock_path)

    # ------------------------------------------------------------------
    # Lifecycle and lock helpers
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        with self._exclusive():
            conn = connect(self.db_path)
            try:
                initialize_schema(conn)
            finally:
                conn.close()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._thread_lock:
            with FileLock(self.lock_path):
                yield

    def _new_conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def initialize_if_needed(self) -> None:
        if not self.db_path.exists():
            with self._exclusive():
                conn = connect(self.db_path)
                try:
                    initialize_schema(conn)
                finally:
                    conn.close()

    def _crashpoint(self, name: str) -> None:
        requested = self.crashpoint or os.environ.get("QUOTA_LEDGER_CRASH_POINT")
        if requested == name:
            print(f"CRASH_POINT:{name}", flush=True)
            os._exit(99)

    # ------------------------------------------------------------------
    # Primitive persistence helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    @staticmethod
    def _current_clock(conn: sqlite3.Connection) -> int:
        return int(LedgerEngine._meta(conn, "logical_clock_ms", "0") or 0)

    @staticmethod
    def _advance_clock(conn: sqlite3.Connection, at_ms: int) -> int:
        new_clock = max(LedgerEngine._current_clock(conn), int(at_ms))
        LedgerEngine._set_meta(conn, "logical_clock_ms", new_clock)
        return new_clock

    @staticmethod
    def _get_tenant(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM tenants WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise error("TENANT_UNKNOWN", f"未知租户: {name}")
        return row

    @staticmethod
    def _get_service(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM services WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise error("SERVICE_UNKNOWN", f"未知服务: {name}")
        return row

    @staticmethod
    def _get_application(
        conn: sqlite3.Connection, application_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM applications WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        if row is None:
            raise error(
                "APPLICATION_UNKNOWN",
                f"未知申请: {application_id}",
                details={"application_id": application_id},
            )
        return row

    @staticmethod
    def _get_reservation(
        conn: sqlite3.Connection, application_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM reservations WHERE application_id = ?",
            (application_id,),
        ).fetchone()

    @staticmethod
    def _service_active_count(conn: sqlite3.Connection, service: str) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM reservations
            WHERE service = ? AND state = 'ACTIVE'
            """,
            (service,),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _round_service_count(
        conn: sqlite3.Connection, round_id: str, service: str
    ) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM applications
            WHERE round_id = ? AND service = ? AND round_enrolled = 1
            """,
            (round_id, service),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _pool_available(
        conn: sqlite3.Connection, resource_pool: str
    ) -> tuple[int, int]:
        pool = conn.execute(
            """
            SELECT capacity_mc, capacity_mib, borrowed_mc, borrowed_mib
            FROM resource_pools WHERE name = ?
            """,
            (resource_pool,),
        ).fetchone()
        if pool is None:
            raise error(
                "RESOURCE_POOL_UNKNOWN",
                f"未知资源池: {resource_pool}",
            )
        allocated = conn.execute(
            """
            SELECT COALESCE(SUM(quota_mc), 0) AS q_mc,
                   COALESCE(SUM(quota_mib), 0) AS q_mib
            FROM tenants WHERE resource_pool = ?
            """,
            (resource_pool,),
        ).fetchone()
        avail_mc = (
            int(pool["capacity_mc"])
            - int(allocated["q_mc"])
            - int(pool["borrowed_mc"])
        )
        avail_mib = (
            int(pool["capacity_mib"])
            - int(allocated["q_mib"])
            - int(pool["borrowed_mib"])
        )
        return avail_mc, avail_mib

    @staticmethod
    def _submitted_watermark(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) AS watermark
            FROM applications WHERE status != 'STAGED'
            """
        ).fetchone()
        return int(row["watermark"])

    @staticmethod
    def _dump_rows(
        conn: sqlite3.Connection,
        table: str,
        columns: Sequence[str],
        where: str = "",
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        column_sql = ", ".join(columns)
        sql = f"SELECT {column_sql} FROM {table}"
        if where:
            sql += f" WHERE {where}"
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def _snapshot_digests(self, conn: sqlite3.Connection) -> dict[str, str]:
        tenant_rows = self._dump_rows(
            conn,
            "tenants",
            (
                "name",
                "resource_pool",
                "current_version",
                "current_digest",
                "quota_mc",
                "quota_mib",
                "allow_overdraft",
                "borrow_limit_mc",
                "borrow_limit_mib",
                "base_used_mc",
                "base_used_mib",
                "borrowed_mc",
                "borrowed_mib",
            ),
            "1 = 1 ORDER BY name",
        )
        reservation_rows = self._dump_rows(
            conn,
            "reservations",
            (
                "reservation_id",
                "application_id",
                "tenant",
                "service",
                "declaration_version",
                "base_mc",
                "base_mib",
                "borrow_mc",
                "borrow_mib",
                "state",
                "released_at_ms",
            ),
            "1 = 1 ORDER BY reservation_id",
        )
        queue_rows = self._dump_rows(
            conn,
            "wait_queue",
            (
                "application_id",
                "tenant",
                "sequence",
                "requested_at_ms",
                "reason_code",
            ),
            "1 = 1 ORDER BY tenant, requested_at_ms, sequence",
        )
        borrow_rows = self._dump_rows(
            conn,
            "borrow_obligations",
            (
                "obligation_id",
                "application_id",
                "tenant",
                "resource_pool",
                "outstanding_mc",
                "outstanding_mib",
                "state",
            ),
            "1 = 1 ORDER BY obligation_id",
        )
        ledger_rows = self._dump_rows(
            conn,
            "ledger_entries",
            (
                "id",
                "transaction_id",
                "sequence_in_transaction",
                "tenant",
                "application_id",
                "entry_type",
                "amount_mc",
                "amount_mib",
                "base_used_after_mc",
                "base_used_after_mib",
                "borrowed_after_mc",
                "borrowed_after_mib",
            ),
            "1 = 1 ORDER BY id",
        )
        return {
            "tenant_digest": digest(tenant_rows),
            "reservation_digest": digest(reservation_rows),
            "queue_digest": digest(queue_rows),
            "borrow_digest": digest(borrow_rows),
            "ledger_digest": digest(ledger_rows),
        }

    def _write_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        operation_id: str,
        anchor: str,
        at_ms: int,
    ) -> dict[str, Any]:
        digests = self._snapshot_digests(conn)
        clock = self._current_clock(conn)
        watermark = self._submitted_watermark(conn)
        combined = digest(
            {
                "logical_clock_ms": clock,
                "submitted_watermark": watermark,
                **digests,
            }
        )
        conn.execute(
            """
            INSERT INTO snapshots(
                operation_id, anchor, logical_clock_ms, submitted_watermark,
                tenant_digest, reservation_digest, queue_digest, borrow_digest,
                ledger_digest, combined_digest, at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                anchor,
                clock,
                watermark,
                digests["tenant_digest"],
                digests["reservation_digest"],
                digests["queue_digest"],
                digests["borrow_digest"],
                digests["ledger_digest"],
                combined,
                at_ms,
            ),
        )
        self._set_meta(conn, "submitted_watermark", watermark)
        self._set_meta(conn, "last_operation_id", operation_id)
        return {
            "logical_clock_ms": clock,
            "submitted_watermark": watermark,
            "combined_digest": combined,
            **digests,
        }

    # ------------------------------------------------------------------
    # Loading and version replacement
    # ------------------------------------------------------------------
    def load_bundle(
        self,
        tenants: TenantBundle,
        services: ServiceBundle,
        applications: ApplicationBundle,
        *,
        reset: bool = False,
        at_ms: int = 0,
    ) -> dict[str, Any]:
        self.initialize_if_needed()
        with self._exclusive():
            conn = self._new_conn()
            try:
                with transaction(conn):
                    if reset:
                        for table in DELETE_ORDER:
                            conn.execute(f"DROP TABLE IF EXISTS {table}")
                        initialize_schema(conn)

                    now = self._advance_clock(conn, at_ms)
                    summary = self._load_within_transaction(
                        conn,
                        tenants,
                        services,
                        applications,
                        reset=reset,
                        at_ms=now,
                    )
                    snapshot = self._write_snapshot(
                        conn,
                        operation_id=f"load:{now}:{summary['bundle_digest'][:16]}",
                        anchor="LOAD",
                        at_ms=now,
                    )
                    summary["snapshot"] = snapshot
                    return summary
            finally:
                conn.close()

    def _load_within_transaction(
        self,
        conn: sqlite3.Connection,
        tenants: TenantBundle,
        services: ServiceBundle,
        applications: ApplicationBundle,
        *,
        reset: bool,
        at_ms: int,
    ) -> dict[str, Any]:
        existing_state = conn.execute(
            "SELECT COUNT(*) AS n FROM tenants"
        ).fetchone()["n"]

        current_scale = int(self._meta(conn, "fixed_scale", "1000") or 1000)
        current_tol = float(
            self._meta(conn, "precision_tolerance", "0.000001") or 0.000001
        )
        if existing_state and not reset:
            if current_scale != tenants.fixed_scale:
                raise error(
                    "FIXED_SCALE_CONFLICT",
                    "已初始化账本的定点标度不可静默变更",
                    details={
                        "current": current_scale,
                        "incoming": tenants.fixed_scale,
                    },
                )
            if current_tol != tenants.precision_tolerance:
                raise error(
                    "PRECISION_TOLERANCE_CONFLICT",
                    "已初始化账本的精度容差不可静默变更",
                    details={
                        "current": current_tol,
                        "incoming": tenants.precision_tolerance,
                    },
                )
        self._set_meta(conn, "fixed_scale", tenants.fixed_scale)
        self._set_meta(conn, "precision_tolerance", tenants.precision_tolerance)

        pool_changes: list[dict[str, Any]] = []
        for pool in tenants.pools:
            existing = conn.execute(
                "SELECT * FROM resource_pools WHERE name = ?", (pool.name,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO resource_pools(
                        name, capacity_mc, capacity_mib, borrowed_mc, borrowed_mib
                    ) VALUES (?, ?, ?, 0, 0)
                    """,
                    (pool.name, pool.capacity_mc, pool.capacity_mib),
                )
                pool_changes.append({"name": pool.name, "operation": "ADDED"})
            elif (
                int(existing["capacity_mc"]) != pool.capacity_mc
                or int(existing["capacity_mib"]) != pool.capacity_mib
            ):
                raise error(
                    "RESOURCE_POOL_CAPACITY_IMMUTABLE",
                    f"资源池 {pool.name} 容量已固定，不允许静默改容",
                    details={
                        "resource_pool": pool.name,
                        "current": {
                            "millicores": existing["capacity_mc"],
                            "mib": existing["capacity_mib"],
                        },
                        "incoming": {
                            "millicores": pool.capacity_mc,
                            "mib": pool.capacity_mib,
                        },
                    },
                )

        service_changes: list[dict[str, Any]] = []
        for service in services.services:
            existing = conn.execute(
                "SELECT * FROM services WHERE name = ?", (service.name,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO services(
                        name, resource_pool, requests_per_round,
                        max_concurrent_reservations
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        service.name,
                        service.resource_pool,
                        service.requests_per_round,
                        service.max_concurrent_reservations,
                    ),
                )
                service_changes.append(
                    {"name": service.name, "operation": "INCREMENTAL_APPEND"}
                )
                continue
            if (
                existing["resource_pool"] != service.resource_pool
                or int(existing["requests_per_round"]) != service.requests_per_round
                or int(existing["max_concurrent_reservations"])
                != service.max_concurrent_reservations
            ):
                raise error(
                    "SERVICE_DECLARATION_CONFLICT",
                    f"服务 {service.name} 声明不可使用未编号的覆盖更新",
                    details={"service": service.name},
                )
            service_changes.append(
                {"name": service.name, "operation": "NO_OP"}
            )

        tenant_changes: list[dict[str, Any]] = []
        for tenant in tenants.tenants:
            existing = conn.execute(
                "SELECT * FROM tenants WHERE name = ?", (tenant.name,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO tenants(
                        name, resource_pool, current_version, current_digest,
                        quota_mc, quota_mib, allow_overdraft,
                        borrow_limit_mc, borrow_limit_mib,
                        base_used_mc, base_used_mib, borrowed_mc, borrowed_mib,
                        precision_sensitive, cumulative_error_micro,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?, ?)
                    """,
                    (
                        tenant.name,
                        tenant.resource_pool,
                        tenant.declaration_version,
                        tenant.digest,
                        tenant.quota_mc,
                        tenant.quota_mib,
                        int(tenant.allow_overdraft),
                        tenant.borrow_limit_mc,
                        tenant.borrow_limit_mib,
                        at_ms,
                        at_ms,
                    ),
                )
                self._insert_tenant_version(
                    conn,
                    tenant,
                    classification="INCREMENTAL_APPEND",
                    at_ms=at_ms,
                )
                tenant_changes.append(
                    {"tenant": tenant.name, "operation": "INCREMENTAL_APPEND"}
                )
                continue

            current_version = int(existing["current_version"])
            if tenant.declaration_version < current_version:
                raise error(
                    "VERSION_ROLLBACK",
                    f"租户 {tenant.name} 的配额声明版本回退被拒绝",
                    details={
                        "tenant": tenant.name,
                        "current_version": current_version,
                        "incoming_version": tenant.declaration_version,
                    },
                )
            if existing["resource_pool"] != tenant.resource_pool:
                raise error(
                    "TENANT_POOL_IMMUTABLE",
                    f"租户 {tenant.name} 不允许通过声明替换迁移资源池",
                    details={"tenant": tenant.name},
                )
            if tenant.declaration_version == current_version:
                if existing["current_digest"] != tenant.digest:
                    raise error(
                        "VERSION_CONFLICT",
                        f"租户 {tenant.name} 同一版本出现不同内容摘要",
                        details={
                            "tenant": tenant.name,
                            "version": current_version,
                            "current_digest": existing["current_digest"],
                            "incoming_digest": tenant.digest,
                        },
                    )
                tenant_changes.append(
                    {"tenant": tenant.name, "operation": "NO_OP"}
                )
                continue

            changed = (
                int(existing["quota_mc"]) != tenant.quota_mc
                or int(existing["quota_mib"]) != tenant.quota_mib
                or int(existing["borrow_limit_mc"]) != tenant.borrow_limit_mc
                or int(existing["borrow_limit_mib"]) != tenant.borrow_limit_mib
                or int(existing["allow_overdraft"]) != int(tenant.allow_overdraft)
            )
            if tenant.quota_mc < int(existing["base_used_mc"]):
                raise error(
                    "VERSION_WOULD_UNDERFLOW_QUOTA",
                    f"租户 {tenant.name} 新版本毫核配额低于已用基础配额",
                    details={
                        "tenant": tenant.name,
                        "quota_mc": tenant.quota_mc,
                        "base_used_mc": existing["base_used_mc"],
                    },
                )
            if tenant.quota_mib < int(existing["base_used_mib"]):
                raise error(
                    "VERSION_WOULD_UNDERFLOW_QUOTA",
                    f"租户 {tenant.name} 新版本 MiB 配额低于已用基础配额",
                    details={
                        "tenant": tenant.name,
                        "quota_mib": tenant.quota_mib,
                        "base_used_mib": existing["base_used_mib"],
                    },
                )
            if tenant.borrow_limit_mc < int(existing["borrowed_mc"]):
                raise error(
                    "VERSION_WOULD_UNDERFLOW_BORROW",
                    f"租户 {tenant.name} 新版本毫核借调上限低于未归还借调量",
                    details={
                        "tenant": tenant.name,
                        "borrow_limit_mc": tenant.borrow_limit_mc,
                        "borrowed_mc": existing["borrowed_mc"],
                    },
                )
            if tenant.borrow_limit_mib < int(existing["borrowed_mib"]):
                raise error(
                    "VERSION_WOULD_UNDERFLOW_BORROW",
                    f"租户 {tenant.name} 新版本 MiB 借调上限低于未归还借调量",
                    details={
                        "tenant": tenant.name,
                        "borrow_limit_mib": tenant.borrow_limit_mib,
                        "borrowed_mib": existing["borrowed_mib"],
                    },
                )
            classification = "FULL_REPLACE" if changed else "NO_OP"
            self._insert_tenant_version(
                conn, tenant, classification=classification, at_ms=at_ms
            )
            conn.execute(
                """
                UPDATE tenants SET
                    current_version = ?, current_digest = ?,
                    quota_mc = ?, quota_mib = ?,
                    allow_overdraft = ?, borrow_limit_mc = ?, borrow_limit_mib = ?,
                    updated_at_ms = ?
                WHERE name = ?
                """,
                (
                    tenant.declaration_version,
                    tenant.digest,
                    tenant.quota_mc,
                    tenant.quota_mib,
                    int(tenant.allow_overdraft),
                    tenant.borrow_limit_mc,
                    tenant.borrow_limit_mib,
                    at_ms,
                    tenant.name,
                ),
            )
            tenant_changes.append(
                {
                    "tenant": tenant.name,
                    "operation": classification,
                    "from_version": current_version,
                    "to_version": tenant.declaration_version,
                }
            )

        pool_guard = conn.execute(
            """
            SELECT p.name, p.capacity_mc, p.capacity_mib,
                   p.borrowed_mc, p.borrowed_mib,
                   COALESCE(SUM(t.quota_mc), 0) AS quota_mc,
                   COALESCE(SUM(t.quota_mib), 0) AS quota_mib
            FROM resource_pools p
            LEFT JOIN tenants t ON t.resource_pool = p.name
            GROUP BY p.name
            """
        ).fetchall()
        for row in pool_guard:
            if int(row["quota_mc"]) + int(row["borrowed_mc"]) > int(
                row["capacity_mc"]
            ) or int(row["quota_mib"]) + int(row["borrowed_mib"]) > int(
                row["capacity_mib"]
            ):
                raise error(
                    "RESOURCE_POOL_CAPACITY_EXCEEDED",
                    f"资源池 {row['name']} 的基础配额与在借量超过池容量",
                    details={
                        "resource_pool": row["name"],
                        "committed_mc": int(row["quota_mc"])
                        + int(row["borrowed_mc"]),
                        "capacity_mc": int(row["capacity_mc"]),
                        "committed_mib": int(row["quota_mib"])
                        + int(row["borrowed_mib"]),
                        "capacity_mib": int(row["capacity_mib"]),
                    },
                )

        existing_apps = {
            row["application_id"]: row
            for row in conn.execute("SELECT * FROM applications").fetchall()
        }
        sequence_rows = {
            int(row["sequence"]): row["application_id"]
            for row in conn.execute(
                "SELECT sequence, application_id FROM applications"
            ).fetchall()
        }
        max_sequence = max(sequence_rows, default=0)
        app_changes: list[dict[str, Any]] = []
        for app_spec in applications.applications:
            existing_app = existing_apps.get(app_spec.application_id)
            if existing_app is not None:
                if existing_app["fingerprint"] != app_spec.fingerprint:
                    raise error(
                        "APPLICATION_IDEMPOTENCY_CONFLICT",
                        f"申请 {app_spec.application_id} 已存在且内容不同",
                        details={
                            "application_id": app_spec.application_id,
                            "sequence": existing_app["sequence"],
                        },
                    )
                app_changes.append(
                    {
                        "application_id": app_spec.application_id,
                        "operation": "NO_OP",
                    }
                )
                continue
            if app_spec.sequence in sequence_rows:
                raise error(
                    "APPLICATION_SEQUENCE_DUPLICATE",
                    f"申请序号 {app_spec.sequence} 已被其他申请占用",
                    details={
                        "sequence": app_spec.sequence,
                        "existing_application_id": sequence_rows[
                            app_spec.sequence
                        ],
                    },
                )
            if app_spec.sequence <= max_sequence:
                raise error(
                    "APPLICATION_SEQUENCE_ORDER",
                    "新增申请序号必须大于账本中的最大已提交序号",
                    details={
                        "sequence": app_spec.sequence,
                        "existing_max_sequence": max_sequence,
                    },
                )
            conn.execute(
                """
                INSERT INTO applications(
                    application_id, sequence, tenant, service,
                    request_mc, request_mib, requested_at_ms, duration_ms,
                    expires_at_ms, fingerprint, status, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'STAGED', ?, ?)
                """,
                (
                    app_spec.application_id,
                    app_spec.sequence,
                    app_spec.tenant,
                    app_spec.service,
                    app_spec.request_mc,
                    app_spec.request_mib,
                    app_spec.requested_at_ms,
                    app_spec.duration_ms,
                    app_spec.expires_at_ms,
                    app_spec.fingerprint,
                    at_ms,
                    at_ms,
                ),
            )
            existing_apps[app_spec.application_id] = conn.execute(
                "SELECT * FROM applications WHERE application_id = ?",
                (app_spec.application_id,),
            ).fetchone()
            sequence_rows[app_spec.sequence] = app_spec.application_id
            max_sequence = max(max_sequence, app_spec.sequence)
            app_changes.append(
                {
                    "application_id": app_spec.application_id,
                    "operation": "STAGED",
                }
            )

        bundle_digest = digest(
            {
                "tenants": [t.payload() for t in tenants.tenants],
                "pools": [p.__dict__ for p in tenants.pools],
                "services": [s.__dict__ for s in services.services],
                "applications": [a.payload() for a in applications.applications],
            }
        )
        changed_tenants = [
            c
            for c in tenant_changes
            if c["operation"] in {"FULL_REPLACE", "INCREMENTAL_APPEND"}
        ]
        if tenant_changes and all(
            c["operation"] == "NO_OP" for c in tenant_changes
        ):
            tenant_operation = "NO_OP"
        elif changed_tenants and all(
            c["operation"] == "INCREMENTAL_APPEND" for c in changed_tenants
        ):
            tenant_operation = "INCREMENTAL_APPEND"
        elif changed_tenants:
            tenant_operation = "FULL_REPLACE"
        else:
            tenant_operation = "NO_OP"

        return {
            "ok": True,
            "operation": tenant_operation,
            "reset": reset,
            "tenant_changes": tenant_changes,
            "service_changes": service_changes,
            "application_changes": app_changes,
            "pool_changes": pool_changes,
            "fixed_scale": tenants.fixed_scale,
            "precision_tolerance": tenants.precision_tolerance,
            "bundle_digest": bundle_digest,
        }

    def _insert_tenant_version(
        self,
        conn: sqlite3.Connection,
        tenant: TenantSpec,
        *,
        classification: str,
        at_ms: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO tenant_versions(
                tenant_name, version, digest, resource_pool,
                quota_mc, quota_mib, allow_overdraft,
                borrow_limit_mc, borrow_limit_mib, classification,
                created_at_ms, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant.name,
                tenant.declaration_version,
                tenant.digest,
                tenant.resource_pool,
                tenant.quota_mc,
                tenant.quota_mib,
                int(tenant.allow_overdraft),
                tenant.borrow_limit_mc,
                tenant.borrow_limit_mib,
                classification,
                at_ms,
                canonical_json(tenant.payload()),
            ),
        )

    # ------------------------------------------------------------------
    # Read helpers used by CLI and web UI
    # ------------------------------------------------------------------
    def list_staged_applications(self) -> list[dict[str, Any]]:
        self.initialize_if_needed()
        conn = self._new_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM applications WHERE status = 'STAGED' ORDER BY sequence"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_application(self, application_id: str) -> dict[str, Any]:
        self.initialize_if_needed()
        conn = self._new_conn()
        try:
            conn.execute("BEGIN")
            app = self._get_application(conn, application_id)
            reservation = self._get_reservation(conn, application_id)
            queue = conn.execute(
                "SELECT * FROM wait_queue WHERE application_id = ?",
                (application_id,),
            ).fetchone()
            borrow = conn.execute(
                "SELECT * FROM borrow_obligations WHERE application_id = ?",
                (application_id,),
            ).fetchone()
            return {
                "application": dict(app),
                "reservation": _row_dict(reservation),
                "queue": _row_dict(queue),
                "borrow_obligation": _row_dict(borrow),
            }
        finally:
            conn.rollback()
            conn.close()

    # ------------------------------------------------------------------
    # Submit and admission
    # ------------------------------------------------------------------
    def submit(
        self,
        application_id: str,
        *,
        now_ms: int | None = None,
        round_id: str | None = None,
        round_start_ms: int | None = None,
        round_budget_ms: int | None = None,
    ) -> dict[str, Any]:
        self.initialize_if_needed()
        with self._exclusive():
            conn = self._new_conn()
            try:
                app = self._get_application(conn, application_id)
                if app["status"] in {
                    "ADMITTED",
                    "WAITING",
                    "RELEASED",
                    "ABANDONED",
                    "CANCELLED",
                }:
                    return self._app_result(conn, app, duplicate=True)
                with transaction(conn):
                    effective_now = max(
                        self._current_clock(conn),
                        int(now_ms)
                        if now_ms is not None
                        else int(app["requested_at_ms"]),
                        int(app["requested_at_ms"]),
                    )
                    self._advance_clock(conn, effective_now)

                self._release_due_locked(conn, effective_now)
                app = self._get_application(conn, application_id)
                if app["status"] in {
                    "ADMITTED",
                    "WAITING",
                    "RELEASED",
                    "ABANDONED",
                    "CANCELLED",
                }:
                    return self._app_result(conn, app, duplicate=True)

                chosen_round = round_id or f"round-auto:{app['requested_at_ms']}"
                chosen_start = (
                    int(round_start_ms)
                    if round_start_ms is not None
                    else int(app["requested_at_ms"])
                )
                chosen_budget = (
                    int(round_budget_ms)
                    if round_budget_ms is not None
                    else 2**62
                )
                if chosen_start < 0 or chosen_budget < 0:
                    raise error(
                        "ROUND_INVALID",
                        "轮次起始与预算必须为非负整数",
                    )

                operation_id = self._admit_operation_id(app)
                existing_op = conn.execute(
                    "SELECT * FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if existing_op is not None and existing_op["state"] == "DONE":
                    return _json_load(existing_op["result_json"], {})
                if existing_op is not None and existing_op["state"] == "FAILED":
                    raise error(
                        "OPERATION_FAILED",
                        "准入操作此前已失败，请检查账本",
                        details={
                            "operation_id": operation_id,
                            "error_code": existing_op["error_code"],
                        },
                    )
                if existing_op is None:
                    payload = {
                        "application_id": application_id,
                        "round_id": chosen_round,
                        "round_start_ms": chosen_start,
                        "round_budget_ms": chosen_budget,
                        "now_ms": effective_now,
                    }
                    with transaction(conn):
                        conn.execute(
                            """
                            INSERT INTO operations(
                                operation_id, kind, application_id, state,
                                payload_json, created_at_ms, updated_at_ms
                            ) VALUES (?, 'ADMIT', ?, 'PREPARED', ?, ?, ?)
                            """,
                            (
                                operation_id,
                                application_id,
                                canonical_json(payload),
                                effective_now,
                                effective_now,
                            ),
                        )
                    self._crashpoint("after_admit_prepare")

                return self._apply_admit_operation(conn, operation_id)
            finally:
                conn.close()

    @staticmethod
    def _admit_operation_id(app: sqlite3.Row) -> str:
        return f"admit:{app['application_id']}:{app['fingerprint'][:16]}"

    def _apply_admit_operation(
        self, conn: sqlite3.Connection, operation_id: str
    ) -> dict[str, Any]:
        with transaction(conn):
            operation = conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise error("OPERATION_UNKNOWN", "准入操作不存在")
            if operation["state"] == "DONE":
                return _json_load(operation["result_json"], {})
            payload = _json_load(operation["payload_json"], {})
            app = self._get_application(
                conn, payload["application_id"]
            )
            at_ms = max(
                self._current_clock(conn), int(payload.get("now_ms", 0))
            )
            self._advance_clock(conn, at_ms)
            if app["status"] != "STAGED":
                result = self._app_result(conn, app, duplicate=True)
                conn.execute(
                    """
                    UPDATE operations SET state = 'DONE', result_json = ?,
                        updated_at_ms = ?
                    WHERE operation_id = ?
                    """,
                    (canonical_json(result), at_ms, operation_id),
                )
                self._write_snapshot(
                    conn,
                    operation_id=operation_id,
                    anchor="ADMIT_RESUME_NOOP",
                    at_ms=at_ms,
                )
                return result

            allowed, abandon_code = self._enroll_round_locked(
                conn,
                app,
                round_id=payload["round_id"],
                round_start_ms=int(payload["round_start_ms"]),
                round_budget_ms=int(payload["round_budget_ms"]),
                at_ms=at_ms,
            )
            if not allowed:
                result = self._abandon_locked(
                    conn,
                    app,
                    reason_code=abandon_code,
                    at_ms=at_ms,
                    round_id=payload["round_id"],
                    round_start_ms=int(payload["round_start_ms"]),
                    round_budget_ms=int(payload["round_budget_ms"]),
                )
                self._finish_operation(conn, operation_id, result, at_ms)
                self._write_snapshot(
                    conn,
                    operation_id=operation_id,
                    anchor="ADMIT_ABANDON",
                    at_ms=at_ms,
                )
                return result

            outcome = self._try_admit_locked(
                conn,
                app,
                at_ms=at_ms,
                operation_id=operation_id,
                transaction_id=operation_id,
                allow_queue=True,
            )
            if outcome["outcome"] == "ADMITTED":
                result = outcome["result"]
            elif outcome["outcome"] == "BLOCKED":
                result = self._enqueue_locked(
                    conn,
                    app,
                    reason_code=outcome["reason_code"],
                    reason_text=outcome.get("reason_text", ""),
                    at_ms=at_ms,
                    last_release_id=None,
                )
            else:
                result = self._abandon_locked(
                    conn,
                    app,
                    reason_code=outcome["reason_code"],
                    at_ms=at_ms,
                    round_id=payload["round_id"],
                    round_start_ms=int(payload["round_start_ms"]),
                    round_budget_ms=int(payload["round_budget_ms"]),
                )
            self._crashpoint("during_admit_apply")
            self._finish_operation(conn, operation_id, result, at_ms)
            self._write_snapshot(
                conn,
                operation_id=operation_id,
                anchor="ADMIT",
                at_ms=at_ms,
            )
            return result

    def _enroll_round_locked(
        self,
        conn: sqlite3.Connection,
        app: sqlite3.Row,
        *,
        round_id: str,
        round_start_ms: int,
        round_budget_ms: int,
        at_ms: int,
    ) -> tuple[bool, str | None]:
        if int(app["round_enrolled"]) == 1:
            return True, None
        requested_at = int(app["requested_at_ms"])
        if requested_at < round_start_ms:
            return False, "ROUND_BUDGET_EXHAUSTED"
        if requested_at - round_start_ms > round_budget_ms:
            return False, "ROUND_BUDGET_EXHAUSTED"
        service = self._get_service(conn, app["service"])
        used = self._round_service_count(conn, round_id, app["service"])
        if used >= int(service["requests_per_round"]):
            return False, "SERVICE_ROUND_BATCH_LIMIT"
        conn.execute(
            """
            UPDATE applications SET
                round_id = ?, round_start_ms = ?, round_budget_ms = ?,
                round_enrolled = 1, updated_at_ms = ?
            WHERE application_id = ?
            """,
            (
                round_id,
                round_start_ms,
                round_budget_ms,
                at_ms,
                app["application_id"],
            ),
        )
        return True, None

    def _try_admit_locked(
        self,
        conn: sqlite3.Connection,
        app: sqlite3.Row,
        *,
        at_ms: int,
        operation_id: str,
        transaction_id: str,
        allow_queue: bool,
    ) -> dict[str, Any]:
        if at_ms >= int(app["expires_at_ms"]):
            return {
                "outcome": "EXPIRED",
                "reason_code": "APPLICATION_EXPIRED",
            }
        service = self._get_service(conn, app["service"])
        if (
            self._service_active_count(conn, app["service"])
            >= int(service["max_concurrent_reservations"])
        ):
            return {
                "outcome": "BLOCKED",
                "reason_code": "SERVICE_CONCURRENCY_LIMIT",
                "reason_text": _reason("SERVICE_CONCURRENCY_LIMIT"),
            }
        tenant = self._get_tenant(conn, app["tenant"])

        decision = self._capacity_decision(conn, tenant, app)
        sensitive = self._record_capacity_view(
            conn,
            tenant=tenant,
            app=app,
            decision=decision,
            operation_id=operation_id,
            at_ms=at_ms,
        )
        if not decision["admitted"]:
            return {
                "outcome": "BLOCKED",
                "reason_code": decision["reason_code"],
                "reason_text": _reason(decision["reason_code"]),
                "precision_sensitive": sensitive,
            }

        base_mc = int(decision["base_mc"])
        base_mib = int(decision["base_mib"])
        borrow_mc = int(decision["borrow_mc"])
        borrow_mib = int(decision["borrow_mib"])
        request_mc = int(app["request_mc"])
        request_mib = int(app["request_mib"])
        if base_mc + borrow_mc != request_mc or base_mib + borrow_mib != request_mib:
            raise error(
                "ADMISSION_SPLIT_INVARIANT",
                "准入资源拆分不守恒",
                details={
                    "application_id": app["application_id"],
                    "base_mc": base_mc,
                    "borrow_mc": borrow_mc,
                    "request_mc": request_mc,
                    "base_mib": base_mib,
                    "borrow_mib": borrow_mib,
                    "request_mib": request_mib,
                },
            )

        before_base_mc = int(tenant["base_used_mc"])
        before_base_mib = int(tenant["base_used_mib"])
        before_borrowed_mc = int(tenant["borrowed_mc"])
        before_borrowed_mib = int(tenant["borrowed_mib"])
        pool_before = conn.execute(
            """
            SELECT borrowed_mc, borrowed_mib FROM resource_pools WHERE name = ?
            """,
            (tenant["resource_pool"],),
        ).fetchone()
        after_base_mc = before_base_mc + base_mc
        after_base_mib = before_base_mib + base_mib
        after_borrowed_mc = before_borrowed_mc + borrow_mc
        after_borrowed_mib = before_borrowed_mib + borrow_mib
        if (
            after_base_mc > int(tenant["quota_mc"])
            or after_base_mib > int(tenant["quota_mib"])
        ):
            raise error(
                "NEGATIVE_REMAINING",
                "准入将导致基础配额余量为负",
                details={"tenant": tenant["name"]},
            )
        if (
            after_borrowed_mc > int(tenant["borrow_limit_mc"])
            or after_borrowed_mib > int(tenant["borrow_limit_mib"])
        ):
            raise error(
                "BORROW_LIMIT_EXCEEDED",
                "准入将导致借调余额超过上限",
                details={"tenant": tenant["name"]},
            )

        conn.execute(
            """
            UPDATE tenants SET base_used_mc = ?, base_used_mib = ?,
                borrowed_mc = ?, borrowed_mib = ?, updated_at_ms = ?
            WHERE name = ?
            """,
            (
                after_base_mc,
                after_base_mib,
                after_borrowed_mc,
                after_borrowed_mib,
                at_ms,
                tenant["name"],
            ),
        )
        if borrow_mc or borrow_mib:
            conn.execute(
                """
                UPDATE resource_pools SET borrowed_mc = borrowed_mc + ?,
                    borrowed_mib = borrowed_mib + ?
                WHERE name = ?
                """,
                (borrow_mc, borrow_mib, tenant["resource_pool"]),
            )
        pool_after = conn.execute(
            """
            SELECT borrowed_mc, borrowed_mib FROM resource_pools WHERE name = ?
            """,
            (tenant["resource_pool"],),
        ).fetchone()

        reservation_id = f"reservation:{app['application_id']}"
        conn.execute(
            """
            INSERT INTO reservations(
                reservation_id, application_id, tenant, service,
                declaration_version, declaration_digest,
                base_mc, base_mib, borrow_mc, borrow_mib, total_mc, total_mib,
                admitted_at_ms, expires_at_ms, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
            """,
            (
                reservation_id,
                app["application_id"],
                tenant["name"],
                app["service"],
                int(tenant["current_version"]),
                tenant["current_digest"],
                base_mc,
                base_mib,
                borrow_mc,
                borrow_mib,
                request_mc,
                request_mib,
                at_ms,
                int(app["expires_at_ms"]),
            ),
        )
        if borrow_mc or borrow_mib:
            conn.execute(
                """
                INSERT INTO borrow_obligations(
                    obligation_id, application_id, tenant, resource_pool,
                    borrow_mc, borrow_mib,
                    source_available_mc, source_available_mib,
                    outstanding_mc, outstanding_mib,
                    borrowed_at_ms, due_at_ms, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                """,
                (
                    f"borrow:{app['application_id']}",
                    app["application_id"],
                    tenant["name"],
                    tenant["resource_pool"],
                    borrow_mc,
                    borrow_mib,
                    int(decision.get("pool_available_mc", 0)),
                    int(decision.get("pool_available_mib", 0)),
                    borrow_mc,
                    borrow_mib,
                    at_ms,
                    int(app["expires_at_ms"]),
                ),
            )

        conn.execute(
            """
            UPDATE applications SET status = 'ADMITTED', reason_code = NULL,
                reason_text = NULL, admitted_version = ?, admitted_at_ms = ?,
                round_id = COALESCE(round_id, ?),
                updated_at_ms = ?
            WHERE application_id = ?
            """,
            (
                int(tenant["current_version"]),
                at_ms,
                f"wake:{operation_id}",
                at_ms,
                app["application_id"],
            ),
        )
        conn.execute(
            "DELETE FROM wait_queue WHERE application_id = ?",
            (app["application_id"],),
        )
        conn.execute(
            """
            INSERT INTO ledger_entries(
                transaction_id, operation_id, sequence_in_transaction,
                tenant, application_id, entry_type, amount_mc, amount_mib,
                base_used_before_mc, base_used_after_mc,
                base_used_before_mib, base_used_after_mib,
                borrowed_before_mc, borrowed_after_mc,
                borrowed_before_mib, borrowed_after_mib,
                pool_borrowed_before_mc, pool_borrowed_after_mc,
                pool_borrowed_before_mib, pool_borrowed_after_mib,
                declaration_version, at_ms
            ) VALUES (?, ?, 1, ?, ?, 'QUOTA_ADMIT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction_id,
                operation_id,
                tenant["name"],
                app["application_id"],
                base_mc,
                base_mib,
                before_base_mc,
                after_base_mc,
                before_base_mib,
                after_base_mib,
                before_borrowed_mc,
                before_borrowed_mc,
                before_borrowed_mib,
                before_borrowed_mib,
                int(pool_before["borrowed_mc"]),
                int(pool_before["borrowed_mc"]),
                int(pool_before["borrowed_mib"]),
                int(pool_before["borrowed_mib"]),
                int(tenant["current_version"]),
                at_ms,
            ),
        )
        if borrow_mc or borrow_mib:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                    transaction_id, operation_id, sequence_in_transaction,
                    tenant, application_id, entry_type, amount_mc, amount_mib,
                    base_used_before_mc, base_used_after_mc,
                    base_used_before_mib, base_used_after_mib,
                    borrowed_before_mc, borrowed_after_mc,
                    borrowed_before_mib, borrowed_after_mib,
                    pool_borrowed_before_mc, pool_borrowed_after_mc,
                    pool_borrowed_before_mib, pool_borrowed_after_mib,
                    declaration_version, at_ms
                ) VALUES (?, ?, 2, ?, ?, 'BORROW_ADMIT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    operation_id,
                    tenant["name"],
                    app["application_id"],
                    borrow_mc,
                    borrow_mib,
                    after_base_mc,
                    after_base_mc,
                    after_base_mib,
                    after_base_mib,
                    before_borrowed_mc,
                    after_borrowed_mc,
                    before_borrowed_mib,
                    after_borrowed_mib,
                    int(pool_before["borrowed_mc"]),
                    int(pool_after["borrowed_mc"]),
                    int(pool_before["borrowed_mib"]),
                    int(pool_after["borrowed_mib"]),
                    int(tenant["current_version"]),
                    at_ms,
                ),
            )

        refreshed = self._get_application(conn, app["application_id"])
        result = self._app_result(conn, refreshed, duplicate=False)
        result["precision_sensitive"] = sensitive
        return {"outcome": "ADMITTED", "result": result}

    def _capacity_decision(
        self,
        conn: sqlite3.Connection,
        tenant: sqlite3.Row,
        app: sqlite3.Row,
    ) -> dict[str, Any]:
        request_mc = int(app["request_mc"])
        request_mib = int(app["request_mib"])
        free_mc = int(tenant["quota_mc"]) - int(tenant["base_used_mc"])
        free_mib = int(tenant["quota_mib"]) - int(tenant["base_used_mib"])
        if free_mc < 0 or free_mib < 0:
            raise error(
                "NEGATIVE_REMAINING",
                "租户基础配额余量为负，拒绝继续准入",
                details={"tenant": tenant["name"]},
            )
        deficit_mc = max(0, request_mc - free_mc)
        deficit_mib = max(0, request_mib - free_mib)
        if deficit_mc == 0 and deficit_mib == 0:
            return {
                "admitted": True,
                "base_mc": request_mc,
                "base_mib": request_mib,
                "borrow_mc": 0,
                "borrow_mib": 0,
                "free_mc": free_mc,
                "free_mib": free_mib,
                "reason_code": None,
            }
        allow = bool(tenant["allow_overdraft"])
        borrow_remaining_mc = (
            int(tenant["borrow_limit_mc"]) - int(tenant["borrowed_mc"])
            if allow
            else 0
        )
        borrow_remaining_mib = (
            int(tenant["borrow_limit_mib"]) - int(tenant["borrowed_mib"])
            if allow
            else 0
        )
        if not allow:
            return {
                "admitted": False,
                "reason_code": "OVERDRAFT_DISABLED",
                "free_mc": free_mc,
                "free_mib": free_mib,
                "borrow_remaining_mc": 0,
                "borrow_remaining_mib": 0,
                "deficit_mc": deficit_mc,
                "deficit_mib": deficit_mib,
            }
        if (
            deficit_mc > borrow_remaining_mc
            or deficit_mib > borrow_remaining_mib
        ):
            return {
                "admitted": False,
                "reason_code": "TENANT_BORROW_LIMIT",
                "free_mc": free_mc,
                "free_mib": free_mib,
                "borrow_remaining_mc": borrow_remaining_mc,
                "borrow_remaining_mib": borrow_remaining_mib,
                "deficit_mc": deficit_mc,
                "deficit_mib": deficit_mib,
            }
        pool_avail_mc, pool_avail_mib = self._pool_available(
            conn, tenant["resource_pool"]
        )
        if deficit_mc > pool_avail_mc or deficit_mib > pool_avail_mib:
            return {
                "admitted": False,
                "reason_code": "POOL_BORROW_EXHAUSTED",
                "free_mc": free_mc,
                "free_mib": free_mib,
                "borrow_remaining_mc": borrow_remaining_mc,
                "borrow_remaining_mib": borrow_remaining_mib,
                "pool_available_mc": pool_avail_mc,
                "pool_available_mib": pool_avail_mib,
                "deficit_mc": deficit_mc,
                "deficit_mib": deficit_mib,
            }
        return {
            "admitted": True,
            "base_mc": min(free_mc, request_mc),
            "base_mib": min(free_mib, request_mib),
            "borrow_mc": deficit_mc,
            "borrow_mib": deficit_mib,
            "free_mc": free_mc,
            "free_mib": free_mib,
            "borrow_remaining_mc": borrow_remaining_mc,
            "borrow_remaining_mib": borrow_remaining_mib,
            "pool_available_mc": pool_avail_mc,
            "pool_available_mib": pool_avail_mib,
            "reason_code": None,
        }

    def _record_capacity_view(
        self,
        conn: sqlite3.Connection,
        *,
        tenant: sqlite3.Row,
        app: sqlite3.Row,
        decision: dict[str, Any],
        operation_id: str,
        at_ms: int,
    ) -> bool:
        scale = int(self._meta(conn, "fixed_scale", "1000") or 1000)
        tolerance = float(
            self._meta(conn, "precision_tolerance", "0.000001") or 0.000001
        )
        free_mc = int(decision.get("free_mc", 0))
        free_mib = int(decision.get("free_mib", 0))
        borrow_remaining_mc = int(decision.get("borrow_remaining_mc", 0))
        borrow_remaining_mib = int(decision.get("borrow_remaining_mib", 0))
        pool_available_mc = int(decision.get("pool_available_mc", 0))
        pool_available_mib = int(decision.get("pool_available_mib", 0))
        projected_base_mc = int(tenant["base_used_mc"]) + int(
            decision.get("base_mc", 0)
        )
        projected_base_mib = int(tenant["base_used_mib"]) + int(
            decision.get("base_mib", 0)
        )
        projected_borrow_mc = int(tenant["borrowed_mc"]) + int(
            decision.get("borrow_mc", 0)
        )
        projected_borrow_mib = int(tenant["borrowed_mib"]) + int(
            decision.get("borrow_mib", 0)
        )
        conversions = [
            exact_scaled("free_quota_mc", free_mc, scale),
            exact_scaled("free_quota_mib", free_mib, scale),
            exact_scaled("borrowed_mc", int(tenant["borrowed_mc"]), scale),
            exact_scaled("borrowed_mib", int(tenant["borrowed_mib"]), scale),
            exact_scaled("borrow_amount_mc", int(decision.get("borrow_mc", 0)), scale),
            exact_scaled("borrow_amount_mib", int(decision.get("borrow_mib", 0)), scale),
            exact_scaled("borrow_remaining_mc", borrow_remaining_mc, scale),
            exact_scaled("borrow_remaining_mib", borrow_remaining_mib, scale),
            exact_scaled("pool_available_mc", pool_available_mc, scale),
            exact_scaled("pool_available_mib", pool_available_mib, scale),
            exact_scaled("request_mc", int(app["request_mc"]), scale),
            exact_scaled("request_mib", int(app["request_mib"]), scale),
        ]
        if int(tenant["quota_mc"]) > 0:
            conversions.append(
                quantize(
                    "usage_rate_mc",
                    projected_base_mc + projected_borrow_mc,
                    int(tenant["quota_mc"]),
                    scale,
                    rounding="DOWN",
                )
            )
        if int(tenant["quota_mib"]) > 0:
            conversions.append(
                quantize(
                    "usage_rate_mib",
                    projected_base_mib + projected_borrow_mib,
                    int(tenant["quota_mib"]),
                    scale,
                    rounding="DOWN",
                )
            )

        persisted_steps: list[dict[str, Any]] = []
        cumulative_error = 0
        for conversion in conversions:
            cursor = conn.execute(
                """
                INSERT INTO conversion_steps(
                    operation_id, tenant, application_id, field, scale,
                    rounding, raw_numerator, raw_denominator, converted,
                    error_bound_micro, at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    tenant["name"],
                    app["application_id"],
                    conversion.field,
                    conversion.scale,
                    conversion.rounding,
                    conversion.raw_numerator,
                    conversion.raw_denominator,
                    conversion.converted,
                    conversion.error_bound_micro,
                    at_ms,
                ),
            )
            persisted_steps.append(
                {"id": cursor.lastrowid, **conversion.as_dict()}
            )
            cumulative_error += conversion.error_bound_micro

        allow = bool(tenant["allow_overdraft"])
        effective_mc = free_mc + (
            min(borrow_remaining_mc, max(pool_available_mc, 0)) if allow else 0
        )
        effective_mib = free_mib + (
            min(borrow_remaining_mib, max(pool_available_mib, 0)) if allow else 0
        )
        request_mc = int(app["request_mc"])
        request_mib = int(app["request_mib"])
        fits_mc = request_mc <= effective_mc
        fits_mib = request_mib <= effective_mib
        gap_mc_micro = abs(effective_mc - request_mc) * 1_000_000
        gap_mib_micro = abs(effective_mib - request_mib) * 1_000_000
        tolerance_value = tolerance_micro(tolerance)
        if fits_mc and fits_mib:
            sensitive = (
                gap_mc_micro < tolerance_value
                or gap_mib_micro < tolerance_value
            )
        elif not fits_mc and fits_mib:
            sensitive = gap_mc_micro < tolerance_value
        elif fits_mc and not fits_mib:
            sensitive = gap_mib_micro < tolerance_value
        else:
            sensitive = False
        gap_micro = min(gap_mc_micro, gap_mib_micro)
        decision_name = "ADMIT" if decision["admitted"] else "BLOCK"
        chain = {
            "tenant": tenant["name"],
            "tenant_version": int(tenant["current_version"]),
            "application_id": app["application_id"],
            "application_sequence": int(app["sequence"]),
            "decision": decision_name,
            "reason_code": decision.get("reason_code"),
            "fixed_scale": scale,
            "precision_tolerance": tolerance,
            "effective_available_mc": effective_mc,
            "effective_available_mib": effective_mib,
            "request_mc": request_mc,
            "request_mib": request_mib,
            "gap_micro": gap_micro,
            "tolerance_micro": tolerance_value,
            "steps": persisted_steps,
        }
        conn.execute(
            """
            INSERT INTO precision_events(
                tenant, application_id, decision, gap_micro,
                tolerance_micro, sensitive, chain_json, at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant["name"],
                app["application_id"],
                decision_name,
                gap_micro,
                tolerance_value,
                int(sensitive),
                canonical_json(chain),
                at_ms,
            ),
        )
        conn.execute(
            """
            UPDATE tenants SET precision_sensitive = CASE WHEN ? THEN 1
                    ELSE precision_sensitive END,
                cumulative_error_micro = cumulative_error_micro + ?,
                updated_at_ms = ?
            WHERE name = ?
            """,
            (int(sensitive), cumulative_error, at_ms, tenant["name"]),
        )
        if sensitive:
            conn.execute(
                """
                UPDATE applications SET precision_sensitive = 1,
                    updated_at_ms = ?
                WHERE application_id = ?
                """,
                (at_ms, app["application_id"]),
            )
        return sensitive

    def _enqueue_locked(
        self,
        conn: sqlite3.Connection,
        app: sqlite3.Row,
        *,
        reason_code: str,
        reason_text: str,
        at_ms: int,
        last_release_id: str | None,
    ) -> dict[str, Any]:
        text = reason_text or _reason(reason_code)
        conn.execute(
            """
            UPDATE applications SET status = 'WAITING', reason_code = ?,
                reason_text = ?, updated_at_ms = ?
            WHERE application_id = ?
            """,
            (reason_code, text, at_ms, app["application_id"]),
        )
        conn.execute(
            """
            INSERT INTO wait_queue(
                application_id, tenant, sequence, requested_at_ms,
                enqueued_at_ms, reason_code, reason_text, last_release_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
                enqueued_at_ms = excluded.enqueued_at_ms,
                reason_code = excluded.reason_code,
                reason_text = excluded.reason_text,
                last_release_id = excluded.last_release_id
            """,
            (
                app["application_id"],
                app["tenant"],
                int(app["sequence"]),
                int(app["requested_at_ms"]),
                at_ms,
                reason_code,
                text,
                last_release_id,
            ),
        )
        refreshed = self._get_application(conn, app["application_id"])
        result = self._app_result(conn, refreshed, duplicate=False)
        result["queue"]["reason_code"] = reason_code
        result["queue"]["reason_text"] = text
        return result

    def _abandon_locked(
        self,
        conn: sqlite3.Connection,
        app: sqlite3.Row,
        *,
        reason_code: str,
        at_ms: int,
        round_id: str | None = None,
        round_start_ms: int | None = None,
        round_budget_ms: int | None = None,
    ) -> dict[str, Any]:
        text = _reason(reason_code)
        conn.execute(
            """
            UPDATE applications SET status = 'ABANDONED', reason_code = ?,
                reason_text = ?, round_id = COALESCE(?, round_id),
                round_start_ms = COALESCE(?, round_start_ms),
                round_budget_ms = COALESCE(?, round_budget_ms),
                updated_at_ms = ?
            WHERE application_id = ?
            """,
            (
                reason_code,
                text,
                round_id,
                round_start_ms,
                round_budget_ms,
                at_ms,
                app["application_id"],
            ),
        )
        conn.execute(
            "DELETE FROM wait_queue WHERE application_id = ?",
            (app["application_id"],),
        )
        refreshed = self._get_application(conn, app["application_id"])
        return self._app_result(conn, refreshed, duplicate=False)

    def abandon_application(
        self,
        application_id: str,
        *,
        reason_code: str = "MANUAL_ABANDON",
        at_ms: int | None = None,
        round_id: str | None = None,
        round_start_ms: int | None = None,
        round_budget_ms: int | None = None,
    ) -> dict[str, Any]:
        self.initialize_if_needed()
        with self._exclusive():
            conn = self._new_conn()
            try:
                app = self._get_application(conn, application_id)
                if app["status"] != "STAGED":
                    return self._app_result(conn, app, duplicate=True)
                with transaction(conn):
                    now = max(
                        self._current_clock(conn),
                        int(at_ms)
                        if at_ms is not None
                        else int(app["requested_at_ms"]),
                    )
                    self._advance_clock(conn, now)
                    result = self._abandon_locked(
                        conn,
                        app,
                        reason_code=reason_code,
                        at_ms=now,
                        round_id=round_id,
                        round_start_ms=round_start_ms,
                        round_budget_ms=round_budget_ms,
                    )
                    self._write_snapshot(
                        conn,
                        operation_id=f"abandon:{application_id}",
                        anchor="ABANDON",
                        at_ms=now,
                    )
                    return result
            finally:
                conn.close()

    def _finish_operation(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        result: dict[str, Any],
        at_ms: int,
    ) -> None:
        conn.execute(
            """
            UPDATE operations SET state = 'DONE', result_json = ?,
                updated_at_ms = ?
            WHERE operation_id = ?
            """,
            (canonical_json(result), at_ms, operation_id),
        )

    @staticmethod
    def _app_result(
        conn: sqlite3.Connection, app: sqlite3.Row, *, duplicate: bool
    ) -> dict[str, Any]:
        reservation = conn.execute(
            "SELECT * FROM reservations WHERE application_id = ?",
            (app["application_id"],),
        ).fetchone()
        queue = conn.execute(
            "SELECT * FROM wait_queue WHERE application_id = ?",
            (app["application_id"],),
        ).fetchone()
        obligation = conn.execute(
            "SELECT * FROM borrow_obligations WHERE application_id = ?",
            (app["application_id"],),
        ).fetchone()
        return {
            "ok": True,
            "duplicate": duplicate,
            "application": dict(app),
            "reservation": _row_dict(reservation),
            "queue": _row_dict(queue),
            "borrow_obligation": _row_dict(obligation),
        }

    # ------------------------------------------------------------------
    # Release, wake-up and due processing
    # ------------------------------------------------------------------
    def release(
        self,
        application_id: str,
        *,
        at_ms: int | None = None,
        trigger_type: str = "MANUAL",
    ) -> dict[str, Any]:
        self.initialize_if_needed()
        with self._exclusive():
            conn = self._new_conn()
            try:
                app = self._get_application(conn, application_id)
                reservation = self._get_reservation(conn, application_id)
                if reservation is None:
                    if app["status"] == "WAITING":
                        return self._cancel_waiting(conn, app, at_ms=at_ms)
                    if app["status"] in {"RELEASED", "ABANDONED", "CANCELLED"}:
                        event = conn.execute(
                            """
                            SELECT * FROM release_events
                            WHERE application_id = ?
                            ORDER BY requested_at_ms DESC LIMIT 1
                            """,
                            (application_id,),
                        ).fetchone()
                        return {
                            "ok": True,
                            "duplicate": True,
                            "application": dict(app),
                            "release_event": _row_dict(event),
                        }
                    raise error(
                        "RESERVATION_NOT_ACTIVE",
                        "申请当前没有可释放的有效预留",
                        details={
                            "application_id": application_id,
                            "status": app["status"],
                        },
                    )
                effective_at = max(
                    self._current_clock(conn),
                    int(at_ms)
                    if at_ms is not None
                    else self._current_clock(conn),
                )
                if trigger_type == "EXPIRY":
                    if effective_at < int(reservation["expires_at_ms"]):
                        raise error(
                            "RESERVATION_NOT_DUE",
                            "预留尚未到期",
                            details={
                                "application_id": application_id,
                                "logical_clock_ms": effective_at,
                                "expires_at_ms": reservation["expires_at_ms"],
                            },
                        )
                self._release_due_locked(
                    conn, effective_at, exclude_application=application_id
                )
                result = self._prepare_and_apply_release(
                    conn,
                    app,
                    at_ms=effective_at,
                    trigger_type=trigger_type,
                )
                return result
            finally:
                conn.close()

    def _cancel_waiting(
        self,
        conn: sqlite3.Connection,
        app: sqlite3.Row,
        *,
        at_ms: int | None,
    ) -> dict[str, Any]:
        with transaction(conn):
            now = max(
                self._current_clock(conn),
                int(at_ms)
                if at_ms is not None
                else int(app["requested_at_ms"]),
            )
            self._advance_clock(conn, now)
            conn.execute(
                """
                UPDATE applications SET status = 'CANCELLED',
                    reason_code = 'QUEUE_CANCELLED',
                    reason_text = '等待中的申请被撤销', updated_at_ms = ?
                WHERE application_id = ?
                """,
                (now, app["application_id"]),
            )
            conn.execute(
                "DELETE FROM wait_queue WHERE application_id = ?",
                (app["application_id"],),
            )
            self._write_snapshot(
                conn,
                operation_id=f"cancel-wait:{app['application_id']}",
                anchor="CANCEL_WAITING",
                at_ms=now,
            )
            refreshed = self._get_application(conn, app["application_id"])
            return {
                "ok": True,
                "duplicate": False,
                "application": dict(refreshed),
                "release_event": None,
            }

    def _prepare_and_apply_release(
        self,
        conn: sqlite3.Connection,
        app: sqlite3.Row,
        *,
        at_ms: int,
        trigger_type: str,
    ) -> dict[str, Any]:
        reservation = self._get_reservation(conn, app["application_id"])
        if reservation is None:
            terminal = self._get_application(conn, app["application_id"])
            return self._app_result(conn, terminal, duplicate=True)
        operation_id = f"release:{app['application_id']}:{reservation['reservation_id']}"
        existing_op = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if existing_op is None:
            payload = {
                "application_id": app["application_id"],
                "trigger_type": trigger_type,
                "at_ms": at_ms,
            }
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO operations(
                        operation_id, kind, application_id, state,
                        payload_json, created_at_ms, updated_at_ms
                    ) VALUES (?, 'RELEASE', ?, 'PREPARED', ?, ?, ?)
                    """,
                    (
                        operation_id,
                        app["application_id"],
                        canonical_json(payload),
                        at_ms,
                        at_ms,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO release_events(
                        release_id, operation_id, application_id, trigger_type,
                        trigger_source_application_id, requested_at_ms, state
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PREPARED')
                    """,
                    (
                        f"rel:{app['application_id']}",
                        operation_id,
                        app["application_id"],
                        trigger_type,
                        app["application_id"],
                        at_ms,
                    ),
                )
            self._crashpoint("after_release_prepare")
        elif existing_op["state"] == "DONE":
            event = conn.execute(
                "SELECT * FROM release_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            return {
                "ok": True,
                "duplicate": True,
                "application": dict(
                    self._get_application(conn, app["application_id"])
                ),
                "release_event": _row_dict(event),
            }
        return self._apply_release_operation(conn, operation_id)

    def _apply_release_operation(
        self, conn: sqlite3.Connection, operation_id: str
    ) -> dict[str, Any]:
        with transaction(conn):
            operation = conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise error("OPERATION_UNKNOWN", "释放操作不存在")
            if operation["state"] == "DONE":
                event = conn.execute(
                    "SELECT * FROM release_events WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                return {
                    "ok": True,
                    "duplicate": True,
                    "application": dict(
                        self._get_application(
                            conn, operation["application_id"]
                        )
                    ),
                    "release_event": _row_dict(event),
                }
            event = conn.execute(
                "SELECT * FROM release_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if event is None:
                raise error(
                    "RELEASE_EVENT_MISSING",
                    "释放事件缺失，拒绝半提交释放",
                )
            payload = _json_load(operation["payload_json"], {})
            app = self._get_application(conn, operation["application_id"])
            reservation = self._get_reservation(
                conn, operation["application_id"]
            )
            if reservation is None:
                raise error(
                    "RELEASE_RESERVATION_MISSING",
                    "释放操作找不到预留记录",
                )
            at_ms = max(
                self._current_clock(conn),
                int(payload.get("at_ms", event["requested_at_ms"])),
            )
            self._advance_clock(conn, at_ms)
            if event["state"] == "COMPLETE" or reservation["state"] == "RELEASED":
                if event["state"] != "COMPLETE" or reservation["state"] != "RELEASED":
                    raise error(
                        "RELEASE_STATE_INCONSISTENT",
                        "预留与释放事件状态不一致，拒绝重复归还",
                    )
                result = {
                    "ok": True,
                    "duplicate": True,
                    "application": dict(app),
                    "release_event": dict(event),
                }
                self._finish_operation(conn, operation_id, result, at_ms)
                self._write_snapshot(
                    conn,
                    operation_id=operation_id,
                    anchor="RELEASE_RESUME_NOOP",
                    at_ms=at_ms,
                )
                return result
            if app["status"] != "ADMITTED":
                raise error(
                    "RELEASE_APPLICATION_STATE",
                    "申请状态不允许释放",
                    details={
                        "application_id": app["application_id"],
                        "status": app["status"],
                    },
                )

            tenant = self._get_tenant(conn, reservation["tenant"])
            pool = conn.execute(
                "SELECT * FROM resource_pools WHERE name = ?",
                (tenant["resource_pool"],),
            ).fetchone()
            obligations = conn.execute(
                """
                SELECT * FROM borrow_obligations
                WHERE application_id = ? AND state = 'OPEN'
                """,
                (app["application_id"],),
            ).fetchall()
            reserved_borrow_mc = int(reservation["borrow_mc"])
            reserved_borrow_mib = int(reservation["borrow_mib"])
            obligation_mc = sum(int(o["outstanding_mc"]) for o in obligations)
            obligation_mib = sum(int(o["outstanding_mib"]) for o in obligations)
            if (
                obligation_mc != reserved_borrow_mc
                or obligation_mib != reserved_borrow_mib
            ):
                raise error(
                    "RELEASE_BORROW_MISMATCH",
                    "借调归还义务与预留记录不平，拒绝释放",
                    details={
                        "application_id": app["application_id"],
                        "reserved_borrow_mc": reserved_borrow_mc,
                        "obligation_mc": obligation_mc,
                        "reserved_borrow_mib": reserved_borrow_mib,
                        "obligation_mib": obligation_mib,
                    },
                )
            if (
                int(tenant["borrowed_mc"]) < reserved_borrow_mc
                or int(tenant["borrowed_mib"]) < reserved_borrow_mib
                or int(pool["borrowed_mc"]) < reserved_borrow_mc
                or int(pool["borrowed_mib"]) < reserved_borrow_mib
            ):
                raise error(
                    "RELEASE_BORROW_MISMATCH",
                    "租户或资源池借调账本不足以归还该预留，拒绝释放",
                    details={
                        "application_id": app["application_id"],
                        "tenant_borrowed": {
                            "millicores": tenant["borrowed_mc"],
                            "mib": tenant["borrowed_mib"],
                        },
                        "pool_borrowed": {
                            "millicores": pool["borrowed_mc"],
                            "mib": pool["borrowed_mib"],
                        },
                        "reservation_borrow": {
                            "millicores": reserved_borrow_mc,
                            "mib": reserved_borrow_mib,
                        },
                    },
                )
            if (
                int(tenant["base_used_mc"]) < int(reservation["base_mc"])
                or int(tenant["base_used_mib"]) < int(reservation["base_mib"])
            ):
                raise error(
                    "RELEASE_QUOTA_MISMATCH",
                    "租户基础配额账本不足以归还该预留，拒绝释放",
                    details={"application_id": app["application_id"]},
                )

            before_base_mc = int(tenant["base_used_mc"])
            before_base_mib = int(tenant["base_used_mib"])
            before_borrowed_mc = int(tenant["borrowed_mc"])
            before_borrowed_mib = int(tenant["borrowed_mib"])
            before_pool_mc = int(pool["borrowed_mc"])
            before_pool_mib = int(pool["borrowed_mib"])
            after_borrow_mc = before_borrowed_mc - reserved_borrow_mc
            after_borrow_mib = before_borrowed_mib - reserved_borrow_mib
            after_pool_mc = before_pool_mc - reserved_borrow_mc
            after_pool_mib = before_pool_mib - reserved_borrow_mib
            after_base_mc = before_base_mc - int(reservation["base_mc"])
            after_base_mib = before_base_mib - int(reservation["base_mib"])

            sequence = 0
            if reserved_borrow_mc or reserved_borrow_mib:
                sequence += 1
                conn.execute(
                    """
                    UPDATE tenants SET borrowed_mc = ?, borrowed_mib = ?,
                        updated_at_ms = ? WHERE name = ?
                    """,
                    (after_borrow_mc, after_borrow_mib, at_ms, tenant["name"]),
                )
                conn.execute(
                    """
                    UPDATE resource_pools SET borrowed_mc = ?,
                        borrowed_mib = ? WHERE name = ?
                    """,
                    (after_pool_mc, after_pool_mib, tenant["resource_pool"]),
                )
                conn.execute(
                    """
                    UPDATE borrow_obligations SET state = 'RETURNED',
                        outstanding_mc = 0, outstanding_mib = 0,
                        returned_at_ms = ?, release_id = ?
                    WHERE application_id = ? AND state = 'OPEN'
                    """,
                    (at_ms, event["release_id"], app["application_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO ledger_entries(
                        transaction_id, operation_id, sequence_in_transaction,
                        tenant, application_id, entry_type, amount_mc, amount_mib,
                        base_used_before_mc, base_used_after_mc,
                        base_used_before_mib, base_used_after_mib,
                        borrowed_before_mc, borrowed_after_mc,
                        borrowed_before_mib, borrowed_after_mib,
                        pool_borrowed_before_mc, pool_borrowed_after_mc,
                        pool_borrowed_before_mib, pool_borrowed_after_mib,
                        declaration_version, at_ms
                    ) VALUES (?, ?, ?, ?, ?, 'BORROW_RETURN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        operation_id,
                        sequence,
                        tenant["name"],
                        app["application_id"],
                        reserved_borrow_mc,
                        reserved_borrow_mib,
                        before_base_mc,
                        before_base_mc,
                        before_base_mib,
                        before_base_mib,
                        before_borrowed_mc,
                        after_borrow_mc,
                        before_borrowed_mib,
                        after_borrow_mib,
                        before_pool_mc,
                        after_pool_mc,
                        before_pool_mib,
                        after_pool_mib,
                        int(reservation["declaration_version"]),
                        at_ms,
                    ),
                )

            sequence += 1
            conn.execute(
                """
                UPDATE tenants SET base_used_mc = ?, base_used_mib = ?,
                    updated_at_ms = ? WHERE name = ?
                """,
                (after_base_mc, after_base_mib, at_ms, tenant["name"]),
            )
            conn.execute(
                """
                INSERT INTO ledger_entries(
                    transaction_id, operation_id, sequence_in_transaction,
                    tenant, application_id, entry_type, amount_mc, amount_mib,
                    base_used_before_mc, base_used_after_mc,
                    base_used_before_mib, base_used_after_mib,
                    borrowed_before_mc, borrowed_after_mc,
                    borrowed_before_mib, borrowed_after_mib,
                    pool_borrowed_before_mc, pool_borrowed_after_mc,
                    pool_borrowed_before_mib, pool_borrowed_after_mib,
                    declaration_version, at_ms
                ) VALUES (?, ?, ?, ?, ?, 'QUOTA_RELEASE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    operation_id,
                    sequence,
                    tenant["name"],
                    app["application_id"],
                    int(reservation["base_mc"]),
                    int(reservation["base_mib"]),
                    before_base_mc,
                    after_base_mc,
                    before_base_mib,
                    after_base_mib,
                    after_borrow_mc,
                    after_borrow_mc,
                    after_borrow_mib,
                    after_borrow_mib,
                    after_pool_mc,
                    after_pool_mc,
                    after_pool_mib,
                    after_pool_mib,
                    int(reservation["declaration_version"]),
                    at_ms,
                ),
            )
            conn.execute(
                """
                UPDATE reservations SET state = 'RELEASED',
                    released_at_ms = ?, release_id = ?
                WHERE application_id = ? AND state = 'ACTIVE'
                """,
                (at_ms, event["release_id"], app["application_id"]),
            )
            conn.execute(
                """
                UPDATE applications SET status = 'RELEASED', reason_code = ?,
                    reason_text = ?, updated_at_ms = ?
                WHERE application_id = ?
                """,
                (
                    payload.get("trigger_type", "MANUAL"),
                    _reason(payload.get("trigger_type", "MANUAL")),
                    at_ms,
                    app["application_id"],
                ),
            )

            wake_attempts, retained_queue = self._wake_queue_locked(
                conn,
                tenant_name=tenant["name"],
                release_id=event["release_id"],
                operation_id=operation_id,
                at_ms=at_ms,
            )
            conn.execute(
                """
                UPDATE release_events SET state = 'COMPLETE',
                    released_at_ms = ?, declaration_version = ?,
                    released_base_mc = ?, released_base_mib = ?,
                    returned_borrow_mc = ?, returned_borrow_mib = ?,
                    wake_json = ?, final_queue_json = ?, completed_at_ms = ?
                WHERE release_id = ?
                """,
                (
                    at_ms,
                    int(reservation["declaration_version"]),
                    int(reservation["base_mc"]),
                    int(reservation["base_mib"]),
                    reserved_borrow_mc,
                    reserved_borrow_mib,
                    canonical_json(wake_attempts),
                    canonical_json(retained_queue),
                    at_ms,
                    event["release_id"],
                ),
            )
            refreshed_event = conn.execute(
                "SELECT * FROM release_events WHERE release_id = ?",
                (event["release_id"],),
            ).fetchone()
            refreshed_app = self._get_application(
                conn, app["application_id"]
            )
            result = {
                "ok": True,
                "duplicate": False,
                "application": dict(refreshed_app),
                "release_event": dict(refreshed_event),
                "wake_attempts": wake_attempts,
                "final_queue": retained_queue,
            }
            self._crashpoint("during_release_apply")
            self._finish_operation(conn, operation_id, result, at_ms)
            self._write_snapshot(
                conn,
                operation_id=operation_id,
                anchor="RELEASE",
                at_ms=at_ms,
            )
            return result

    def _wake_queue_locked(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_name: str,
        release_id: str,
        operation_id: str,
        at_ms: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        queue_rows = conn.execute(
            """
            SELECT q.*, a.* FROM wait_queue q
            JOIN applications a ON a.application_id = q.application_id
            WHERE q.tenant = ?
            ORDER BY q.requested_at_ms, q.sequence
            """,
            (tenant_name,),
        ).fetchall()
        attempts: list[dict[str, Any]] = []
        stop = False
        for rank, row in enumerate(queue_rows, start=1):
            if stop:
                break
            candidate_id = row["application_id"]
            app = self._get_application(conn, candidate_id)
            transaction_id = f"{release_id}:wake:{candidate_id}"
            outcome = self._try_admit_locked(
                conn,
                app,
                at_ms=at_ms,
                operation_id=operation_id,
                transaction_id=transaction_id,
                allow_queue=False,
            )
            if outcome["outcome"] == "ADMITTED":
                attempts.append(
                    {
                        "rank": rank,
                        "application_id": candidate_id,
                        "outcome": "ADMITTED",
                        "reason_code": None,
                        "reason_text": None,
                    }
                )
                conn.execute(
                    "DELETE FROM wait_queue WHERE application_id = ?",
                    (candidate_id,),
                )
                continue
            reason_code = outcome["reason_code"]
            reason_text = outcome.get("reason_text") or _reason(reason_code)
            if outcome["outcome"] == "EXPIRED":
                conn.execute(
                    """
                    UPDATE applications SET status = 'ABANDONED',
                        reason_code = ?, reason_text = ?, updated_at_ms = ?
                    WHERE application_id = ?
                    """,
                    (reason_code, reason_text, at_ms, candidate_id),
                )
                conn.execute(
                    "DELETE FROM wait_queue WHERE application_id = ?",
                    (candidate_id,),
                )
                attempts.append(
                    {
                        "rank": rank,
                        "application_id": candidate_id,
                        "outcome": "BLOCKED",
                        "reason_code": reason_code,
                        "reason_text": reason_text,
                    }
                )
                continue
            attempts.append(
                {
                    "rank": rank,
                    "application_id": candidate_id,
                    "outcome": "BLOCKED",
                    "reason_code": reason_code,
                    "reason_text": reason_text,
                }
            )
            conn.execute(
                """
                UPDATE applications SET reason_code = ?, reason_text = ?,
                    updated_at_ms = ? WHERE application_id = ?
                """,
                (reason_code, reason_text, at_ms, candidate_id),
            )
            conn.execute(
                """
                UPDATE wait_queue SET reason_code = ?, reason_text = ?,
                    last_release_id = ? WHERE application_id = ?
                """,
                (reason_code, reason_text, release_id, candidate_id),
            )
            stop = True

        for attempt in attempts:
            conn.execute(
                """
                INSERT INTO wake_attempts(
                    release_id, tenant, candidate_application_id,
                    queue_rank, outcome, reason_code, reason_text, at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    release_id,
                    tenant_name,
                    attempt["application_id"],
                    attempt["rank"],
                    attempt["outcome"],
                    attempt["reason_code"],
                    attempt["reason_text"],
                    at_ms,
                ),
            )
        retained = conn.execute(
            """
            SELECT q.*, a.status, a.reason_text AS application_reason_text
            FROM wait_queue q JOIN applications a
                ON a.application_id = q.application_id
            WHERE q.tenant = ?
            ORDER BY q.requested_at_ms, q.sequence
            """,
            (tenant_name,),
        ).fetchall()
        return attempts, [dict(row) for row in retained]

    def _release_due_locked(
        self,
        conn: sqlite3.Connection,
        at_ms: int,
        *,
        exclude_application: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT r.application_id, a.sequence
            FROM reservations r JOIN applications a
                ON a.application_id = r.application_id
            WHERE r.state = 'ACTIVE' AND r.expires_at_ms <= ?
            ORDER BY r.expires_at_ms, a.sequence
            """,
            (at_ms,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            if row["application_id"] == exclude_application:
                continue
            app = self._get_application(conn, row["application_id"])
            results.append(
                self._prepare_and_apply_release(
                    conn,
                    app,
                    at_ms=at_ms,
                    trigger_type="EXPIRY",
                )
            )
        return results

    # ------------------------------------------------------------------
    # Resume and recovery
    # ------------------------------------------------------------------
    def resume(self, *, now_ms: int | None = None) -> dict[str, Any]:
        self.initialize_if_needed()
        with self._exclusive():
            conn = self._new_conn()
            try:
                prepared = conn.execute(
                    """
                    SELECT * FROM operations WHERE state = 'PREPARED'
                    ORDER BY created_at_ms, operation_id
                    """
                ).fetchall()
                recovered: list[dict[str, Any]] = []
                failures: list[dict[str, Any]] = []
                for operation in prepared:
                    try:
                        if operation["kind"] == "ADMIT":
                            result = self._apply_admit_operation(
                                conn, operation["operation_id"]
                            )
                        else:
                            result = self._apply_release_operation(
                                conn, operation["operation_id"]
                            )
                        recovered.append(
                            {
                                "operation_id": operation["operation_id"],
                                "kind": operation["kind"],
                                "result": result,
                            }
                        )
                    except BusinessError as exc:
                        failures.append(
                            {
                                "operation_id": operation["operation_id"],
                                "kind": operation["kind"],
                                "error_code": exc.code,
                                "message": exc.message,
                                "details": exc.details,
                            }
                        )
                effective_now = max(
                    self._current_clock(conn),
                    int(now_ms)
                    if now_ms is not None
                    else self._current_clock(conn),
                )
                due = self._release_due_locked(conn, effective_now)
                with transaction(conn):
                    self._advance_clock(conn, effective_now)
                    snapshot = self._write_snapshot(
                        conn,
                        operation_id=f"resume:{effective_now}",
                        anchor="RESUME",
                        at_ms=effective_now,
                    )
                return {
                    "ok": not failures,
                    "logical_clock_ms": effective_now,
                    "recovered_operations": recovered,
                    "recovery_failures": failures,
                    "due_releases": due,
                    "snapshot": snapshot,
                }
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Status and verification
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        self.initialize_if_needed()
        conn = self._new_conn()
        try:
            conn.execute("BEGIN")
            scale = int(self._meta(conn, "fixed_scale", "1000") or 1000)
            tolerance = float(
                self._meta(conn, "precision_tolerance", "0.000001")
                or 0.000001
            )
            tenants: list[dict[str, Any]] = []
            for row in conn.execute(
                "SELECT * FROM tenants ORDER BY name"
            ).fetchall():
                base_used_mc = int(row["base_used_mc"])
                base_used_mib = int(row["base_used_mib"])
                borrowed_mc = int(row["borrowed_mc"])
                borrowed_mib = int(row["borrowed_mib"])
                remaining_mc = int(row["quota_mc"]) - base_used_mc
                remaining_mib = int(row["quota_mib"]) - base_used_mib
                queue_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM wait_queue WHERE tenant = ?",
                    (row["name"],),
                ).fetchone()["n"]
                utilization = {}
                if int(row["quota_mc"]) > 0:
                    up = quantize(
                        "usage_rate_mc",
                        base_used_mc + borrowed_mc,
                        int(row["quota_mc"]),
                        scale,
                        rounding="DOWN",
                    )
                    utilization["mc"] = {
                        "scaled": up.converted,
                        "scale": scale,
                        "rounding": up.rounding,
                        "error_bound_micro": up.error_bound_micro,
                        "value": str(Fraction(up.converted, scale)),
                    }
                else:
                    utilization["mc"] = None
                if int(row["quota_mib"]) > 0:
                    up = quantize(
                        "usage_rate_mib",
                        base_used_mib + borrowed_mib,
                        int(row["quota_mib"]),
                        scale,
                        rounding="DOWN",
                    )
                    utilization["mib"] = {
                        "scaled": up.converted,
                        "scale": scale,
                        "rounding": up.rounding,
                        "error_bound_micro": up.error_bound_micro,
                        "value": str(Fraction(up.converted, scale)),
                    }
                else:
                    utilization["mib"] = None
                tenants.append(
                    {
                        "name": row["name"],
                        "resource_pool": row["resource_pool"],
                        "declaration_version": int(row["current_version"]),
                        "declaration_digest": row["current_digest"],
                        "quota": {
                            "millicores": int(row["quota_mc"]),
                            "mib": int(row["quota_mib"]),
                        },
                        "base_used": {
                            "millicores": base_used_mc,
                            "mib": base_used_mib,
                        },
                        "used": {
                            "millicores": base_used_mc + borrowed_mc,
                            "mib": base_used_mib + borrowed_mib,
                        },
                        "remaining": {
                            "millicores": remaining_mc,
                            "mib": remaining_mib,
                        },
                        "borrowed": {
                            "millicores": borrowed_mc,
                            "mib": borrowed_mib,
                        },
                        "borrow_limit": {
                            "millicores": int(row["borrow_limit_mc"]),
                            "mib": int(row["borrow_limit_mib"]),
                        },
                        "borrow_remaining": {
                            "millicores": int(row["borrow_limit_mc"])
                            - borrowed_mc,
                            "mib": int(row["borrow_limit_mib"]) - borrowed_mib,
                        },
                        "allow_overdraft": bool(row["allow_overdraft"]),
                        "waiting": int(queue_count),
                        "precision_sensitive": bool(row["precision_sensitive"]),
                        "cumulative_error_micro": int(
                            row["cumulative_error_micro"]
                        ),
                        "usage_rate": utilization,
                    }
                )

            pools: list[dict[str, Any]] = []
            for row in conn.execute(
                "SELECT * FROM resource_pools ORDER BY name"
            ).fetchall():
                allocated = conn.execute(
                    """
                    SELECT COALESCE(SUM(quota_mc), 0) AS mc,
                           COALESCE(SUM(quota_mib), 0) AS mib
                    FROM tenants WHERE resource_pool = ?
                    """,
                    (row["name"],),
                ).fetchone()
                pools.append(
                    {
                        "name": row["name"],
                        "capacity": {
                            "millicores": int(row["capacity_mc"]),
                            "mib": int(row["capacity_mib"]),
                        },
                        "allocated_quota": {
                            "millicores": int(allocated["mc"]),
                            "mib": int(allocated["mib"]),
                        },
                        "borrowed": {
                            "millicores": int(row["borrowed_mc"]),
                            "mib": int(row["borrowed_mib"]),
                        },
                        "available_to_lend": {
                            "millicores": int(row["capacity_mc"])
                            - int(allocated["mc"])
                            - int(row["borrowed_mc"]),
                            "mib": int(row["capacity_mib"])
                            - int(allocated["mib"])
                            - int(row["borrowed_mib"]),
                        },
                    }
                )

            services: list[dict[str, Any]] = []
            for row in conn.execute(
                "SELECT * FROM services ORDER BY name"
            ).fetchall():
                active = self._service_active_count(conn, row["name"])
                services.append(
                    {
                        "name": row["name"],
                        "resource_pool": row["resource_pool"],
                        "requests_per_round": int(row["requests_per_round"]),
                        "max_concurrent_reservations": int(
                            row["max_concurrent_reservations"]
                        ),
                        "active_reservations": active,
                    }
                )

            applications = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM applications ORDER BY sequence"
                ).fetchall()
            ]
            queue = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM wait_queue
                    ORDER BY tenant, requested_at_ms, sequence
                    """
                ).fetchall()
            ]
            precision_events = [
                {
                    **dict(row),
                    "chain": _json_load(row["chain_json"], {}),
                }
                for row in conn.execute(
                    """
                    SELECT * FROM precision_events
                    ORDER BY id DESC LIMIT 20
                    """
                ).fetchall()
            ]
            pending_operations = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT operation_id, kind, application_id, state,
                           created_at_ms, updated_at_ms
                    FROM operations WHERE state = 'PREPARED'
                    ORDER BY created_at_ms, operation_id
                    """
                ).fetchall()
            ]
            return {
                "ok": True,
                "logical_clock_ms": self._current_clock(conn),
                "submitted_watermark": self._submitted_watermark(conn),
                "fixed_scale": scale,
                "precision_tolerance": tolerance,
                "precision_sensitive": any(
                    t["precision_sensitive"] for t in tenants
                ),
                "tenants": tenants,
                "resource_pools": pools,
                "services": services,
                "applications": applications,
                "wait_queue": queue,
                "precision_events": precision_events,
                "pending_operations": pending_operations,
            }
        finally:
            conn.rollback()
            conn.close()

    def verify(self) -> dict[str, Any]:
        self.initialize_if_needed()
        conn = self._new_conn()
        issues: list[dict[str, Any]] = []
        checks = 0

        def fail(code: str, message: str, details: dict[str, Any] | None = None) -> None:
            issues.append(
                {"code": code, "message": message, "details": details or {}}
            )

        try:
            conn.execute("BEGIN")
            checks += 1
            tenants = conn.execute(
                "SELECT * FROM tenants ORDER BY name"
            ).fetchall()
            for tenant in tenants:
                checks += 1
                active = conn.execute(
                    """
                    SELECT COALESCE(SUM(base_mc), 0) AS base_mc,
                           COALESCE(SUM(base_mib), 0) AS base_mib,
                           COALESCE(SUM(borrow_mc), 0) AS borrow_mc,
                           COALESCE(SUM(borrow_mib), 0) AS borrow_mib
                    FROM reservations
                    WHERE tenant = ? AND state = 'ACTIVE'
                    """,
                    (tenant["name"],),
                ).fetchone()
                if int(tenant["base_used_mc"]) != int(active["base_mc"]):
                    fail(
                        "TENANT_BASE_MC_MISMATCH",
                        "租户毫核基础账本与活动预留之和不一致",
                        {
                            "tenant": tenant["name"],
                            "ledger": tenant["base_used_mc"],
                            "reservations": active["base_mc"],
                        },
                    )
                if int(tenant["base_used_mib"]) != int(active["base_mib"]):
                    fail(
                        "TENANT_BASE_MIB_MISMATCH",
                        "租户 MiB 基础账本与活动预留之和不一致",
                        {
                            "tenant": tenant["name"],
                            "ledger": tenant["base_used_mib"],
                            "reservations": active["base_mib"],
                        },
                    )
                if int(tenant["borrowed_mc"]) != int(active["borrow_mc"]):
                    fail(
                        "TENANT_BORROW_MC_MISMATCH",
                        "租户毫核借调账本与活动预留之和不一致",
                        {
                            "tenant": tenant["name"],
                            "ledger": tenant["borrowed_mc"],
                            "reservations": active["borrow_mc"],
                        },
                    )
                if int(tenant["borrowed_mib"]) != int(active["borrow_mib"]):
                    fail(
                        "TENANT_BORROW_MIB_MISMATCH",
                        "租户 MiB 借调账本与活动预留之和不一致",
                        {
                            "tenant": tenant["name"],
                            "ledger": tenant["borrowed_mib"],
                            "reservations": active["borrow_mib"],
                        },
                    )
                remaining_mc = int(tenant["quota_mc"]) - int(
                    tenant["base_used_mc"]
                )
                remaining_mib = int(tenant["quota_mib"]) - int(
                    tenant["base_used_mib"]
                )
                borrow_remaining_mc = int(tenant["borrow_limit_mc"]) - int(
                    tenant["borrowed_mc"]
                )
                borrow_remaining_mib = int(tenant["borrow_limit_mib"]) - int(
                    tenant["borrowed_mib"]
                )
                if remaining_mc < 0 or remaining_mib < 0:
                    fail(
                        "NEGATIVE_REMAINING",
                        "租户基础配额余量为负",
                        {"tenant": tenant["name"]},
                    )
                if borrow_remaining_mc < 0 or borrow_remaining_mib < 0:
                    fail(
                        "NEGATIVE_BORROW_REMAINING",
                        "租户借调余额为负",
                        {"tenant": tenant["name"]},
                    )
                version = conn.execute(
                    """
                    SELECT * FROM tenant_versions
                    WHERE tenant_name = ? AND version = ?
                    """,
                    (tenant["name"], tenant["current_version"]),
                ).fetchone()
                if version is None:
                    fail(
                        "TENANT_VERSION_POINTER_MISSING",
                        "当前声明版本指针无对应快照",
                        {"tenant": tenant["name"]},
                    )
                elif (
                    version["digest"] != tenant["current_digest"]
                    or int(version["quota_mc"]) != int(tenant["quota_mc"])
                    or int(version["quota_mib"]) != int(tenant["quota_mib"])
                    or int(version["borrow_limit_mc"])
                    != int(tenant["borrow_limit_mc"])
                    or int(version["borrow_limit_mib"])
                    != int(tenant["borrow_limit_mib"])
                    or int(version["allow_overdraft"])
                    != int(tenant["allow_overdraft"])
                ):
                    fail(
                        "TENANT_VERSION_POINTER_CORRUPT",
                        "当前声明指针与版本快照不一致",
                        {"tenant": tenant["name"]},
                    )

                ledger = conn.execute(
                    """
                    SELECT
                      COALESCE(SUM(CASE WHEN entry_type = 'QUOTA_ADMIT'
                        THEN amount_mc ELSE 0 END), 0)
                      - COALESCE(SUM(CASE WHEN entry_type = 'QUOTA_RELEASE'
                        THEN amount_mc ELSE 0 END), 0) AS base_mc,
                      COALESCE(SUM(CASE WHEN entry_type = 'QUOTA_ADMIT'
                        THEN amount_mib ELSE 0 END), 0)
                      - COALESCE(SUM(CASE WHEN entry_type = 'QUOTA_RELEASE'
                        THEN amount_mib ELSE 0 END), 0) AS base_mib,
                      COALESCE(SUM(CASE WHEN entry_type = 'BORROW_ADMIT'
                        THEN amount_mc ELSE 0 END), 0)
                      - COALESCE(SUM(CASE WHEN entry_type = 'BORROW_RETURN'
                        THEN amount_mc ELSE 0 END), 0) AS borrow_mc,
                      COALESCE(SUM(CASE WHEN entry_type = 'BORROW_ADMIT'
                        THEN amount_mib ELSE 0 END), 0)
                      - COALESCE(SUM(CASE WHEN entry_type = 'BORROW_RETURN'
                        THEN amount_mib ELSE 0 END), 0) AS borrow_mib
                    FROM ledger_entries WHERE tenant = ?
                    """,
                    (tenant["name"],),
                ).fetchone()
                if (
                    int(ledger["base_mc"]) != int(tenant["base_used_mc"])
                    or int(ledger["base_mib"]) != int(tenant["base_used_mib"])
                    or int(ledger["borrow_mc"]) != int(tenant["borrowed_mc"])
                    or int(ledger["borrow_mib"])
                    != int(tenant["borrowed_mib"])
                ):
                    fail(
                        "LEDGER_TENANT_NET_MISMATCH",
                        "审计分录净额与租户账本不一致",
                        {
                            "tenant": tenant["name"],
                            "ledger": dict(ledger),
                        },
                    )

            duplicate_admissions = conn.execute(
                """
                SELECT application_id, COUNT(*) AS n
                FROM ledger_entries WHERE entry_type = 'QUOTA_ADMIT'
                GROUP BY application_id HAVING COUNT(*) > 1
                """
            ).fetchall()
            checks += 1
            for row in duplicate_admissions:
                fail(
                    "DUPLICATE_QUOTA_ADMISSION",
                    "同一申请出现多次配额扣减",
                    dict(row),
                )

            pools = conn.execute(
                "SELECT * FROM resource_pools ORDER BY name"
            ).fetchall()
            for pool in pools:
                checks += 1
                active_borrow = conn.execute(
                    """
                    SELECT COALESCE(SUM(r.borrow_mc), 0) AS mc,
                           COALESCE(SUM(r.borrow_mib), 0) AS mib
                    FROM reservations r JOIN tenants t ON t.name = r.tenant
                    WHERE t.resource_pool = ? AND r.state = 'ACTIVE'
                    """,
                    (pool["name"],),
                ).fetchone()
                if (
                    int(active_borrow["mc"]) != int(pool["borrowed_mc"])
                    or int(active_borrow["mib"])
                    != int(pool["borrowed_mib"])
                ):
                    fail(
                        "POOL_BORROW_MISMATCH",
                        "资源池借调账本与活动预留不一致",
                        {
                            "resource_pool": pool["name"],
                            "ledger": {
                                "millicores": pool["borrowed_mc"],
                                "mib": pool["borrowed_mib"],
                            },
                            "reservations": dict(active_borrow),
                        },
                    )
                allocated = conn.execute(
                    """
                    SELECT COALESCE(SUM(quota_mc), 0) AS mc,
                           COALESCE(SUM(quota_mib), 0) AS mib
                    FROM tenants WHERE resource_pool = ?
                    """,
                    (pool["name"],),
                ).fetchone()
                if (
                    int(allocated["mc"]) + int(pool["borrowed_mc"])
                    > int(pool["capacity_mc"])
                    or int(allocated["mib"]) + int(pool["borrowed_mib"])
                    > int(pool["capacity_mib"])
                ):
                    fail(
                        "POOL_CAPACITY_OVERCOMMITTED",
                        "资源池基础配额与借调量超过容量",
                        {"resource_pool": pool["name"]},
                    )

            open_obligations = conn.execute(
                "SELECT * FROM borrow_obligations WHERE state = 'OPEN' ORDER BY obligation_id"
            ).fetchall()
            checks += 1
            for obligation in open_obligations:
                reservation = conn.execute(
                    """
                    SELECT * FROM reservations
                    WHERE application_id = ? AND state = 'ACTIVE'
                    """,
                    (obligation["application_id"],),
                ).fetchone()
                if reservation is None:
                    fail(
                        "OPEN_BORROW_WITHOUT_RESERVATION",
                        "借调义务没有对应活动预留",
                        {"application_id": obligation["application_id"]},
                    )
                    continue
                if (
                    int(obligation["outstanding_mc"])
                    != int(reservation["borrow_mc"])
                    or int(obligation["outstanding_mib"])
                    != int(reservation["borrow_mib"])
                    or int(obligation["source_available_mc"])
                    < int(obligation["borrow_mc"])
                    or int(obligation["source_available_mib"])
                    < int(obligation["borrow_mib"])
                ):
                    fail(
                        "BORROW_OBLIGATION_MISMATCH",
                        "借调义务余额与预留借调量不一致",
                        {"application_id": obligation["application_id"]},
                    )

            active_without_admit = conn.execute(
                """
                SELECT r.application_id FROM reservations r
                LEFT JOIN ledger_entries l
                  ON l.application_id = r.application_id
                 AND l.entry_type = 'QUOTA_ADMIT'
                WHERE r.state = 'ACTIVE' AND l.id IS NULL
                """
            ).fetchall()
            checks += 1
            for row in active_without_admit:
                fail(
                    "RESERVATION_WITHOUT_QUOTA_DEDUCTION",
                    "活动预留缺少配额扣减分录",
                    dict(row),
                )

            status_checks = conn.execute(
                """
                SELECT a.application_id, a.status,
                       r.state AS reservation_state,
                       q.application_id AS queue_id
                FROM applications a
                LEFT JOIN reservations r ON r.application_id = a.application_id
                LEFT JOIN wait_queue q ON q.application_id = a.application_id
                ORDER BY a.sequence
                """
            ).fetchall()
            checks += 1
            for row in status_checks:
                if row["status"] == "ADMITTED" and row["reservation_state"] != "ACTIVE":
                    fail(
                        "ADMITTED_WITHOUT_ACTIVE_RESERVATION",
                        "已准入申请缺少活动预留",
                        {"application_id": row["application_id"]},
                    )
                if row["status"] == "WAITING" and row["queue_id"] is None:
                    fail(
                        "WAITING_WITHOUT_QUEUE_ROW",
                        "等待申请缺少队列记录",
                        {"application_id": row["application_id"]},
                    )
                if row["status"] not in {"WAITING"} and row["queue_id"] is not None:
                    fail(
                        "QUEUE_ROW_FOR_NON_WAITING_APPLICATION",
                        "非等待申请仍占用等待队列",
                        {"application_id": row["application_id"]},
                    )
                if row["status"] == "RELEASED" and row["reservation_state"] != "RELEASED":
                    fail(
                        "RELEASED_WITHOUT_RELEASED_RESERVATION",
                        "已释放申请没有释放预留记录",
                        {"application_id": row["application_id"]},
                    )

            release_order = conn.execute(
                """
                SELECT transaction_id,
                       SUM(CASE WHEN entry_type = 'BORROW_RETURN' THEN 1 ELSE 0 END) AS b,
                       SUM(CASE WHEN entry_type = 'QUOTA_RELEASE' THEN 1 ELSE 0 END) AS q,
                       MIN(CASE WHEN entry_type = 'BORROW_RETURN'
                           THEN sequence_in_transaction END) AS b_seq,
                       MIN(CASE WHEN entry_type = 'QUOTA_RELEASE'
                           THEN sequence_in_transaction END) AS q_seq
                FROM ledger_entries
                GROUP BY transaction_id
                HAVING b > 0 AND q > 0 AND b_seq > q_seq
                """
            ).fetchall()
            checks += 1
            for row in release_order:
                fail(
                    "BORROW_RETURN_ORDER_VIOLATION",
                    "释放事务未先归还借调再恢复租户余量",
                    dict(row),
                )

            duplicate_ledger = conn.execute(
                """
                SELECT transaction_id, entry_type, COUNT(*) AS n
                FROM ledger_entries
                GROUP BY transaction_id, entry_type
                HAVING COUNT(*) > 1
                """
            ).fetchall()
            checks += 1
            for row in duplicate_ledger:
                fail(
                    "DUPLICATE_LEDGER_ENTRY_TYPE",
                    "同一操作出现重复同类账本分录",
                    dict(row),
                )

            service_limits = conn.execute(
                """
                SELECT s.name, s.max_concurrent_reservations,
                       COUNT(r.application_id) AS active
                FROM services s
                LEFT JOIN reservations r
                  ON r.service = s.name AND r.state = 'ACTIVE'
                GROUP BY s.name
                ORDER BY s.name
                """
            ).fetchall()
            checks += 1
            for row in service_limits:
                if int(row["active"]) > int(row["max_concurrent_reservations"]):
                    fail(
                        "SERVICE_CONCURRENCY_EXCEEDED",
                        "服务活动预留超过并发上限",
                        dict(row),
                    )

            round_limits = conn.execute(
                """
                SELECT a.round_id, a.service, COUNT(*) AS enrolled,
                       s.requests_per_round
                FROM applications a
                JOIN services s ON s.name = a.service
                WHERE a.round_enrolled = 1 AND a.round_id IS NOT NULL
                GROUP BY a.round_id, a.service
                ORDER BY a.round_id, a.service
                """
            ).fetchall()
            checks += 1
            for row in round_limits:
                if int(row["enrolled"]) > int(row["requests_per_round"]):
                    fail(
                        "SERVICE_ROUND_BATCH_EXCEEDED",
                        "服务单轮启动申请数超过批次上限",
                        dict(row),
                    )

            invalid_bindings = conn.execute(
                """
                SELECT r.application_id, r.declaration_version,
                       r.declaration_digest, v.digest AS version_digest
                FROM reservations r
                LEFT JOIN tenant_versions v
                  ON v.tenant_name = r.tenant
                 AND v.version = r.declaration_version
                WHERE v.tenant_name IS NULL
                   OR v.digest != r.declaration_digest
                """
            ).fetchall()
            checks += 1
            for row in invalid_bindings:
                fail(
                    "RESERVATION_VERSION_BINDING_MISSING",
                    "预留绑定的声明版本不存在",
                    dict(row),
                )

            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM operations WHERE state = 'PREPARED'"
            ).fetchone()["n"]
            checks += 1
            if int(pending):
                fail(
                    "PENDING_OPERATIONS",
                    "存在未完成的 PREPARED 操作，需要先执行 resume",
                    {"count": int(pending)},
                )

            latest_snapshot = conn.execute(
                "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest_snapshot is not None and int(pending) == 0:
                checks += 1
                current_digests = self._snapshot_digests(conn)
                current_combined = digest(
                    {
                        "logical_clock_ms": self._current_clock(conn),
                        "submitted_watermark": self._submitted_watermark(conn),
                        **current_digests,
                    }
                )
                if current_combined != latest_snapshot["combined_digest"]:
                    fail(
                        "SNAPSHOT_DIGEST_MISMATCH",
                        "当前账本摘要与最新持久化快照不一致",
                        {
                            "snapshot": latest_snapshot["combined_digest"],
                            "current": current_combined,
                        },
                    )

            conn.rollback()
            return {
                "ok": not issues,
                "checks": checks,
                "issues": issues,
                "logical_clock_ms": self._current_clock(conn),
            }
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def load_documents(
    engine: LedgerEngine,
    tenants_document: Any,
    services_document: Any,
    applications_document: Any,
    *,
    reset: bool = False,
    at_ms: int = 0,
) -> dict[str, Any]:
    tenants, services, applications = parse_bundles(
        tenants_document, services_document, applications_document
    )
    return engine.load_bundle(
        tenants,
        services,
        applications,
        reset=reset,
        at_ms=at_ms,
    )
