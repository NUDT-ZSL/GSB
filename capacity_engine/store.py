from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

from .clock import Schedule
from .errors import BizError
from .ledger import LedgerEntry, scene_config_ledger
from .scene import BatchPlan, Scene
from .util import FileLock, canonical_json, pid_alive


SCHEMA_VERSION = "1"
TERMINAL_UNIT_STATUSES = {
    "COMPLETED",
    "SKIPPED_EARLY_TERMINATION",
    "ABANDONED",
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    unit_key TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    service TEXT NOT NULL,
    tenant TEXT NOT NULL,
    gradient_index INTEGER NOT NULL,
    concurrency INTEGER NOT NULL,
    logical_start_ms INTEGER NOT NULL,
    logical_end_ms INTEGER NOT NULL,
    cost_ms INTEGER NOT NULL,
    window_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    owner_pid INTEGER,
    owner_token TEXT,
    claim_count INTEGER NOT NULL DEFAULT 0,
    budget_reserved_ms INTEGER NOT NULL DEFAULT 0,
    success_rate_scaled INTEGER,
    p95_latency_scaled INTEGER,
    success_ledger_seq INTEGER,
    p95_ledger_seq INTEGER,
    decision_json TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS units_status_order
ON units(status, batch_id, gradient_index, unit_key);

CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_key TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL,
    unit_key TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    service TEXT NOT NULL,
    tenant TEXT NOT NULL,
    gradient_index INTEGER NOT NULL,
    window_index INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    success_count INTEGER NOT NULL,
    failure_count INTEGER NOT NULL,
    success_rate_scaled INTEGER NOT NULL,
    p95_latency_scaled INTEGER,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    ledger_seq_start INTEGER,
    ledger_seq_end INTEGER,
    UNIQUE(unit_key, window_index, version)
);

CREATE INDEX IF NOT EXISTS windows_unit_order
ON windows(unit_key, window_index, version);

CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    local_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    operation TEXT NOT NULL,
    scale INTEGER NOT NULL,
    exact_num INTEGER NOT NULL,
    exact_den INTEGER NOT NULL,
    result_scaled INTEGER NOT NULL,
    discarded_num INTEGER NOT NULL,
    discarded_den INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    conversion_error_num INTEGER NOT NULL,
    conversion_error_den INTEGER NOT NULL,
    cumulative_error_num INTEGER NOT NULL,
    cumulative_error_den INTEGER NOT NULL,
    source TEXT NOT NULL,
    details_json TEXT NOT NULL,
    UNIQUE(scope_key, local_seq)
);

CREATE INDEX IF NOT EXISTS ledger_scope_order
ON ledger(scope_key, local_seq);

CREATE TABLE IF NOT EXISTS request_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_key TEXT NOT NULL,
    unit_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT,
    fields_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS request_errors_window
ON request_errors(window_key, id);

CREATE TABLE IF NOT EXISTS tenant_leases (
    tenant TEXT NOT NULL,
    slot INTEGER NOT NULL,
    unit_key TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    owner_token TEXT NOT NULL,
    PRIMARY KEY(tenant, slot)
);

CREATE TABLE IF NOT EXISTS conclusions (
    batch_id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sensitivity_marks (
    mark_key TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS sensitivity_batch
ON sensitivity_marks(batch_id, mark_key);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")

    def exists(self) -> bool:
        return self.db_path.exists()

    def initialize(self, scene: Scene, schedule: Schedule) -> dict[str, Any]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            conn = self._connect(create=True)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA_SQL)
                conn.execute("BEGIN IMMEDIATE")
                existing_hash = self._meta_get_locked(conn, "scene_sha256")
                if existing_hash is not None:
                    self._assert_same_scene_locked(conn, scene)
                    conn.commit()
                    return {"initialized": False, "resumed": True}
                self._assert_schema_locked(conn)
                meta_values = {
                    "schema_version": SCHEMA_VERSION,
                    "scene_id": scene.scene_id,
                    "scene_sha256": scene.sha256,
                    "scene_json": scene.canonical_text,
                    "global_budget_ms": str(scene.global_logical_time_ms),
                    "budget_used_ms": "0",
                    "logical_clock_ms": str(schedule.logical_start_ms),
                    "logical_clock_high_watermark_ms": str(schedule.logical_start_ms),
                    "total_planned_units": str(len(schedule.units)),
                    "total_planned_requests": str(schedule.total_requests),
                }
                for key, value in sorted(meta_values.items()):
                    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (key, value))
                for unit in schedule.units:
                    conn.execute(
                        """
                        INSERT INTO units(
                            unit_key, batch_id, service, tenant, gradient_index, concurrency,
                            logical_start_ms, logical_end_ms, cost_ms, window_count, status
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                        """,
                        (
                            unit.unit_key,
                            unit.batch_id,
                            unit.service,
                            unit.tenant,
                            unit.gradient_index,
                            unit.concurrency,
                            unit.start_ms,
                            unit.end_ms,
                            unit.cost_ms,
                            unit.window_count,
                        ),
                    )
                entries = scene_config_ledger(scene)
                self._insert_ledger_locked(conn, entries)
                conn.commit()
                return {"initialized": True, "resumed": False}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def load_scene(self) -> Scene:
        from .scene import parse_scene

        with FileLock(self.lock_path):
            conn = self._connect(readonly=True)
            try:
                snapshot = self._meta_get_locked(conn, "scene_json")
            finally:
                conn.close()
        if snapshot is None:
            raise BizError("E_SCHEMA_MISMATCH", "database does not contain a scene snapshot")
        return parse_scene(json.loads(snapshot))

    def reclaim_stale(self, owner_pid: int) -> int:
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                reclaimed = self._reclaim_stale_locked(conn, owner_pid, reclaim_current=True)
                conn.commit()
                return reclaimed
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _reclaim_stale_locked(
        self,
        conn: sqlite3.Connection,
        owner_pid: int,
        reclaim_current: bool = False,
    ) -> int:
        leases = conn.execute(
            "SELECT tenant, slot, unit_key, owner_pid, owner_token FROM tenant_leases ORDER BY tenant, slot"
        ).fetchall()
        stale_tokens: set[str] = set()
        for row in leases:
            pid = int(row["owner_pid"])
            if (reclaim_current and pid == owner_pid) or not pid_alive(pid):
                stale_tokens.add(str(row["owner_token"]))
                conn.execute(
                    "DELETE FROM tenant_leases WHERE tenant=? AND slot=?",
                    (row["tenant"], row["slot"]),
                )
        units = conn.execute(
            "SELECT unit_key, owner_pid, owner_token, status FROM units WHERE status='RUNNING'"
        ).fetchall()
        reclaimed = 0
        for row in units:
            pid = row["owner_pid"]
            token = row["owner_token"]
            if (
                pid is None
                or (reclaim_current and int(pid) == owner_pid)
                or not pid_alive(int(pid))
                or token in stale_tokens
            ):
                conn.execute(
                    """
                    UPDATE units
                    SET status='PENDING', owner_pid=NULL, owner_token=NULL,
                        reason='STALE_OWNER_RECLAIMED'
                    WHERE unit_key=?
                    """,
                    (row["unit_key"],),
                )
                reclaimed += 1
        return reclaimed

    def claim_next_unit(
        self,
        scene: Scene,
        owner_pid: int,
        owner_token: str,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._reclaim_stale_locked(conn, owner_pid, reclaim_current=False)
                batches = {batch.batch_id: batch for batch in scene.batches}
                limits = scene.tenant_concurrency_limits
                rows = conn.execute(
                    """
                    SELECT * FROM units
                    WHERE status='PENDING'
                    ORDER BY batch_id, gradient_index, unit_key
                    """
                ).fetchall()
                if not rows:
                    busy = conn.execute("SELECT COUNT(*) AS n FROM units WHERE status='RUNNING'").fetchone()["n"]
                    conn.commit()
                    return {"unit": None, "state": "busy" if busy else "done"}
                budget_exhausted: list[str] = []
                for row in rows:
                    unit_key = str(row["unit_key"])
                    batch = batches.get(str(row["batch_id"]))
                    if batch is None:
                        self._abandon_locked(conn, unit_key, "UNKNOWN_BATCH")
                        continue
                    if batch.allow_early_termination and self._has_unfinished_predecessor_locked(conn, row):
                        continue
                    tenant = str(row["tenant"])
                    limit = limits.get(tenant, 1)
                    concurrency = int(row["concurrency"])
                    if concurrency > limit:
                        self._abandon_locked(conn, unit_key, "TENANT_CONCURRENCY_LIMIT_EXHAUSTED")
                        continue
                    reserved = int(row["budget_reserved_ms"])
                    if reserved == 0:
                        budget = int(self._meta_get_locked(conn, "global_budget_ms") or "0")
                        used = int(self._meta_get_locked(conn, "budget_used_ms") or "0")
                        cost = int(row["cost_ms"])
                        if used + cost > budget:
                            budget_exhausted.append(unit_key)
                            continue
                        used += cost
                        conn.execute("UPDATE meta SET value=? WHERE key='budget_used_ms'", (str(used),))
                        conn.execute(
                            "UPDATE units SET budget_reserved_ms=? WHERE unit_key=?",
                            (cost, unit_key),
                        )
                        reserved = cost
                    free_slots = self._free_slots_locked(conn, tenant, limit)
                    if len(free_slots) < concurrency:
                        continue
                    selected_slots = free_slots[:concurrency]
                    for slot in selected_slots:
                        conn.execute(
                            """
                            INSERT INTO tenant_leases(tenant, slot, unit_key, owner_pid, owner_token)
                            VALUES(?, ?, ?, ?, ?)
                            """,
                            (tenant, slot, unit_key, owner_pid, owner_token),
                        )
                    conn.execute(
                        """
                        UPDATE units
                        SET status='RUNNING', owner_pid=?, owner_token=?, claim_count=claim_count+1,
                            reason=NULL
                        WHERE unit_key=?
                        """,
                        (owner_pid, owner_token, unit_key),
                    )
                    current_clock = int(
                        self._meta_get_locked(conn, "logical_clock_high_watermark_ms") or "0"
                    )
                    high_watermark = max(current_clock, int(row["logical_end_ms"]))
                    conn.execute(
                        "UPDATE meta SET value=? WHERE key='logical_clock_high_watermark_ms'",
                        (str(high_watermark),),
                    )
                    conn.execute(
                        "UPDATE meta SET value=? WHERE key='logical_clock_ms'",
                        (str(high_watermark),),
                    )
                    claimed = dict(conn.execute("SELECT * FROM units WHERE unit_key=?", (unit_key,)).fetchone())
                    claimed["budget_reserved_ms"] = reserved
                    conn.commit()
                    return {"unit": claimed, "state": "claimed"}
                if budget_exhausted:
                    for unit_key in budget_exhausted:
                        self._abandon_locked(conn, unit_key, "GLOBAL_LOGICAL_BUDGET_EXHAUSTED")
                    pending = conn.execute(
                        "SELECT unit_key FROM units WHERE status='PENDING' AND budget_reserved_ms=0"
                    ).fetchall()
                    for pending_row in pending:
                        self._abandon_locked(
                            conn,
                            str(pending_row["unit_key"]),
                            "GLOBAL_LOGICAL_BUDGET_EXHAUSTED",
                        )
                    conn.commit()
                    return {"unit": None, "state": "budget_exhausted"}
                busy = conn.execute("SELECT COUNT(*) AS n FROM units WHERE status='RUNNING'").fetchone()["n"]
                conn.commit()
                return {"unit": None, "state": "busy" if busy else "blocked"}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def commit_window(
        self,
        unit_key: str,
        summary: dict[str, Any],
        payload: dict[str, Any],
        ledger_entries: list[LedgerEntry],
        version: int = 1,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM windows WHERE window_key=?",
                    (summary["window_key"],),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] == summary["payload_hash"] and int(existing["version"]) == version:
                        conn.commit()
                        return {
                            "committed": False,
                            "idempotent": True,
                            "window_key": summary["window_key"],
                            "ledger_seq_start": existing["ledger_seq_start"],
                            "ledger_seq_end": existing["ledger_seq_end"],
                            "commit_id": existing["id"],
                        }
                    conn.rollback()
                    raise BizError(
                        "E_WINDOW_SUMMARY_MISMATCH",
                        "a different summary already exists for this window version",
                        details={
                            "window_key": summary["window_key"],
                            "version": version,
                            "existing_payload_hash": existing["payload_hash"],
                            "incoming_payload_hash": summary["payload_hash"],
                        },
                    )
                cursor = conn.execute(
                    """
                    INSERT INTO windows(
                        window_key, version, unit_key, batch_id, service, tenant,
                        gradient_index, window_index, start_ms, end_ms, request_count,
                        success_count, failure_count, success_rate_scaled,
                        p95_latency_scaled, payload_hash, payload_json, summary_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary["window_key"],
                        version,
                        unit_key,
                        summary["batch_id"],
                        summary["service"],
                        summary["tenant"],
                        summary["gradient_index"],
                        summary["window_index"],
                        summary["start_ms"],
                        summary["end_ms"],
                        summary["request_count"],
                        summary["success_count"],
                        summary["failure_count"],
                        summary["success_rate_scaled"],
                        summary["p95_latency_scaled"],
                        summary["payload_hash"],
                        canonical_json(payload),
                        canonical_json(summary),
                    ),
                )
                window_id = int(cursor.lastrowid)
                seqs = self._insert_ledger_locked(conn, ledger_entries)
                local_seqs = sorted(entry.local_seq for entry in ledger_entries)
                seq_start = seqs.get(local_seqs[0]) if local_seqs else None
                seq_end = seqs.get(local_seqs[-1]) if local_seqs else None
                summary = dict(summary)
                summary["ledger_seq_start"] = seq_start
                summary["ledger_seq_end"] = seq_end
                summary["ledger_global_seqs"] = [
                    {"local_seq": local_seq, "global_seq": seqs.get(local_seq)}
                    for local_seq in local_seqs
                ]
                conn.execute(
                    "UPDATE windows SET summary_json=?, ledger_seq_start=?, ledger_seq_end=? WHERE id=?",
                    (canonical_json(summary), seq_start, seq_end, window_id),
                )
                for observation in payload.get("observations", []):
                    code = observation.get("error_code")
                    if not code:
                        continue
                    conn.execute(
                        """
                        INSERT INTO request_errors(
                            window_key, unit_key, request_id, code, message, fields_json
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            summary["window_key"],
                            unit_key,
                            observation.get("request_id"),
                            code,
                            observation.get("error_message"),
                            canonical_json(observation.get("error_fields", {})),
                        ),
                    )
                conn.commit()
                return {
                    "committed": True,
                    "idempotent": False,
                    "window_key": summary["window_key"],
                    "ledger_seq_start": seq_start,
                    "ledger_seq_end": seq_end,
                    "commit_id": window_id,
                }
            except BizError:
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def complete_unit(
        self,
        unit_key: str,
        owner_token: str,
        metric_summary: dict[str, Any],
        decision: dict[str, Any],
        ledger_entries: list[LedgerEntry],
        batch: BatchPlan,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                unit = conn.execute(
                    "SELECT * FROM units WHERE unit_key=?",
                    (unit_key,),
                ).fetchone()
                if unit is None or unit["status"] != "RUNNING" or unit["owner_token"] != owner_token:
                    raise BizError(
                        "E_UNIT_NOT_CLAIMED",
                        "unit is not owned by this evaluator",
                        details={"unit_key": unit_key, "owner_token": owner_token},
                    )
                seqs = self._insert_ledger_locked(conn, ledger_entries)
                success_local = metric_summary.get("success_ledger_local_seq")
                p95_local = metric_summary.get("p95_ledger_local_seq")
                success_seq = seqs.get(int(success_local)) if success_local is not None else None
                p95_seq = seqs.get(int(p95_local)) if p95_local is not None else None
                decision_payload = {
                    "metric": metric_summary,
                    "decision": decision,
                    "success_ledger_seq": success_seq,
                    "p95_ledger_seq": p95_seq,
                }
                conn.execute(
                    """
                    UPDATE units
                    SET status='COMPLETED', owner_pid=NULL, owner_token=NULL,
                        success_rate_scaled=?, p95_latency_scaled=?,
                        success_ledger_seq=?, p95_ledger_seq=?, decision_json=?, reason=?
                    WHERE unit_key=?
                    """,
                    (
                        metric_summary.get("success_rate_scaled"),
                        metric_summary.get("p95_latency_scaled"),
                        success_seq,
                        p95_seq,
                        canonical_json(decision_payload),
                        decision.get("status"),
                        unit_key,
                    ),
                )
                conn.execute(
                    "DELETE FROM tenant_leases WHERE unit_key=? AND owner_token=?",
                    (unit_key, owner_token),
                )
                if decision.get("status") == "ABORT" and batch.allow_early_termination:
                    conn.execute(
                        """
                        UPDATE units
                        SET status='SKIPPED_EARLY_TERMINATION',
                            reason='EARLY_TERMINATION_AFTER_ABORT'
                        WHERE batch_id=? AND gradient_index>? AND status='PENDING'
                        """,
                        (batch.batch_id, int(unit["gradient_index"])),
                    )
                conn.commit()
                return {
                    "unit_key": unit_key,
                    "status": "COMPLETED",
                    "decision_status": decision.get("status"),
                    "success_ledger_seq": success_seq,
                    "p95_ledger_seq": p95_seq,
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def abandon_unit(self, unit_key: str, owner_token: str | None, reason: str) -> None:
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if owner_token is None:
                    self._abandon_locked(conn, unit_key, reason)
                else:
                    conn.execute(
                        """
                        UPDATE units
                        SET status='ABANDONED', reason=?, owner_pid=NULL, owner_token=NULL
                        WHERE unit_key=? AND owner_token=?
                        """,
                        (reason, unit_key, owner_token),
                    )
                    conn.execute(
                        "DELETE FROM tenant_leases WHERE unit_key=? AND owner_token=?",
                        (unit_key, owner_token),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def release_unit(self, unit_key: str, owner_token: str, reason: str) -> None:
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE units
                    SET status='PENDING', owner_pid=NULL, owner_token=NULL, reason=?
                    WHERE unit_key=? AND owner_token=? AND status='RUNNING'
                    """,
                    (reason, unit_key, owner_token),
                )
                conn.execute(
                    "DELETE FROM tenant_leases WHERE unit_key=? AND owner_token=?",
                    (unit_key, owner_token),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def persist_batch_analysis(
        self,
        batch_id: str,
        conclusion: dict[str, Any],
        marks: list[dict[str, Any]],
    ) -> None:
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO conclusions(batch_id, data_json) VALUES(?, ?)
                    ON CONFLICT(batch_id) DO UPDATE SET data_json=excluded.data_json
                    """,
                    (batch_id, canonical_json(conclusion)),
                )
                conn.execute("DELETE FROM sensitivity_marks WHERE batch_id=?", (batch_id,))
                for mark in marks:
                    conn.execute(
                        """
                        INSERT INTO sensitivity_marks(mark_key, batch_id, data_json)
                        VALUES(?, ?, ?)
                        """,
                        (mark["mark_key"], batch_id, canonical_json(mark)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def status_snapshot(self) -> dict[str, Any]:
        conn = self._connect(readonly=True)
        try:
            meta = {
                row["key"]: row["value"]
                for row in conn.execute("SELECT key, value FROM meta ORDER BY key")
                if row["key"] != "scene_json"
            }
            units = [dict(row) for row in conn.execute("SELECT * FROM units ORDER BY batch_id, gradient_index, unit_key")]
            counts = {
                row["status"]: int(row["n"])
                for row in conn.execute("SELECT status, COUNT(*) AS n FROM units GROUP BY status ORDER BY status")
            }
            windows = int(conn.execute("SELECT COUNT(*) AS n FROM windows").fetchone()["n"])
            leases = int(conn.execute("SELECT COUNT(*) AS n FROM tenant_leases").fetchone()["n"])
            errors = [
                dict(row)
                for row in conn.execute(
                    "SELECT code, COUNT(*) AS n FROM request_errors GROUP BY code ORDER BY code"
                )
            ]
            return {
                "meta": meta,
                "unit_status_counts": counts,
                "windows_committed": windows,
                "active_tenant_leases": leases,
                "request_error_counts": errors,
                "units": units,
            }
        finally:
            conn.close()

    def committed_window_keys(self, unit_key: str) -> set[str]:
        conn = self._connect(readonly=True)
        try:
            return {
                str(row["window_key"])
                for row in conn.execute(
                    "SELECT window_key FROM windows WHERE unit_key=?",
                    (unit_key,),
                )
            }
        finally:
            conn.close()

    def load_windows_for_unit(self, unit_key: str) -> list[dict[str, Any]]:
        conn = self._connect(readonly=True)
        try:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM windows
                    WHERE unit_key=?
                    ORDER BY window_index, version
                    """,
                    (unit_key,),
                )
            ]
        finally:
            conn.close()

    def report_context(self) -> dict[str, Any]:
        conn = self._connect(readonly=True)
        try:
            meta = {
                row["key"]: row["value"]
                for row in conn.execute("SELECT key, value FROM meta ORDER BY key")
            }
            units = [dict(row) for row in conn.execute("SELECT * FROM units ORDER BY batch_id, gradient_index, unit_key")]
            windows = [dict(row) for row in conn.execute("SELECT * FROM windows ORDER BY unit_key, window_index, version")]
            ledger = [dict(row) for row in conn.execute("SELECT * FROM ledger ORDER BY seq")]
            errors = [dict(row) for row in conn.execute("SELECT * FROM request_errors ORDER BY id")]
            conclusions = [dict(row) for row in conn.execute("SELECT * FROM conclusions ORDER BY batch_id")]
            marks = [dict(row) for row in conn.execute("SELECT * FROM sensitivity_marks ORDER BY mark_key")]
            active_leases = int(
                conn.execute("SELECT COUNT(*) AS n FROM tenant_leases").fetchone()["n"]
            )
            return {
                "meta": meta,
                "units": units,
                "windows": windows,
                "ledger": ledger,
                "errors": errors,
                "conclusions": conclusions,
                "marks": marks,
                "active_leases": active_leases,
            }
        finally:
            conn.close()

    def _abandon_locked(self, conn: sqlite3.Connection, unit_key: str, reason: str) -> None:
        conn.execute(
            """
            UPDATE units
            SET status='ABANDONED', reason=?, owner_pid=NULL, owner_token=NULL
            WHERE unit_key=? AND status='PENDING'
            """,
            (reason, unit_key),
        )
        conn.execute("DELETE FROM tenant_leases WHERE unit_key=?", (unit_key,))

    def _has_unfinished_predecessor_locked(self, conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
        count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM units
            WHERE batch_id=? AND gradient_index<? AND status NOT IN ('COMPLETED', 'SKIPPED_EARLY_TERMINATION', 'ABANDONED')
            """,
            (row["batch_id"], row["gradient_index"]),
        ).fetchone()["n"]
        return int(count) > 0

    def _free_slots_locked(self, conn: sqlite3.Connection, tenant: str, limit: int) -> list[int]:
        occupied = {
            int(row["slot"])
            for row in conn.execute(
                "SELECT slot FROM tenant_leases WHERE tenant=? ORDER BY slot",
                (tenant,),
            )
        }
        return [slot for slot in range(1, limit + 1) if slot not in occupied]

    def _insert_ledger_locked(
        self,
        conn: sqlite3.Connection,
        entries: Iterable[LedgerEntry],
    ) -> dict[int, int]:
        seqs: dict[int, int] = {}
        for entry in entries:
            existing = conn.execute(
                "SELECT seq FROM ledger WHERE scope_key=? AND local_seq=?",
                (entry.scope_key, entry.local_seq),
            ).fetchone()
            if existing is not None:
                seqs[entry.local_seq] = int(existing["seq"])
                continue
            cursor = conn.execute(
                """
                INSERT INTO ledger(
                    scope_key, local_seq, kind, operation, scale, exact_num, exact_den,
                    result_scaled, discarded_num, discarded_den, rounding_direction,
                    conversion_error_num, conversion_error_den,
                    cumulative_error_num, cumulative_error_den, source, details_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.scope_key,
                    entry.local_seq,
                    entry.kind,
                    entry.operation,
                    entry.scale,
                    entry.exact_num,
                    entry.exact_den,
                    entry.result_scaled,
                    entry.discarded.numerator,
                    entry.discarded.denominator,
                    entry.rounding_direction,
                    entry.conversion_error.numerator,
                    entry.conversion_error.denominator,
                    entry.cumulative_error.numerator,
                    entry.cumulative_error.denominator,
                    entry.source,
                    canonical_json(entry.details),
                ),
            )
            seqs[entry.local_seq] = int(cursor.lastrowid)
        return seqs

    def _assert_same_scene_locked(self, conn: sqlite3.Connection, scene: Scene) -> None:
        self._assert_schema_locked(conn)
        schema_version = self._meta_get_locked(conn, "schema_version")
        if schema_version != SCHEMA_VERSION:
            raise BizError(
                "E_SCHEMA_MISMATCH",
                "database schema version does not match this engine",
                details={"expected": SCHEMA_VERSION, "actual": schema_version},
            )
        stored_hash = self._meta_get_locked(conn, "scene_sha256")
        if stored_hash != scene.sha256:
            raise BizError(
                "E_SCENE_MISMATCH",
                "database was initialized with a different scene",
                details={"stored_sha256": stored_hash, "incoming_sha256": scene.sha256},
            )

    def _assert_schema_locked(self, conn: sqlite3.Connection) -> None:
        schema_version = self._meta_get_locked(conn, "schema_version")
        if schema_version is not None and schema_version != SCHEMA_VERSION:
            raise BizError(
                "E_SCHEMA_MISMATCH",
                "database schema version does not match this engine",
                details={"expected": SCHEMA_VERSION, "actual": schema_version},
            )

    def _meta_get_locked(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def _connect(self, create: bool = False, readonly: bool = False) -> sqlite3.Connection:
        if not create and not self.db_path.exists():
            raise BizError(
                "E_DB_NOT_FOUND",
                "database file does not exist",
                details={"path": str(self.db_path)},
            )
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA synchronous=FULL")
        return conn


def new_owner_token() -> str:
    return uuid.uuid4().hex

