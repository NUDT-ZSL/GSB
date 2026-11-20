from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .errors import (
    E_APPLICATION_CONFLICT,
    E_BUILD_BUDGET_EXCEEDED,
    E_CLOCK_REGRESSION,
    E_CONFIG_VERSION_CONFLICT,
    E_CONFIG_VERSION_ROLLBACK,
    E_ENDPOINT_UNHEALTHY,
    E_INVALID_APPLICATION,
    E_LEASE_NOT_FOUND,
    E_SAMPLE_DIGEST_MISMATCH,
    E_STATE_INCONSISTENT,
    E_TENANT_POOL_LABEL,
    E_UNKNOWN_POOL_LABEL,
    PoolError,
    Q_CAPACITY,
    Q_ENDPOINT_CAPACITY,
    Q_ENDPOINT_UNHEALTHY,
    Q_FAIRNESS,
    Q_TENANT_QUOTA,
)
from .model import Application, Catalog, Endpoint, PoolConfig
from .scheduler import decide_mode, estimate_costs, idle_tiers, order_idle
from .storage import Database, FileLock, canonical_json
from .transport import ConnectionHandle, LoopbackTransport, SimulatedTransport
from .validation import parse_config_object, parse_endpoints_object


def application_hash(application: Application) -> str:
    return hashlib.sha256(canonical_json(application.canonical()).encode("utf-8")).hexdigest()


def _maybe_fault(point: str) -> None:
    if os.environ.get("DBPOOL_FAULT_POINT") == point:
        exit_code = int(os.environ.get("DBPOOL_FAULT_EXIT_CODE", "91"))
        os._exit(exit_code)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return bool(int(value))


def _idle_digest(rows: list[dict[str, Any]]) -> str:
    payload = [
        {
            "conn_id": row["conn_id"],
            "endpoint_name": row["endpoint_name"],
            "pool_label": row["pool_label"],
            "tenant": row["tenant"],
            "idle_since_ms": row["idle_since_ms"],
            "generation": row["generation"],
        }
        for row in sorted(rows, key=lambda item: str(item["conn_id"]))
    ]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class Engine:
    def __init__(
        self,
        db_path: str | Path,
        *,
        transport: LoopbackTransport | SimulatedTransport | None = None,
    ) -> None:
        self.db = Database(db_path)
        self.transport = transport if transport is not None else LoopbackTransport()
        self.handles: dict[str, ConnectionHandle] = {}

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()
        self.db.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _state_row(self) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM pool_state WHERE singleton = 1")
        if row is None:
            raise PoolError(
                E_STATE_INCONSISTENT,
                "pool state row is missing",
                operation="state",
            )
        return dict(row)

    def _config(self) -> PoolConfig:
        row = self.db.fetchone(
            "SELECT config_json FROM config_snapshots ORDER BY version DESC LIMIT 1"
        )
        if row is None:
            raise PoolError(
                E_STATE_INCONSISTENT,
                "config snapshot is missing",
                operation="state",
            )
        config, _ = parse_config_object(json.loads(str(row["config_json"])))
        return config

    def _endpoints(self) -> tuple[Endpoint, ...]:
        rows = self.db.fetchall(
            "SELECT endpoint_json FROM endpoint_snapshots ORDER BY name"
        )
        raw = [json.loads(str(row["endpoint_json"])) for row in rows]
        endpoints, _ = parse_endpoints_object({"version": 1, "endpoints": raw})
        return endpoints

    def _quota_map(self) -> dict[str, dict[str, Any]]:
        return {
            str(row["tenant"]): dict(row)
            for row in self.db.fetchall("SELECT * FROM quota_accounts ORDER BY tenant")
        }

    def _idle_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT conn_id, tenant, pool_label, endpoint_name, idle_since_ms,
                       created_at_ms, generation
                FROM connections
                WHERE state = 'IDLE'
                ORDER BY idle_since_ms, endpoint_name, conn_id
                """
            )
        ]

    def _pending_queue_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT * FROM queue_entries
                WHERE status = 'PENDING'
                ORDER BY position
                """
            )
        ]

    def _abort_stale_intents_locked(
        self,
        now_ms: int,
        *,
        preserve_intent_id: str | None = None,
    ) -> int:
        rows = self.db.fetchall(
            "SELECT intent_id FROM operation_intents WHERE state = 'OPENING'"
        )
        aborted = 0
        for row in rows:
            intent_id = str(row["intent_id"])
            if preserve_intent_id is not None and intent_id == preserve_intent_id:
                continue
            self.db.execute(
                """
                UPDATE operation_intents
                SET state = 'ABORTED', updated_at_ms = ?, detail_json = ?
                WHERE intent_id = ? AND state = 'OPENING'
                """,
                (
                    now_ms,
                    canonical_json(
                        {
                            "reason": "STALE_OPENING_INTENT",
                            "note": "no committed lease references this build intent",
                        }
                    ),
                    intent_id,
                ),
            )
            aborted += 1
        return aborted

    def _record_event_locked(
        self,
        now_ms: int,
        kind: str,
        payload: dict[str, Any],
        *,
        app_seq: int | None = None,
        app_version: int | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO events(logical_clock_ms, kind, app_seq, app_version, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (now_ms, kind, app_seq, app_version, canonical_json(payload)),
        )

    def _recompute_counts_locked(self) -> None:
        state = self._state_row()
        connection_counts = self.db.fetchone(
            """
            SELECT
                SUM(CASE WHEN state != 'CLOSED' THEN 1 ELSE 0 END) AS allocated,
                SUM(CASE WHEN state = 'IN_USE' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN state = 'IDLE' THEN 1 ELSE 0 END) AS idle
            FROM connections
            """
        )
        total_allocated = int(connection_counts["allocated"] or 0)
        total_active = int(connection_counts["active"] or 0)
        total_idle = int(connection_counts["idle"] or 0)
        queue_depth = int(
            self.db.fetchone(
                "SELECT COUNT(*) AS count FROM queue_entries WHERE status = 'PENDING'"
            )["count"]
        )

        quota_rows = self.db.fetchall(
            """
            SELECT tenant,
                   SUM(CASE WHEN state != 'CLOSED' THEN 1 ELSE 0 END) AS allocated,
                   SUM(CASE WHEN state = 'IN_USE' THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN state = 'IDLE' THEN 1 ELSE 0 END) AS idle
            FROM connections
            GROUP BY tenant
            """
        )
        derived = {
            str(row["tenant"]): (
                int(row["allocated"] or 0),
                int(row["active"] or 0),
                int(row["idle"] or 0),
            )
            for row in quota_rows
        }
        label_rows = self.db.fetchall(
            """
            SELECT tenant, pool_label
            FROM connections
            WHERE state != 'CLOSED'
            GROUP BY tenant, pool_label
            ORDER BY tenant, pool_label
            """
        )
        derived_labels: dict[str, set[str]] = {}
        for row in label_rows:
            derived_labels.setdefault(str(row["tenant"]), set()).add(
                str(row["pool_label"])
            )
        for tenant, labels in self._config().tenant_pool_labels.items():
            derived_labels.setdefault(tenant, set()).update(labels)
        existing = self._quota_map()
        for tenant in sorted(set(existing) | set(derived)):
            allocated, active, idle = derived.get(tenant, (0, 0, 0))
            labels_json = canonical_json(sorted(derived_labels.get(tenant, set())))
            if tenant in existing:
                self.db.execute(
                    """
                    UPDATE quota_accounts
                    SET allocated = ?, active = ?, idle = ?,
                        pool_labels_json = ?
                    WHERE tenant = ?
                    """,
                    (allocated, active, idle, labels_json, tenant),
                )
            else:
                self.db.execute(
                    """
                    INSERT INTO quota_accounts(
                        tenant, pool_labels_json, allocated, active, idle
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (tenant, labels_json, allocated, active, idle),
                )

        digest = _idle_digest(self._idle_rows())
        self.db.execute(
            """
            UPDATE pool_state
            SET total_allocated = ?, total_active = ?, total_idle = ?,
                queue_depth = ?, idle_digest = ?
            WHERE singleton = 1
            """,
            (
                total_allocated,
                total_active,
                total_idle,
                queue_depth,
                digest,
            ),
        )

    def _finalize_state_locked(
        self,
        now_ms: int,
        *,
        watermark: int | None = None,
        bump: bool = True,
    ) -> None:
        self._recompute_counts_locked()
        state = self._state_row()
        next_watermark = (
            max(int(state["committed_app_watermark"]), watermark)
            if watermark is not None
            else int(state["committed_app_watermark"])
        )
        version = int(state["state_version"]) + (1 if bump else 0)
        self.db.execute(
            """
            UPDATE pool_state
            SET state_version = ?, logical_clock_ms = ?,
                committed_app_watermark = ?
            WHERE singleton = 1
            """,
            (version, now_ms, next_watermark),
        )

    def _evaluate_mode_locked(self, now_ms: int) -> dict[str, Any] | None:
        state = self._state_row()
        config = self._config()
        idle_rows = self._idle_rows()
        queue_depth = len(self._pending_queue_rows())
        costs = estimate_costs(
            idle_rows,
            queue_depth=queue_depth,
            bucket_width_ms=config.bucket_width_ms,
            now_ms=now_ms,
            age_tolerance_ms=config.idle_age_tolerance_ms,
        )
        current = str(state["mode"])
        new_mode, reason = decide_mode(
            current,
            costs,
            margin_ratio=config.switch_margin_ratio,
            tiered_min_idle=config.tiered_min_idle,
        )
        if reason is None:
            return None
        trigger = {
            "idle_count": costs["idle_count"],
            "queue_depth": costs["queue_depth"],
            "pool_label_span": costs["pool_label_span"],
            "occupied_buckets": costs["occupied_buckets"],
            "tiered_min_idle": config.tiered_min_idle,
        }
        self.db.execute(
            """
            UPDATE pool_state
            SET mode = ?, mode_changed_at_ms = ?
            WHERE singleton = 1
            """,
            (new_mode, now_ms),
        )
        cursor = self.db.execute(
            """
            INSERT INTO mode_switches(
                at_clock_ms, from_mode, to_mode, trigger_json,
                cost_json, rejected_mode, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_ms,
                current,
                new_mode,
                canonical_json(trigger),
                canonical_json(costs),
                current,
                reason,
            ),
        )
        event = {
            "switch_id": int(cursor.lastrowid),
            "from_mode": current,
            "to_mode": new_mode,
            "trigger": trigger,
            "cost": costs,
            "rejected_mode": current,
            "reason": reason,
        }
        self._record_event_locked(now_ms, "MODE_SWITCH", event)
        return event

    def _close_idle_locked(
        self,
        row: dict[str, Any],
        now_ms: int,
        reason: str,
    ) -> None:
        self.db.execute(
            """
            UPDATE connections
            SET state = 'CLOSED', idle_since_ms = NULL, closed_at_ms = ?,
                close_reason = ?, current_lease_id = NULL
            WHERE conn_id = ? AND state = 'IDLE'
            """,
            (now_ms, reason, row["conn_id"]),
        )
        handle = self.handles.pop(str(row["conn_id"]), None)
        if handle is not None:
            handle.close()
        self._record_event_locked(
            now_ms,
            "CONNECTION_CLOSED",
            {
                "conn_id": row["conn_id"],
                "tenant": row["tenant"],
                "pool_label": row["pool_label"],
                "endpoint_name": row["endpoint_name"],
                "reason": reason,
            },
        )

    def _reap_idle_locked(
        self,
        now_ms: int,
        config: PoolConfig,
    ) -> list[dict[str, Any]]:
        idle_rows = self._idle_rows()
        closed: list[dict[str, Any]] = []
        idle_count = len(idle_rows)
        expired = [
            row
            for row in idle_rows
            if int(row["idle_since_ms"]) + config.idle_timeout_ms <= now_ms
        ]
        expired.sort(
            key=lambda row: (
                int(row["idle_since_ms"]),
                str(row["endpoint_name"]),
                str(row["conn_id"]),
            )
        )
        for row in expired:
            if idle_count <= config.min_idle:
                break
            self._close_idle_locked(row, now_ms, "IDLE_TIMEOUT")
            idle_count -= 1
            closed.append(
                {
                    "conn_id": row["conn_id"],
                    "tenant": row["tenant"],
                    "reason": "IDLE_TIMEOUT",
                }
            )
        return closed

    def _shrink_quota_locked(
        self,
        config: PoolConfig,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        quotas = self._quota_map()
        for tenant in sorted(quotas):
            allocated = int(quotas[tenant]["allocated"])
            while allocated > config.per_tenant_quota:
                idles = order_idle(
                    [
                        row
                        for row in self._idle_rows()
                        if str(row["tenant"]) == tenant
                    ],
                    now_ms=now_ms,
                    mode="exact",
                    bucket_width_ms=config.bucket_width_ms,
                )
                if not idles:
                    break
                row = dict(idles[0])
                self._close_idle_locked(row, now_ms, "QUOTA_SHRINK")
                allocated -= 1
                closed.append(
                    {
                        "conn_id": row["conn_id"],
                        "tenant": tenant,
                        "reason": "QUOTA_SHRINK",
                    }
                )
                quotas = self._quota_map()
        state = self._state_row()
        while int(state["total_allocated"]) > config.max_total_connections:
            idles = order_idle(
                self._idle_rows(),
                now_ms=now_ms,
                mode="exact",
                bucket_width_ms=config.bucket_width_ms,
            )
            if not idles:
                break
            row = dict(idles[0])
            self._close_idle_locked(row, now_ms, "QUOTA_SHRINK")
            closed.append(
                {
                    "conn_id": row["conn_id"],
                    "tenant": row["tenant"],
                    "reason": "QUOTA_SHRINK",
                }
            )
            self._recompute_counts_locked()
            state = self._state_row()
        return closed

    def _choose_endpoint_locked(
        self,
        endpoints: tuple[Endpoint, ...],
        pool_label: str,
    ) -> tuple[Endpoint | None, str | None]:
        supporting = [item for item in endpoints if pool_label in item.pool_labels]
        if not supporting:
            raise PoolError(
                E_UNKNOWN_POOL_LABEL,
                "no endpoint serves the requested pool label",
                operation="acquire",
                pool_label=pool_label,
            )
        healthy = sorted(
            [item for item in supporting if item.healthy],
            key=lambda item: (item.build_latency_ms, item.name),
        )
        if not healthy:
            return None, Q_ENDPOINT_UNHEALTHY
        counts = {
            str(row["endpoint_name"]): int(row["count"])
            for row in self.db.fetchall(
                """
                SELECT endpoint_name, COUNT(*) AS count
                FROM connections
                WHERE state != 'CLOSED'
                GROUP BY endpoint_name
                """
            )
        }
        for endpoint in healthy:
            if counts.get(endpoint.name, 0) < endpoint.max_connections:
                return endpoint, None
        return None, Q_ENDPOINT_CAPACITY

    def _preflight_locked(
        self,
        application: Application,
        now_ms: int,
    ) -> dict[str, Any]:
        state = self._state_row()
        config = self._config()
        endpoints = self._endpoints()
        if now_ms < int(state["logical_clock_ms"]):
            raise PoolError(
                E_CLOCK_REGRESSION,
                "logical clock cannot move backwards",
                operation="acquire",
                current_clock_ms=state["logical_clock_ms"],
                requested_clock_ms=now_ms,
            )
        if now_ms < application.apply_clock_ms:
            raise PoolError(
                E_CLOCK_REGRESSION,
                "acquire clock precedes the application apply clock",
                operation="acquire",
                seq=application.seq,
                apply_clock_ms=application.apply_clock_ms,
                requested_clock_ms=now_ms,
            )
        request = self.db.fetchone(
            "SELECT * FROM requests WHERE app_seq = ? AND app_version = ?",
            (application.seq, application.version),
        )
        if request is not None:
            return {"action": "existing", "request": dict(request)}
        if self._pending_queue_rows():
            return {"action": "queue", "reason": Q_FAIRNESS}
        state = self._state_row()
        quota = self.db.fetchone(
            "SELECT * FROM quota_accounts WHERE tenant = ?",
            (application.tenant,),
        )
        allocated = int(quota["allocated"]) if quota is not None else 0
        idles = [
            row
            for row in self._idle_rows()
            if str(row["tenant"]) == application.tenant
            and str(row["pool_label"]) == application.pool_label
        ]
        if idles:
            return {"action": "reuse"}
        if int(state["total_allocated"]) >= config.max_total_connections:
            return {"action": "queue", "reason": Q_CAPACITY}
        if allocated >= config.per_tenant_quota:
            return {"action": "queue", "reason": Q_TENANT_QUOTA}
        endpoint, reason = self._choose_endpoint_locked(
            endpoints,
            application.pool_label,
        )
        if endpoint is None:
            return {"action": "queue", "reason": reason}
        if endpoint.build_latency_ms > config.round_budget_ms:
            raise PoolError(
                E_BUILD_BUDGET_EXCEEDED,
                "endpoint build latency exceeds the per-round logical budget",
                operation="acquire",
                endpoint=endpoint.name,
                build_latency_ms=endpoint.build_latency_ms,
                round_budget_ms=config.round_budget_ms,
            )
        return {"action": "new", "endpoint": endpoint}

    def _create_intent_locked(
        self,
        application: Application,
        endpoint: Endpoint,
        now_ms: int,
    ) -> str:
        intent_id = (
            f"intent-{application.seq:08d}-{application.version:08d}-{now_ms:012d}"
        )
        detail = {
            "tenant": application.tenant,
            "pool_label": application.pool_label,
            "address": endpoint.address,
        }
        self.db.execute(
            """
            INSERT INTO operation_intents(
                intent_id, app_seq, app_version, endpoint_name, address,
                state, created_at_ms, updated_at_ms, detail_json
            ) VALUES (?, ?, ?, ?, ?, 'OPENING', ?, ?, ?)
            ON CONFLICT(intent_id) DO UPDATE SET
                state = 'OPENING',
                updated_at_ms = excluded.updated_at_ms,
                detail_json = excluded.detail_json
            """,
            (
                intent_id,
                application.seq,
                application.version,
                endpoint.name,
                endpoint.address,
                now_ms,
                now_ms,
                canonical_json(detail),
            ),
        )
        return intent_id

    def _set_intent_state_locked(
        self,
        intent_id: str,
        state: str,
        now_ms: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE operation_intents
            SET state = ?, updated_at_ms = ?, detail_json = ?
            WHERE intent_id = ?
            """,
            (state, now_ms, canonical_json(detail or {}), intent_id),
        )

    def _next_connection_id_locked(self) -> str:
        state = self._state_row()
        conn_seq = int(state["next_connection_seq"])
        self.db.execute(
            """
            UPDATE pool_state
            SET next_connection_seq = ?
            WHERE singleton = 1
            """,
            (conn_seq + 1,),
        )
        return f"conn-{conn_seq:08d}"

    def _next_lease_id_locked(self) -> str:
        state = self._state_row()
        lease_seq = int(state["next_lease_seq"])
        self.db.execute(
            """
            UPDATE pool_state
            SET next_lease_seq = ?
            WHERE singleton = 1
            """,
            (lease_seq + 1,),
        )
        return f"lease-{lease_seq:08d}"

    def _lease_dict_locked(self, lease_id: str) -> dict[str, Any]:
        row = self.db.fetchone(
            "SELECT * FROM leases WHERE lease_id = ?",
            (lease_id,),
        )
        if row is None:
            raise PoolError(
                E_STATE_INCONSISTENT,
                "lease row is missing after allocation",
                operation="acquire",
                lease_id=lease_id,
            )
        result = dict(row)
        result["reused"] = _as_bool(result["reused"])
        return result

    def _issue_lease_locked(
        self,
        application: Application,
        *,
        now_ms: int,
        endpoint_name: str,
        conn_id: str,
        build_latency_ms: int,
        reused: bool,
    ) -> dict[str, Any]:
        lease_id = self._next_lease_id_locked()
        self.db.execute(
            """
            UPDATE connections
            SET state = 'IN_USE', idle_since_ms = NULL, current_lease_id = ?,
                closed_at_ms = NULL, close_reason = NULL,
                owner_instance = ?
            WHERE conn_id = ?
            """,
            (lease_id, "local", conn_id),
        )
        self.db.execute(
            """
            INSERT INTO leases(
                lease_id, app_seq, app_version, tenant, pool_label,
                endpoint_name, conn_id, apply_clock_ms, issued_at_ms,
                hold_ms, build_latency_ms, reused, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
            """,
            (
                lease_id,
                application.seq,
                application.version,
                application.tenant,
                application.pool_label,
                endpoint_name,
                conn_id,
                application.apply_clock_ms,
                now_ms,
                application.hold_ms,
                build_latency_ms,
                1 if reused else 0,
            ),
        )
        self.db.execute(
            """
            UPDATE requests
            SET status = 'LEASED', lease_id = ?, queue_reason = NULL
            WHERE app_seq = ? AND app_version = ?
            """,
            (lease_id, application.seq, application.version),
        )
        self.db.execute(
            """
            UPDATE queue_entries
            SET status = 'SATISFIED', lease_id = ?
            WHERE app_seq = ? AND app_version = ? AND status = 'PENDING'
            """,
            (lease_id, application.seq, application.version),
        )
        payload = {
            "lease_id": lease_id,
            "conn_id": conn_id,
            "tenant": application.tenant,
            "pool_label": application.pool_label,
            "endpoint_name": endpoint_name,
            "issued_at_ms": now_ms,
            "build_latency_ms": build_latency_ms,
            "reused": reused,
        }
        self._record_event_locked(
            now_ms,
            "LEASE_ISSUED",
            payload,
            app_seq=application.seq,
            app_version=application.version,
        )
        return self._lease_dict_locked(lease_id)

    def _reuse_idle_locked(
        self,
        application: Application,
        *,
        now_ms: int,
        config: PoolConfig,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        idles = [
            row
            for row in self._idle_rows()
            if str(row["tenant"]) == application.tenant
            and str(row["pool_label"]) == application.pool_label
        ]
        if not idles:
            return None
        ordered = order_idle(
            idles,
            now_ms=now_ms,
            mode=str(state["mode"]),
            bucket_width_ms=config.bucket_width_ms,
        )
        row = dict(ordered[0])
        return self._issue_lease_locked(
            application,
            now_ms=now_ms,
            endpoint_name=str(row["endpoint_name"]),
            conn_id=str(row["conn_id"]),
            build_latency_ms=0,
            reused=True,
        )

    def _new_connection_locked(
        self,
        application: Application,
        *,
        now_ms: int,
        config: PoolConfig,
        endpoint: Endpoint,
        prepared_handle: ConnectionHandle | None,
    ) -> dict[str, Any]:
        if endpoint.build_latency_ms > config.round_budget_ms:
            raise PoolError(
                E_BUILD_BUDGET_EXCEEDED,
                "endpoint build latency exceeds the per-round logical budget",
                operation="acquire",
                endpoint=endpoint.name,
                build_latency_ms=endpoint.build_latency_ms,
                round_budget_ms=config.round_budget_ms,
            )
        handle = prepared_handle
        if handle is None:
            handle = self.transport.open(
                endpoint,
                budget_ms=config.round_budget_ms,
            )
        conn_id = self._next_connection_id_locked()
        self.handles[conn_id] = handle
        self.db.execute(
            """
            INSERT INTO connections(
                conn_id, endpoint_name, pool_label, tenant, state,
                created_at_ms, idle_since_ms, closed_at_ms, close_reason,
                current_lease_id, build_latency_ms, owner_instance, generation
            ) VALUES (?, ?, ?, ?, 'IN_USE', ?, NULL, NULL, NULL, NULL, ?, ?, 1)
            """,
            (
                conn_id,
                endpoint.name,
                application.pool_label,
                application.tenant,
                now_ms,
                endpoint.build_latency_ms,
                "local",
            ),
        )
        return self._issue_lease_locked(
            application,
            now_ms=now_ms,
            endpoint_name=endpoint.name,
            conn_id=conn_id,
            build_latency_ms=endpoint.build_latency_ms,
            reused=False,
        )

    def _enqueue_locked(
        self,
        application: Application,
        *,
        now_ms: int,
        reason: str,
    ) -> dict[str, Any]:
        next_position = int(
            self.db.fetchone(
                "SELECT COALESCE(MAX(position), 0) + 1 AS position FROM queue_entries"
            )["position"]
        )
        self.db.execute(
            """
            INSERT INTO queue_entries(
                app_seq, app_version, position, tenant, pool_label,
                apply_clock_ms, hold_ms, enqueued_at_ms, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (
                application.seq,
                application.version,
                next_position,
                application.tenant,
                application.pool_label,
                application.apply_clock_ms,
                application.hold_ms,
                now_ms,
            ),
        )
        self.db.execute(
            """
            UPDATE requests
            SET status = 'QUEUED', queue_reason = ?
            WHERE app_seq = ? AND app_version = ?
            """,
            (reason, application.seq, application.version),
        )
        self._record_event_locked(
            now_ms,
            "APPLICATION_QUEUED",
            {
                "tenant": application.tenant,
                "pool_label": application.pool_label,
                "position": next_position,
                "reason": reason,
            },
            app_seq=application.seq,
            app_version=application.version,
        )
        return {
            "status": "QUEUED",
            "app_seq": application.seq,
            "app_version": application.version,
            "tenant": application.tenant,
            "pool_label": application.pool_label,
            "queue_position": next_position,
            "reason": reason,
            "idempotent": False,
        }

    def _allocate_or_queue_locked(
        self,
        application: Application,
        *,
        now_ms: int,
        prepared_endpoint: Endpoint | None = None,
        prepared_handle: ConnectionHandle | None = None,
        prepared_intent_id: str | None = None,
        enqueue_if_blocked: bool = True,
    ) -> dict[str, Any]:
        config = self._config()
        state = self._state_row()
        lease = self._reuse_idle_locked(
            application,
            now_ms=now_ms,
            config=config,
            state=state,
        )
        if lease is not None:
            if prepared_intent_id is not None:
                self._set_intent_state_locked(
                    prepared_intent_id,
                    "ABORTED",
                    now_ms,
                    {"reason": "IDLE_REUSE_WON"},
                )
            if prepared_handle is not None:
                prepared_handle.close()
            return {"status": "LEASED", "lease": lease, "idempotent": False}

        if int(state["total_allocated"]) >= config.max_total_connections:
            if not enqueue_if_blocked:
                return {"status": "BLOCKED", "reason": Q_CAPACITY}
            result = self._enqueue_locked(
                application,
                now_ms=now_ms,
                reason=Q_CAPACITY,
            )
            self._abort_prepared_locked(
                now_ms,
                prepared_intent_id,
                prepared_handle,
                "QUEUE_CAPACITY",
            )
            return result

        quota = self.db.fetchone(
            "SELECT * FROM quota_accounts WHERE tenant = ?",
            (application.tenant,),
        )
        allocated = int(quota["allocated"]) if quota is not None else 0
        if allocated >= config.per_tenant_quota:
            if not enqueue_if_blocked:
                return {"status": "BLOCKED", "reason": Q_TENANT_QUOTA}
            result = self._enqueue_locked(
                application,
                now_ms=now_ms,
                reason=Q_TENANT_QUOTA,
            )
            self._abort_prepared_locked(
                now_ms,
                prepared_intent_id,
                prepared_handle,
                "QUEUE_TENANT_QUOTA",
            )
            return result

        chosen = prepared_endpoint
        if chosen is None:
            chosen, reason = self._choose_endpoint_locked(
                self._endpoints(),
                application.pool_label,
            )
            if chosen is None:
                if not enqueue_if_blocked:
                    return {
                        "status": "BLOCKED",
                        "reason": reason or Q_ENDPOINT_UNHEALTHY,
                    }
                result = self._enqueue_locked(
                    application,
                    now_ms=now_ms,
                    reason=reason or Q_ENDPOINT_UNHEALTHY,
                )
                self._abort_prepared_locked(
                    now_ms,
                    prepared_intent_id,
                    prepared_handle,
                    "QUEUE_ENDPOINT_UNAVAILABLE",
                )
                return result
        if chosen.build_latency_ms > config.round_budget_ms:
            raise PoolError(
                E_BUILD_BUDGET_EXCEEDED,
                "endpoint build latency exceeds the per-round logical budget",
                operation="acquire",
                endpoint=chosen.name,
                build_latency_ms=chosen.build_latency_ms,
                round_budget_ms=config.round_budget_ms,
            )
        lease = self._new_connection_locked(
            application,
            now_ms=now_ms,
            config=config,
            endpoint=chosen,
            prepared_handle=prepared_handle,
        )
        if prepared_intent_id is not None:
            self._set_intent_state_locked(
                prepared_intent_id,
                "COMMITTED",
                now_ms,
                {"lease_id": lease["lease_id"], "conn_id": lease["conn_id"]},
            )
        return {"status": "LEASED", "lease": lease, "idempotent": False}

    def _abort_prepared_locked(
        self,
        now_ms: int,
        intent_id: str | None,
        handle: ConnectionHandle | None,
        reason: str,
    ) -> None:
        if handle is not None:
            handle.close()
        if intent_id is not None:
            self._set_intent_state_locked(
                intent_id,
                "ABORTED",
                now_ms,
                {"reason": reason},
            )

    def _discard_uncommitted_handles_locked(self) -> None:
        for conn_id in list(self.handles):
            row = self.db.fetchone(
                "SELECT 1 AS present FROM connections WHERE conn_id = ?",
                (conn_id,),
            )
            if row is None:
                handle = self.handles.pop(conn_id, None)
                if handle is not None:
                    handle.close()

    def _existing_result_locked(
        self,
        application: Application,
    ) -> dict[str, Any] | None:
        request = self.db.fetchone(
            "SELECT * FROM requests WHERE app_seq = ? AND app_version = ?",
            (application.seq, application.version),
        )
        if request is None:
            return None
        if str(request["content_hash"]) != application_hash(application):
            raise PoolError(
                E_APPLICATION_CONFLICT,
                "application seq/version was already submitted with different content",
                operation="acquire",
                seq=application.seq,
                version=application.version,
            )
        lease_id = request["lease_id"]
        if lease_id is not None:
            return {
                "status": "LEASED",
                "lease": self._lease_dict_locked(str(lease_id)),
                "idempotent": True,
            }
        queue = self.db.fetchone(
            "SELECT * FROM queue_entries WHERE app_seq = ? AND app_version = ?",
            (application.seq, application.version),
        )
        if queue is not None and str(queue["status"]) == "PENDING":
            return {
                "status": "QUEUED",
                "app_seq": application.seq,
                "app_version": application.version,
                "tenant": application.tenant,
                "pool_label": application.pool_label,
                "queue_position": int(queue["position"]),
                "reason": str(request["queue_reason"] or Q_FAIRNESS),
                "idempotent": True,
            }
        raise PoolError(
            E_STATE_INCONSISTENT,
            "request exists without a lease or pending queue entry",
            operation="acquire",
            seq=application.seq,
            request_status=request["status"],
        )

    def _dispatch_queue_locked(
        self,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        issued: list[dict[str, Any]] = []
        while True:
            # Mutations performed earlier in this transaction (for example an
            # active close or an idle reaper pass) must be reflected before
            # checking capacity and tenant quota for the next queue entry.
            self._recompute_counts_locked()
            pending = self._pending_queue_rows()
            if not pending:
                break
            item = pending[0]
            application = Application(
                seq=int(item["app_seq"]),
                version=int(item["app_version"]),
                tenant=str(item["tenant"]),
                pool_label=str(item["pool_label"]),
                apply_clock_ms=int(item["apply_clock_ms"]),
                hold_ms=int(item["hold_ms"]),
                release=True,
            )
            try:
                result = self._allocate_or_queue_locked(
                    application,
                    now_ms=now_ms,
                    enqueue_if_blocked=False,
                )
            except PoolError as exc:
                self.db.execute(
                    """
                    UPDATE queue_entries
                    SET last_error_code = ?, last_error_at_ms = ?
                    WHERE app_seq = ? AND app_version = ? AND status = 'PENDING'
                    """,
                    (
                        exc.code,
                        now_ms,
                        application.seq,
                        application.version,
                    ),
                )
                self._record_event_locked(
                    now_ms,
                    "QUEUE_ISSUE_FAILED",
                    {"code": exc.code, "message": exc.message},
                    app_seq=application.seq,
                    app_version=application.version,
                )
                break
            if result["status"] != "LEASED":
                break
            issued.append(result["lease"])
        return issued

    def _advance_locked(
        self,
        now_ms: int,
        *,
        preserve_intent_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._state_row()
        if now_ms < int(state["logical_clock_ms"]):
            raise PoolError(
                E_CLOCK_REGRESSION,
                "logical clock cannot move backwards",
                operation="advance",
                current_clock_ms=state["logical_clock_ms"],
                requested_clock_ms=now_ms,
            )
        config = self._config()
        aborted = self._abort_stale_intents_locked(
            now_ms,
            preserve_intent_id=preserve_intent_id,
        )
        closed = self._reap_idle_locked(now_ms, config)
        self._recompute_counts_locked()
        closed.extend(self._shrink_quota_locked(config, now_ms))
        self._evaluate_mode_locked(now_ms)
        issued = self._dispatch_queue_locked(now_ms)
        self._finalize_state_locked(now_ms)
        return {
            "closed": closed,
            "dispatched": issued,
            "aborted_intents": aborted,
        }

    def advance(self, now_ms: int) -> dict[str, Any]:
        with FileLock(self.db.lock_path):
            with self.db.transaction():
                result = self._advance_locked(now_ms)
            return result

    def current_clock(self) -> int:
        return int(self._state_row()["logical_clock_ms"])

    def acquire(self, application: Application, now_ms: int) -> dict[str, Any]:
        with FileLock(self.db.lock_path):
            preflight = self._preflight_locked(application, now_ms)
            prepared_endpoint: Endpoint | None = preflight.get("endpoint")
            prepared_handle: ConnectionHandle | None = None
            intent_id: str | None = None
            if preflight["action"] == "new":
                endpoint = prepared_endpoint
                assert endpoint is not None
                with self.db.transaction():
                    intent_id = self._create_intent_locked(application, endpoint, now_ms)
                _maybe_fault("after_intent_before_connect")
                try:
                    prepared_handle = self.transport.open(
                        endpoint,
                        budget_ms=self._config().round_budget_ms,
                    )
                except BaseException:
                    with self.db.transaction():
                        self._set_intent_state_locked(
                            intent_id,
                            "ABORTED",
                            now_ms,
                            {"reason": "BUILD_FAILED"},
                        )
                    raise
                _maybe_fault("after_connect_before_commit")

            try:
                with self.db.transaction():
                    advanced = self._advance_locked(
                        now_ms,
                        preserve_intent_id=intent_id,
                    )
                    existing = self._existing_result_locked(application)
                    if existing is not None:
                        if intent_id is not None:
                            self._set_intent_state_locked(
                                intent_id,
                                "ABORTED",
                                now_ms,
                                {"reason": "IDEMPOTENT_REQUEST"},
                            )
                        if prepared_handle is not None:
                            prepared_handle.close()
                        self._finalize_state_locked(now_ms)
                        result = existing
                        result["dispatched"] = advanced["dispatched"]
                        result["closed"] = advanced["closed"]
                    else:
                        request = self.db.fetchone(
                            "SELECT * FROM requests WHERE app_seq = ? AND app_version = ?",
                            (application.seq, application.version),
                        )
                        if request is None:
                            self.db.execute(
                                """
                                INSERT INTO requests(
                                    app_seq, app_version, tenant, pool_label,
                                    apply_clock_ms, hold_ms, release_flag,
                                    content_hash, status, submitted_at_ms
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTED', ?)
                                """,
                                (
                                    application.seq,
                                    application.version,
                                    application.tenant,
                                    application.pool_label,
                                    application.apply_clock_ms,
                                    application.hold_ms,
                                    1 if application.release else 0,
                                    application_hash(application),
                                    now_ms,
                                ),
                            )
                        self._evaluate_mode_locked(now_ms)
                        result = self._allocate_or_queue_locked(
                            application,
                            now_ms=now_ms,
                            prepared_endpoint=prepared_endpoint,
                            prepared_handle=prepared_handle,
                            prepared_intent_id=intent_id,
                        )
                        result["dispatched"] = advanced["dispatched"]
                        result["closed"] = advanced["closed"]
                        self._finalize_state_locked(
                            now_ms,
                            watermark=application.seq,
                        )
                    _maybe_fault("after_rows_before_commit")
                _maybe_fault("after_commit_before_ack")
                return result
            except BaseException:
                if prepared_handle is not None:
                    prepared_handle.close()
                self._discard_uncommitted_handles_locked()
                if intent_id is not None:
                    try:
                        with self.db.transaction():
                            self._set_intent_state_locked(
                                intent_id,
                                "ABORTED",
                                now_ms,
                                {"reason": "UNCOMMITTED_ACQUIRE"},
                            )
                    except BaseException:
                        pass
                raise

    def release(
        self,
        app_seq: int,
        app_version: int,
        now_ms: int,
        *,
        close: bool = False,
        reason: str = "HOLD_EXPIRED",
    ) -> dict[str, Any]:
        with FileLock(self.db.lock_path):
            with self.db.transaction():
                advanced = self._advance_locked(now_ms)
                receipt = self.db.fetchone(
                    """
                    SELECT * FROM release_receipts
                    WHERE app_seq = ? AND app_version = ?
                    """,
                    (app_seq, app_version),
                )
                if receipt is not None:
                    lease = self._lease_dict_locked(str(receipt["lease_id"]))
                    receipt_payload = dict(receipt)
                    receipt_payload["closed"] = _as_bool(receipt_payload["closed"])
                    self._finalize_state_locked(now_ms)
                    return {
                        "status": "RELEASED",
                        "lease": lease,
                        "receipt": receipt_payload,
                        "dispatched": advanced["dispatched"],
                        "closed": advanced["closed"],
                        "idempotent": True,
                    }
                lease_row = self.db.fetchone(
                    "SELECT * FROM leases WHERE app_seq = ? AND app_version = ?",
                    (app_seq, app_version),
                )
                if lease_row is None:
                    raise PoolError(
                        E_LEASE_NOT_FOUND,
                        "lease does not exist for the application",
                        operation="release",
                        seq=app_seq,
                        version=app_version,
                    )
                lease = dict(lease_row)
                if str(lease["status"]) != "ACTIVE":
                    raise PoolError(
                        E_STATE_INCONSISTENT,
                        "lease is released but has no release receipt",
                        operation="release",
                        seq=app_seq,
                        version=app_version,
                    )
                conn_id = str(lease["conn_id"])
                if close:
                    self.db.execute(
                        """
                        UPDATE connections
                        SET state = 'CLOSED', idle_since_ms = NULL,
                            closed_at_ms = ?, close_reason = 'ACTIVE_CLOSE',
                            current_lease_id = NULL
                        WHERE conn_id = ? AND state = 'IN_USE'
                        """,
                        (now_ms, conn_id),
                    )
                    handle = self.handles.pop(conn_id, None)
                    if handle is not None:
                        handle.close()
                    self._record_event_locked(
                        now_ms,
                        "CONNECTION_CLOSED",
                        {
                            "conn_id": conn_id,
                            "tenant": lease["tenant"],
                            "pool_label": lease["pool_label"],
                            "endpoint_name": lease["endpoint_name"],
                            "reason": "ACTIVE_CLOSE",
                        },
                    )
                else:
                    self.db.execute(
                        """
                        UPDATE connections
                        SET state = 'IDLE', idle_since_ms = ?,
                            current_lease_id = NULL
                        WHERE conn_id = ? AND state = 'IN_USE'
                        """,
                        (now_ms, conn_id),
                    )
                self.db.execute(
                    """
                    UPDATE leases
                    SET status = 'RELEASED', released_at_ms = ?, release_reason = ?
                    WHERE lease_id = ?
                    """,
                    (now_ms, "ACTIVE_CLOSE" if close else reason, lease["lease_id"]),
                )
                self.db.execute(
                    """
                    UPDATE requests
                    SET status = 'RELEASED'
                    WHERE app_seq = ? AND app_version = ?
                    """,
                    (app_seq, app_version),
                )
                cursor = self.db.execute(
                    """
                    INSERT INTO release_receipts(
                        app_seq, app_version, release_clock_ms, reason, closed,
                        lease_id, conn_id, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        app_seq,
                        app_version,
                        now_ms,
                        "ACTIVE_CLOSE" if close else reason,
                        1 if close else 0,
                        lease["lease_id"],
                        conn_id,
                        now_ms,
                    ),
                )
                del cursor
                self._record_event_locked(
                    now_ms,
                    "LEASE_RELEASED",
                    {
                        "lease_id": lease["lease_id"],
                        "conn_id": conn_id,
                        "closed": close,
                        "reason": "ACTIVE_CLOSE" if close else reason,
                    },
                    app_seq=app_seq,
                    app_version=app_version,
                )
                self._recompute_counts_locked()
                quota_closed = self._shrink_quota_locked(self._config(), now_ms)
                self._evaluate_mode_locked(now_ms)
                dispatched = self._dispatch_queue_locked(now_ms)
                self._finalize_state_locked(now_ms)
                _maybe_fault("release_after_state_before_commit")
                result = {
                    "status": "RELEASED",
                    "lease": self._lease_dict_locked(str(lease["lease_id"])),
                    "receipt": {
                        "app_seq": app_seq,
                        "app_version": app_version,
                        "release_clock_ms": now_ms,
                        "reason": "ACTIVE_CLOSE" if close else reason,
                        "closed": close,
                        "lease_id": lease["lease_id"],
                        "conn_id": conn_id,
                    },
                    "dispatched": advanced["dispatched"] + dispatched,
                    "closed": advanced["closed"] + quota_closed,
                    "idempotent": False,
                }
            _maybe_fault("after_commit_before_ack")
            return result

    def _status_locked(self) -> dict[str, Any]:
        state = self._state_row()
        config = self._config()
        idle_rows = self._idle_rows()
        mode = str(state["mode"])
        ordered = order_idle(
            idle_rows,
            now_ms=int(state["logical_clock_ms"]),
            mode=mode,
            bucket_width_ms=config.bucket_width_ms,
        )
        costs = estimate_costs(
            idle_rows,
            queue_depth=int(state["queue_depth"]),
            bucket_width_ms=config.bucket_width_ms,
            now_ms=int(state["logical_clock_ms"]),
            age_tolerance_ms=config.idle_age_tolerance_ms,
        )
        active_leases = [
            {**dict(row), "reused": _as_bool(row["reused"])}
            for row in self.db.fetchall(
                "SELECT * FROM leases WHERE status = 'ACTIVE' ORDER BY app_seq, app_version"
            )
        ]
        released_leases = [
            {**dict(row), "reused": _as_bool(row["reused"])}
            for row in self.db.fetchall(
                """
                SELECT * FROM leases WHERE status = 'RELEASED'
                ORDER BY app_seq, app_version
                """
            )
        ]
        connections = [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM connections ORDER BY conn_id"
            )
        ]
        quotas = [
            {
                **{
                    key: value
                    for key, value in dict(row).items()
                    if key != "pool_labels_json"
                },
                "pool_labels": json.loads(str(row["pool_labels_json"])),
            }
            for row in self.db.fetchall(
                "SELECT * FROM quota_accounts ORDER BY tenant"
            )
        ]
        queue = [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM queue_entries ORDER BY position"
            )
        ]
        switches = [
            {
                **dict(row),
                "trigger": json.loads(str(row["trigger_json"])),
                "cost": json.loads(str(row["cost_json"])),
            }
            for row in self.db.fetchall(
                "SELECT * FROM mode_switches ORDER BY switch_id"
            )
        ]
        intents = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT * FROM operation_intents
                WHERE state = 'OPENING'
                ORDER BY created_at_ms, intent_id
                """
            )
        ]
        close_counts = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT close_reason, COUNT(*) AS count
                FROM connections
                WHERE state = 'CLOSED'
                GROUP BY close_reason
                ORDER BY close_reason
                """
            )
        ]
        events = [
            {
                **dict(row),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in self.db.fetchall(
                "SELECT * FROM events ORDER BY event_id"
            )
        ]
        status: dict[str, Any] = {
            "ok": True,
            "config_version": int(state["config_version"]),
            "logical_clock_ms": int(state["logical_clock_ms"]),
            "state_version": int(state["state_version"]),
            "committed_app_watermark": int(state["committed_app_watermark"]),
            "watermark": {
                "total_allocated": int(state["total_allocated"]),
                "total_active": int(state["total_active"]),
                "total_idle": int(state["total_idle"]),
                "queue_depth": int(state["queue_depth"]),
                "idle_digest": str(state["idle_digest"]),
            },
            "algorithm": {
                "mode": mode,
                "mode_changed_at_ms": int(state["mode_changed_at_ms"]),
                "bucket_width_ms": config.bucket_width_ms,
                "cost_estimate": costs,
                "switches": switches,
            },
            "active_leases": active_leases,
            "released_leases": released_leases,
            "leases": active_leases + released_leases,
            "idle_queue": {
                "algorithm": mode,
                "ordered_connection_ids": [str(row["conn_id"]) for row in ordered],
                "tiers": idle_tiers(
                    idle_rows,
                    now_ms=int(state["logical_clock_ms"]),
                    bucket_width_ms=config.bucket_width_ms,
                ),
            },
            "quota_accounts": quotas,
            "connections": connections,
            "queue": queue,
            "pending_intents": intents,
            "closed_by_reason": close_counts,
            "events": events,
        }
        digest_payload = {
            "state": state,
            "active_leases": active_leases,
            "released_leases": released_leases,
            "quota_accounts": quotas,
            "connections": connections,
            "queue": queue,
            "switches": switches,
            "events": events,
        }
        status["state_digest"] = hashlib.sha256(
            canonical_json(digest_payload).encode("utf-8")
        ).hexdigest()
        return status

    def status(self, *, at_clock_ms: int | None = None) -> dict[str, Any]:
        if at_clock_ms is not None:
            with FileLock(self.db.lock_path):
                with self.db.transaction():
                    self._advance_locked(at_clock_ms)
        return self._status_locked()

    def resume(self, *, now_ms: int | None = None) -> dict[str, Any]:
        with FileLock(self.db.lock_path):
            with self.db.transaction():
                state = self._state_row()
                resume_clock = int(state["logical_clock_ms"]) if now_ms is None else now_ms
                if resume_clock < int(state["logical_clock_ms"]):
                    raise PoolError(
                        E_CLOCK_REGRESSION,
                        "logical clock cannot move backwards",
                        operation="resume",
                        current_clock_ms=state["logical_clock_ms"],
                        requested_clock_ms=resume_clock,
                    )
                aborted = self._abort_stale_intents_locked(resume_clock)
                repairs = 0
                active_leases = [
                    dict(row)
                    for row in self.db.fetchall(
                        "SELECT * FROM leases WHERE status = 'ACTIVE' ORDER BY lease_id"
                    )
                ]
                for lease in active_leases:
                    conn = self.db.fetchone(
                        "SELECT * FROM connections WHERE conn_id = ?",
                        (lease["conn_id"],),
                    )
                    if conn is None:
                        self.db.execute(
                            """
                            INSERT INTO connections(
                                conn_id, endpoint_name, pool_label, tenant, state,
                                created_at_ms, idle_since_ms, closed_at_ms,
                                close_reason, current_lease_id, build_latency_ms,
                                owner_instance, generation
                            ) VALUES (?, ?, ?, ?, 'IN_USE', ?, NULL, NULL, NULL, ?, ?, NULL, 1)
                            """,
                            (
                                lease["conn_id"],
                                lease["endpoint_name"],
                                lease["pool_label"],
                                lease["tenant"],
                                lease["issued_at_ms"],
                                lease["lease_id"],
                                lease["build_latency_ms"],
                            ),
                        )
                        repairs += 1
                    elif str(conn["state"]) == "CLOSED":
                        self.db.execute(
                            """
                            UPDATE connections
                            SET state = 'IN_USE', idle_since_ms = NULL,
                                closed_at_ms = NULL, close_reason = NULL,
                                current_lease_id = ?
                            WHERE conn_id = ?
                            """,
                            (lease["lease_id"], lease["conn_id"]),
                        )
                        repairs += 1
                    elif str(conn["current_lease_id"] or "") != str(lease["lease_id"]):
                        self.db.execute(
                            """
                            UPDATE connections
                            SET state = 'IN_USE', idle_since_ms = NULL,
                                current_lease_id = ?
                            WHERE conn_id = ?
                            """,
                            (lease["lease_id"], lease["conn_id"]),
                        )
                        repairs += 1
                orphan_rows = self.db.fetchall(
                    """
                    SELECT c.conn_id
                    FROM connections c
                    LEFT JOIN leases l
                      ON l.lease_id = c.current_lease_id AND l.status = 'ACTIVE'
                    WHERE c.state = 'IN_USE' AND l.lease_id IS NULL
                    """
                )
                for row in orphan_rows:
                    self.db.execute(
                        """
                        UPDATE connections
                        SET state = 'IDLE', idle_since_ms = ?,
                            current_lease_id = NULL
                        WHERE conn_id = ?
                        """,
                        (resume_clock, row["conn_id"]),
                    )
                    repairs += 1
                repairs += self._abort_stale_intents_locked(resume_clock)
                self._recompute_counts_locked()
                self._shrink_quota_locked(self._config(), resume_clock)
                self._finalize_state_locked(resume_clock)
                _maybe_fault("resume_after_repair_before_commit")
            logical_status = self._status_locked()

        rebuilt = 0
        rebuild_errors: list[dict[str, Any]] = []
        config = self._config()
        endpoints = {endpoint.name: endpoint for endpoint in self._endpoints()}
        for conn in logical_status["connections"]:
            if conn["state"] == "CLOSED":
                continue
            endpoint = endpoints.get(str(conn["endpoint_name"]))
            if endpoint is None:
                rebuild_errors.append(
                    {
                        "conn_id": conn["conn_id"],
                        "code": E_STATE_INCONSISTENT,
                        "message": "endpoint snapshot is missing",
                    }
                )
                continue
            try:
                handle = self.transport.open(
                    endpoint,
                    budget_ms=config.round_budget_ms,
                )
            except PoolError as exc:
                rebuild_errors.append(
                    {
                        "conn_id": conn["conn_id"],
                        "code": exc.code,
                        "endpoint": endpoint.name,
                    }
                )
                continue
            old = self.handles.pop(str(conn["conn_id"]), None)
            if old is not None:
                old.close()
            self.handles[str(conn["conn_id"])] = handle
            rebuilt += 1
        with FileLock(self.db.lock_path):
            with self.db.transaction():
                self.db.execute(
                    """
                    INSERT INTO recovery_runs(
                        at_clock_ms, repaired_rows, aborted_intents,
                        physical_rebuilt, detail_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        resume_clock,
                        repairs,
                        aborted,
                        rebuilt,
                        canonical_json(
                            {
                                "rebuild_errors": rebuild_errors,
                                "quota_recomputed_not_incremented": True,
                            }
                        ),
                    ),
                )
                self._finalize_state_locked(resume_clock)
        logical_status = self._status_locked()
        logical_status["resume"] = {
            "repaired_rows": repairs,
            "aborted_intents": aborted,
            "physical_rebuilt": rebuilt,
            "rebuild_errors": rebuild_errors,
        }
        return logical_status


def initialize_database(
    db_path: str | Path,
    catalog: Catalog,
    *,
    expected_digest: str | None = None,
    initial_mode: str | None = None,
    now_ms: int = 0,
) -> dict[str, Any]:
    if expected_digest is not None and expected_digest != catalog.sample_digest:
        raise PoolError(
            E_SAMPLE_DIGEST_MISMATCH,
            "sample digest does not match --expect-digest",
            operation="load",
            expected=expected_digest,
            actual=catalog.sample_digest,
        )
    if initial_mode is not None and initial_mode not in {"exact", "tiered"}:
        raise PoolError(
            E_INVALID_APPLICATION,
            "initial mode must be exact or tiered",
            operation="load",
            mode=initial_mode,
        )
    db = Database(db_path, create=True)
    lock_path = db.lock_path
    db.close()
    with FileLock(lock_path):
        db = Database(db_path, create=False)
        try:
            with db.transaction():
                state_row = db.fetchone(
                    "SELECT * FROM pool_state WHERE singleton = 1"
                )
                if state_row is not None:
                    current_version = int(state_row["config_version"])
                    if catalog.config.version < current_version:
                        raise PoolError(
                            E_CONFIG_VERSION_ROLLBACK,
                            "pool config version cannot move backwards",
                            operation="load",
                            current_version=current_version,
                            requested_version=catalog.config.version,
                        )
                    stored_config_digest = db.fetchone(
                        """
                        SELECT digest FROM config_snapshots
                        WHERE version = ?
                        """,
                        (current_version,),
                    )
                    if (
                        catalog.config.version == current_version
                        and stored_config_digest is not None
                        and str(stored_config_digest["digest"]) != catalog.config_digest
                    ):
                        raise PoolError(
                            E_CONFIG_VERSION_CONFLICT,
                            "same config version has different content",
                            operation="load",
                            version=current_version,
                            stored_digest=str(stored_config_digest["digest"]),
                            requested_digest=catalog.config_digest,
                        )
                    stored_sample_digest = str(state_row["sample_digest"])
                    if (
                        catalog.config.version == current_version
                        and stored_sample_digest != catalog.sample_digest
                    ):
                        raise PoolError(
                            E_SAMPLE_DIGEST_MISMATCH,
                            "sample digest differs from the loaded state",
                            operation="load",
                            stored=stored_sample_digest,
                            actual=catalog.sample_digest,
                        )
                    changed = False
                    if catalog.config.version > current_version:
                        db.execute(
                            """
                            INSERT INTO config_snapshots(
                                version, digest, config_json, applied_at_ms
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                catalog.config.version,
                                catalog.config_digest,
                                canonical_json(catalog.config.canonical()),
                                now_ms,
                            ),
                        )
                        db.execute(
                            "DELETE FROM endpoint_snapshots"
                        )
                        _store_endpoints(db, catalog)
                        _store_trace(db, catalog)
                        db.execute(
                            """
                            UPDATE pool_state
                            SET sample_digest = ?, config_version = ?,
                                mode = CASE WHEN ? = 'auto' THEN mode ELSE ? END
                            WHERE singleton = 1
                            """,
                            (
                                catalog.sample_digest,
                                catalog.config.version,
                                catalog.config.mode,
                                catalog.config.mode,
                            ),
                        )
                        engine = Engine.__new__(Engine)
                        engine.db = db
                        engine.transport = SimulatedTransport()
                        engine.handles = {}
                        closed = engine._shrink_quota_locked(catalog.config, now_ms)
                        engine._record_event_locked(
                            now_ms,
                            "CONFIG_APPLIED",
                            {
                                "version": catalog.config.version,
                                "quota_shrink_closed": closed,
                            },
                        )
                        engine._finalize_state_locked(now_ms)
                        changed = True
                    if changed:
                        result = {
                            "status": "CONFIG_UPDATED",
                            "config_version": catalog.config.version,
                            "sample_digest": catalog.sample_digest,
                        }
                    else:
                        result = {
                            "status": "ALREADY_LOADED",
                            "config_version": current_version,
                            "sample_digest": stored_sample_digest,
                        }
                else:
                    mode = initial_mode or (
                        catalog.config.mode
                        if catalog.config.mode in {"exact", "tiered"}
                        else "exact"
                    )
                    db.execute(
                        """
                        INSERT INTO pool_state(
                            singleton, state_version, committed_app_watermark,
                            logical_clock_ms, total_allocated, total_active,
                            total_idle, queue_depth, idle_digest, sample_digest,
                            config_version, mode, mode_changed_at_ms,
                            next_connection_seq, next_lease_seq
                        ) VALUES (1, 1, 0, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?, 1, 1)
                        """,
                        (
                            now_ms,
                            _idle_digest([]),
                            catalog.sample_digest,
                            catalog.config.version,
                            mode,
                            now_ms,
                        ),
                    )
                    db.execute(
                        """
                        INSERT INTO config_snapshots(
                            version, digest, config_json, applied_at_ms
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            catalog.config.version,
                            catalog.config_digest,
                            canonical_json(catalog.config.canonical()),
                            now_ms,
                        ),
                    )
                    _store_endpoints(db, catalog)
                    _store_trace(db, catalog)
                    for tenant, labels in sorted(catalog.tenant_pool_labels.items()):
                        db.execute(
                            """
                            INSERT INTO quota_accounts(
                                tenant, pool_labels_json, allocated, active, idle
                            ) VALUES (?, ?, 0, 0, 0)
                            """,
                            (tenant, canonical_json(list(labels))),
                        )
                    db.execute(
                        """
                        INSERT INTO events(
                            logical_clock_ms, kind, payload_json
                        ) VALUES (?, 'POOL_LOADED', ?)
                        """,
                        (
                            now_ms,
                            canonical_json(
                                {
                                    "config_version": catalog.config.version,
                                    "sample_digest": catalog.sample_digest,
                                    "mode": mode,
                                }
                            ),
                        ),
                    )
                    result = {
                        "status": "LOADED",
                        "config_version": catalog.config.version,
                        "sample_digest": catalog.sample_digest,
                        "mode": mode,
                    }
            return result
        finally:
            db.close()


def _store_endpoints(db: Database, catalog: Catalog) -> None:
    for endpoint in catalog.endpoints:
        db.execute(
            """
            INSERT INTO endpoint_snapshots(
                name, address, host, port, max_connections,
                build_latency_ms, healthy, pool_labels_json, endpoint_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                endpoint.name,
                endpoint.address,
                endpoint.host,
                endpoint.port,
                endpoint.max_connections,
                endpoint.build_latency_ms,
                1 if endpoint.healthy else 0,
                canonical_json(list(endpoint.pool_labels)),
                canonical_json(endpoint.canonical()),
            ),
        )


def _store_trace(db: Database, catalog: Catalog) -> None:
    db.execute("DELETE FROM trace_applications")
    for application in catalog.trace.applications:
        db.execute(
            """
            INSERT INTO trace_applications(
                app_seq, app_version, tenant, pool_label, apply_clock_ms,
                hold_ms, release_flag, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                application.seq,
                application.version,
                application.tenant,
                application.pool_label,
                application.apply_clock_ms,
                application.hold_ms,
                1 if application.release else 0,
                application_hash(application),
            ),
        )
