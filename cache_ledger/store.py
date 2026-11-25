from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .errors import CacheLedgerError, ClockRollbackError
from .lock import FileLock
from .models import LAYERS, SampleBundle

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
    name TEXT PRIMARY KEY,
    max_blocks INTEGER NOT NULL,
    soft_quota_blocks INTEGER NOT NULL,
    hard_quota_blocks INTEGER NOT NULL,
    soft_ratio REAL NOT NULL,
    hard_ratio REAL NOT NULL,
    eviction_priority_weight REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    seq INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    tenant TEXT NOT NULL,
    declared_layer TEXT NOT NULL,
    block_size INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    last_access_ms INTEGER NOT NULL,
    access_count INTEGER NOT NULL,
    digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    status_reason TEXT,
    current_layer TEXT,
    policy_source TEXT,
    policy_generation INTEGER NOT NULL DEFAULT 0,
    admitted_decision_seq INTEGER,
    updated_decision_seq INTEGER,
    last_change_clock_ms INTEGER,
    last_migration_clock_ms INTEGER,
    migration_count INTEGER NOT NULL DEFAULT 0,
    eviction_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_tenant_status ON entries(tenant, status, seq);
CREATE INDEX IF NOT EXISTS idx_entries_layer_status ON entries(current_layer, status);
CREATE TABLE IF NOT EXISTS tenant_usage (
    tenant TEXT PRIMARY KEY,
    used_blocks INTEGER NOT NULL DEFAULT 0,
    used_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS layer_usage (
    layer TEXT PRIMARY KEY,
    used_blocks INTEGER NOT NULL DEFAULT 0,
    used_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
    seq INTEGER PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    tenant TEXT,
    entry_key TEXT,
    clock_ms INTEGER NOT NULL,
    policy_fingerprint TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_entry ON decisions(entry_key, seq);
CREATE TABLE IF NOT EXISTS policy_versions (
    fingerprint TEXT PRIMARY KEY,
    source_version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    view_json TEXT NOT NULL,
    closure_json TEXT,
    decision_seq INTEGER
);
CREATE TABLE IF NOT EXISTS pending_ops (
    idempotency_key TEXT PRIMARY KEY,
    intent_json TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_seq INTEGER,
    last_error TEXT,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS command_log (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    started_clock_ms INTEGER NOT NULL DEFAULT 0,
    finished_clock_ms INTEGER
);
CREATE TABLE IF NOT EXISTS run_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    report_json TEXT NOT NULL
);
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class LedgerStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.db_path.with_name(self.db_path.name + ".lock")
        self.file_lock = FileLock(self.lock_path)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.initialize_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LedgerStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def initialize_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    @staticmethod
    def reset_database(db_path: str | Path) -> None:
        path = Path(db_path)
        for suffix in ("", "-wal", "-shm", ".lock"):
            candidate = Path(str(path) + suffix) if suffix else path
            if candidate.exists():
                candidate.unlink()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.file_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value if isinstance(value, str) else canonical_json(value)),
            )
            self.conn.commit()

    def capacity_dict(self) -> dict[str, Any]:
        raw = self.get_meta("capacity_json")
        if raw is None:
            raise CacheLedgerError("E_NOT_LOADED", "sample has not been loaded; run load first")
        return json.loads(raw)

    def check_clock(self, clock_ms: int, tolerance_ms: Optional[int] = None) -> int:
        if not isinstance(clock_ms, int) or isinstance(clock_ms, bool) or clock_ms < 0:
            raise CacheLedgerError("E_INVALID_CLOCK", "clock_ms must be a non-negative integer", field="clock_ms")
        watermark = int(self.get_meta("clock_watermark_ms", "0") or 0)
        if tolerance_ms is None:
            tolerance_ms = int(self.get_meta("clock_tolerance_ms", "0") or 0)
        if clock_ms + tolerance_ms < watermark:
            raise ClockRollbackError(clock_ms, watermark, tolerance_ms)
        return watermark

    def load_bundle(self, bundle: SampleBundle) -> dict[str, Any]:
        from dataclasses import asdict

        with self.file_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for table in (
                    "run_reports", "command_log", "pending_ops", "policy_versions",
                    "decisions", "layer_usage", "tenant_usage", "entries", "tenants", "meta",
                ):
                    self.conn.execute(f"DELETE FROM {table}")
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?)",
                    ("sample_dir", str(bundle.sample_dir)),
                )
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?)",
                    ("capacity_json", canonical_json(asdict(bundle.capacity))),
                )
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?)",
                    ("clock_tolerance_ms", str(bundle.capacity.clock_tolerance_ms)),
                )
                self.conn.execute("INSERT INTO meta(key,value) VALUES('clock_watermark_ms','0')")
                access_watermark = max(entry.last_access_ms for entry in bundle.entries)
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES('access_clock_watermark_ms',?)",
                    (str(access_watermark),),
                )
                for tenant in bundle.tenants.values():
                    self.conn.execute(
                        """INSERT INTO tenants(name,max_blocks,soft_quota_blocks,hard_quota_blocks,
                           soft_ratio,hard_ratio,eviction_priority_weight)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            tenant.name, tenant.max_blocks, tenant.soft_quota_blocks,
                            tenant.hard_quota_blocks, tenant.soft_ratio, tenant.hard_ratio,
                            tenant.eviction_priority_weight,
                        ),
                    )
                    self.conn.execute(
                        "INSERT INTO tenant_usage(tenant,used_blocks,used_bytes) VALUES(?,0,0)",
                        (tenant.name,),
                    )
                for layer in LAYERS:
                    self.conn.execute(
                        "INSERT INTO layer_usage(layer,used_blocks,used_bytes) VALUES(?,0,0)",
                        (layer,),
                    )
                for entry in bundle.entries:
                    self.conn.execute(
                        """INSERT INTO entries(seq,key,tenant,declared_layer,block_size,size_bytes,
                           last_access_ms,access_count,digest,status)
                           VALUES(?,?,?,?,?,?,?,?,?,'new')""",
                        (
                            entry.seq, entry.key, entry.tenant, entry.declared_layer,
                            entry.block_size, entry.block_size * bundle.capacity.block_bytes,
                            entry.last_access_ms, entry.access_count, entry.digest,
                        ),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {"loaded_entries": len(bundle.entries), "tenants": len(bundle.tenants)}

    def start_command(self, command: str, request: dict[str, Any], clock_ms: int = 0) -> int:
        with self.file_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            cursor = self.conn.execute(
                "INSERT INTO command_log(command,request_json,status,started_clock_ms) VALUES(?,?, 'running', ?)",
                (command, canonical_json(request), clock_ms),
            )
            request_id = int(cursor.lastrowid)
            self.conn.commit()
        return request_id

    def finish_command(self, request_id: int, status: str, result: dict[str, Any]) -> None:
        with self.file_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "UPDATE command_log SET status=?, result_json=?, finished_clock_ms=? WHERE request_id=?",
                (status, canonical_json(result), int(result.get("clock_ms", 0) or 0), request_id),
            )
            self.conn.commit()

    def record_report(self, command: str, scope_key: str, clock_ms: int, report: dict[str, Any]) -> int:
        with self.file_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            cursor = self.conn.execute(
                "INSERT INTO run_reports(command,scope_key,clock_ms,report_json) VALUES(?,?,?,?)",
                (command, scope_key, clock_ms, canonical_json(report)),
            )
            report_id = int(cursor.lastrowid)
            self.conn.commit()
        return report_id

    def find_decision(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return self._decision_dict(row) if row else None

    def _decision_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in row.keys() if key != "payload_json"}
        result["payload"] = json.loads(row["payload_json"])
        return result

    def prepare_intent(self, intent: dict[str, Any]) -> str:
        key = intent["idempotency_key"]
        existing = self.find_decision(key)
        if existing is not None:
            return "decision_exists"
        with self.file_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """INSERT INTO pending_ops(idempotency_key,intent_json,status,created_clock_ms,updated_clock_ms)
                   VALUES(?,?,'prepared',?,?)
                   ON CONFLICT(idempotency_key) DO NOTHING""",
                (key, canonical_json(intent), int(intent.get("clock_ms", 0)), int(intent.get("clock_ms", 0))),
            )
            self.conn.commit()
        crash_key = os.environ.get("CACHE_LEDGER_CRASH_AFTER_PREPARE_KEY")
        if crash_key and crash_key == key:
            os._exit(86)
        return "prepared"

    def execute_intent(self, intent: dict[str, Any], *, fail_after_start: bool = False) -> dict[str, Any]:
        existing = self.find_decision(intent["idempotency_key"])
        if existing is not None:
            return existing
        self.prepare_intent(intent)
        return self.commit_prepared(intent["idempotency_key"], fail_after_start=fail_after_start)

    def commit_prepared(self, idempotency_key: str, *, fail_after_start: bool = False) -> dict[str, Any]:
        with self.file_lock:
            row = self.conn.execute(
                "SELECT * FROM pending_ops WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row is None:
                existing = self.find_decision(idempotency_key)
                if existing is not None:
                    return existing
                raise CacheLedgerError("E_PENDING_OP_MISSING", "prepared operation not found", key=idempotency_key)
            intent = json.loads(row["intent_json"])
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing = self.conn.execute(
                    "SELECT * FROM decisions WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    decision = self._decision_dict(existing)
                    self.conn.execute(
                        "UPDATE pending_ops SET status='committed', decision_seq=?, updated_clock_ms=? WHERE idempotency_key=?",
                        (decision["seq"], intent.get("clock_ms", 0), idempotency_key),
                    )
                    self.conn.commit()
                    return decision
                if fail_after_start:
                    raise CacheLedgerError(
                        "E_INJECTED_START_FAILURE",
                        "candidate failed after logical start",
                        field="idempotency_key",
                        details={"key": idempotency_key},
                    )
                seq = int(self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM decisions").fetchone()["next_seq"])
                self._apply_mutation(intent, seq)
                payload = dict(intent.get("payload", {}))
                self.conn.execute(
                    """INSERT INTO decisions(seq,idempotency_key,kind,tenant,entry_key,clock_ms,
                       policy_fingerprint,payload_json) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        seq, idempotency_key, intent["kind"], intent.get("tenant"),
                        intent.get("entry_key"), int(intent.get("clock_ms", 0)),
                        intent.get("policy_fingerprint"), canonical_json(payload),
                    ),
                )
                self.conn.execute(
                    """UPDATE pending_ops SET status='committed', decision_seq=?, last_error=NULL,
                       updated_clock_ms=? WHERE idempotency_key=?""",
                    (seq, int(intent.get("clock_ms", 0)), idempotency_key),
                )
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES('clock_watermark_ms',?) ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER), ?)",
                    (str(intent.get("clock_ms", 0)), str(intent.get("clock_ms", 0))),
                )
                self.assert_invariants_locked()
                crash_key = os.environ.get("CACHE_LEDGER_CRASH_BEFORE_COMMIT_KEY")
                if crash_key and crash_key == idempotency_key:
                    os._exit(86)
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                error_message = str(exc)
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    "UPDATE pending_ops SET status='failed', last_error=?, updated_clock_ms=? WHERE idempotency_key=?",
                    (error_message, int(intent.get("clock_ms", 0)), idempotency_key),
                )
                self.conn.commit()
                if isinstance(exc, CacheLedgerError):
                    raise
                raise CacheLedgerError("E_LEDGER_COMMIT_FAILED", error_message) from exc
        decision = self.find_decision(idempotency_key)
        if decision is None:
            raise CacheLedgerError("E_DECISION_MISSING_AFTER_COMMIT", "decision vanished after commit")
        return decision

    def _apply_mutation(self, intent: dict[str, Any], seq: int) -> None:
        mutation_type = intent.get("mutation_type", intent["kind"])
        payload = intent.get("payload", {})
        clock_ms = int(intent.get("clock_ms", 0))
        if mutation_type == "policy_merge":
            view = payload["view"]
            closure = payload.get("closure")
            fingerprint = view["policy_fingerprint"]
            self.conn.execute(
                """INSERT OR IGNORE INTO policy_versions
                   (fingerprint,source_version,generation,clock_ms,view_json,closure_json,decision_seq)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    fingerprint, view.get("v2_source_version", "unified"),
                    int(view.get("v2_snapshot_generation", 0)), clock_ms,
                    canonical_json(view), canonical_json(closure) if closure is not None else None, seq,
                ),
            )
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('policy_pointer_fingerprint',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (fingerprint,),
            )
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('policy_generation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(view.get("v2_snapshot_generation", 0)),),
            )
            return

        key = payload["key"]
        row = self.conn.execute("SELECT * FROM entries WHERE key=?", (key,)).fetchone()
        if row is None:
            raise CacheLedgerError("E_ENTRY_NOT_FOUND", "mutation references missing entry", key=key)

        if mutation_type == "defer":
            if row["status"] not in ("new", "deferred"):
                raise CacheLedgerError("E_INVALID_ENTRY_STATE", "only new/deferred entry can be deferred", key=key, status=row["status"])
            self.conn.execute(
                "UPDATE entries SET status='deferred', status_reason=?, updated_decision_seq=?, last_change_clock_ms=? WHERE key=?",
                (payload.get("reason", "capacity"), seq, clock_ms, key),
            )
            return

        if mutation_type == "deny":
            if row["status"] not in ("new", "deferred"):
                raise CacheLedgerError("E_INVALID_ENTRY_STATE", "only new/deferred entry can be denied", key=key, status=row["status"])
            self.conn.execute(
                "UPDATE entries SET status='denied', status_reason=?, policy_source=?, policy_generation=?, updated_decision_seq=?, last_change_clock_ms=? WHERE key=?",
                (
                    payload.get("reason", "denied"), payload.get("policy_source", "policy"),
                    int(payload.get("policy_generation", 0)), seq, clock_ms, key,
                ),
            )
            return

        if mutation_type == "admit":
            if row["status"] == "admitted":
                raise CacheLedgerError("E_DUPLICATE_ADMISSION", "entry is already admitted", key=key)
            layer = payload["layer"]
            blocks = int(payload["block_size"])
            bytes_used = int(payload["size_bytes"])
            tenant = row["tenant"]
            tenant_row = self.conn.execute("SELECT * FROM tenants WHERE name=?", (tenant,)).fetchone()
            usage = self.conn.execute("SELECT * FROM tenant_usage WHERE tenant=?", (tenant,)).fetchone()
            if int(usage["used_blocks"]) + blocks > int(tenant_row["hard_quota_blocks"]):
                raise CacheLedgerError("E_CAPACITY_EXCEEDED", "tenant hard quota would be exceeded", tenant=tenant, key=key)
            layer_usage = self.conn.execute("SELECT * FROM layer_usage WHERE layer=?", (layer,)).fetchone()
            capacity = self.capacity_dict()
            if int(layer_usage["used_blocks"]) + blocks > int(capacity["layer_max_blocks"][layer]):
                raise CacheLedgerError("E_CAPACITY_EXCEEDED", "layer block capacity would be exceeded", layer=layer, key=key)
            total_bytes = int(self.conn.execute("SELECT COALESCE(SUM(used_bytes),0) AS b FROM tenant_usage").fetchone()["b"])
            if total_bytes + bytes_used > int(capacity["total_byte_budget"]):
                raise CacheLedgerError("E_CAPACITY_EXCEEDED", "total byte budget would be exceeded", key=key)
            self.conn.execute(
                "UPDATE tenant_usage SET used_blocks=used_blocks+?, used_bytes=used_bytes+? WHERE tenant=?",
                (blocks, bytes_used, tenant),
            )
            self.conn.execute(
                "UPDATE layer_usage SET used_blocks=used_blocks+?, used_bytes=used_bytes+? WHERE layer=?",
                (blocks, bytes_used, layer),
            )
            self.conn.execute(
                """UPDATE entries SET status='admitted', status_reason=NULL, current_layer=?,
                   policy_source=?, policy_generation=?, admitted_decision_seq=?, updated_decision_seq=?,
                   last_change_clock_ms=?, last_migration_clock_ms=? WHERE key=?""",
                (
                    layer, payload.get("policy_source", "default"), int(payload.get("policy_generation", 0)),
                    seq, seq, clock_ms, clock_ms, key,
                ),
            )
            return

        if mutation_type == "rebalance":
            from_layer = payload["from_layer"]
            to_layer = payload["to_layer"]
            blocks = int(payload["block_size"])
            bytes_used = int(payload["size_bytes"])
            if row["status"] != "admitted" or row["current_layer"] != from_layer:
                raise CacheLedgerError(
                    "E_STALE_MIGRATION",
                    "entry layer changed before migration commit",
                    key=key,
                    expected_layer=from_layer,
                    actual_layer=row["current_layer"],
                )
            capacity = self.capacity_dict()
            target = self.conn.execute("SELECT * FROM layer_usage WHERE layer=?", (to_layer,)).fetchone()
            if int(target["used_blocks"]) + blocks > int(capacity["layer_max_blocks"][to_layer]):
                raise CacheLedgerError("E_CAPACITY_EXCEEDED", "target layer capacity would be exceeded", layer=to_layer, key=key)
            self.conn.execute(
                "UPDATE layer_usage SET used_blocks=used_blocks-?, used_bytes=used_bytes-? WHERE layer=?",
                (blocks, bytes_used, from_layer),
            )
            self.conn.execute(
                "UPDATE layer_usage SET used_blocks=used_blocks+?, used_bytes=used_bytes+? WHERE layer=?",
                (blocks, bytes_used, to_layer),
            )
            self.conn.execute(
                """UPDATE entries SET current_layer=?, updated_decision_seq=?, last_change_clock_ms=?,
                   last_migration_clock_ms=?, migration_count=migration_count+1,
                   policy_source=?, policy_generation=? WHERE key=?""",
                (
                    to_layer, seq, clock_ms, clock_ms,
                    payload.get("policy_source", row["policy_source"]),
                    int(payload.get("policy_generation", row["policy_generation"])), key,
                ),
            )
            return

        if mutation_type == "evict":
            layer = payload["layer"]
            blocks = int(payload["block_size"])
            bytes_used = int(payload["size_bytes"])
            if row["status"] != "admitted" or row["current_layer"] != layer:
                raise CacheLedgerError(
                    "E_STALE_EVICTION",
                    "entry state changed before eviction commit",
                    key=key,
                    expected_layer=layer,
                    actual_layer=row["current_layer"],
                    status=row["status"],
                )
            self.conn.execute(
                "UPDATE tenant_usage SET used_blocks=used_blocks-?, used_bytes=used_bytes-? WHERE tenant=?",
                (blocks, bytes_used, row["tenant"]),
            )
            self.conn.execute(
                "UPDATE layer_usage SET used_blocks=used_blocks-?, used_bytes=used_bytes-? WHERE layer=?",
                (blocks, bytes_used, layer),
            )
            self.conn.execute(
                """UPDATE entries SET status='evicted', current_layer=NULL, eviction_reason=?,
                   status_reason=?, updated_decision_seq=?, last_change_clock_ms=? WHERE key=?""",
                (payload.get("reason", "evict"), payload.get("reason", "evict"), seq, clock_ms, key),
            )
            return

        raise CacheLedgerError("E_UNKNOWN_MUTATION", f"unknown mutation type: {mutation_type}")

    def assert_invariants_locked(self) -> dict[str, Any]:
        capacity = self.capacity_dict()
        for tenant in self.conn.execute("SELECT * FROM tenants").fetchall():
            usage = self.conn.execute("SELECT * FROM tenant_usage WHERE tenant=?", (tenant["name"],)).fetchone()
            used_blocks = int(usage["used_blocks"])
            used_bytes = int(usage["used_bytes"])
            entry_blocks = self.conn.execute(
                "SELECT COALESCE(SUM(block_size),0) AS blocks, COALESCE(SUM(size_bytes),0) AS bytes FROM entries WHERE tenant=? AND status='admitted'",
                (tenant["name"],),
            ).fetchone()
            if used_blocks < 0 or used_bytes < 0:
                raise CacheLedgerError("E_LEDGER_NEGATIVE", "negative tenant usage", tenant=tenant["name"])
            if used_blocks != int(entry_blocks["blocks"]) or used_bytes != int(entry_blocks["bytes"]):
                raise CacheLedgerError("E_LEDGER_CONSERVATION", "tenant usage does not match entries", tenant=tenant["name"])
            if used_blocks > int(tenant["hard_quota_blocks"]):
                raise CacheLedgerError("E_QUOTA_EXCEEDED", "tenant hard quota exceeded", tenant=tenant["name"])
        for layer in LAYERS:
            usage = self.conn.execute("SELECT * FROM layer_usage WHERE layer=?", (layer,)).fetchone()
            entries_sum = self.conn.execute(
                "SELECT COALESCE(SUM(block_size),0) AS blocks, COALESCE(SUM(size_bytes),0) AS bytes FROM entries WHERE status='admitted' AND current_layer=?",
                (layer,),
            ).fetchone()
            used_blocks = int(usage["used_blocks"])
            if used_blocks < 0 or int(usage["used_bytes"]) < 0:
                raise CacheLedgerError("E_LEDGER_NEGATIVE", "negative layer usage", layer=layer)
            if used_blocks != int(entries_sum["blocks"]) or int(usage["used_bytes"]) != int(entries_sum["bytes"]):
                raise CacheLedgerError("E_LEDGER_CONSERVATION", "layer usage does not match entries", layer=layer)
            if used_blocks > int(capacity["layer_max_blocks"][layer]):
                raise CacheLedgerError("E_LAYER_CAPACITY_EXCEEDED", "layer capacity exceeded", layer=layer)
        total_bytes = int(self.conn.execute("SELECT COALESCE(SUM(used_bytes),0) AS bytes FROM tenant_usage").fetchone()["bytes"])
        if total_bytes > int(capacity["total_byte_budget"]):
            raise CacheLedgerError("E_BYTE_BUDGET_EXCEEDED", "total byte budget exceeded")
        seq_rows = self.conn.execute("SELECT seq FROM decisions ORDER BY seq").fetchall()
        seqs = [int(row["seq"]) for row in seq_rows]
        if seqs != list(range(1, len(seqs) + 1)):
            raise CacheLedgerError("E_DECISION_SEQUENCE_GAP", "decision sequence is not gapless")
        return {"ok": True, "decision_count": len(seqs), "total_bytes": total_bytes}

    def pending_operations(self, status: str = "prepared") -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM pending_ops WHERE status=? ORDER BY idempotency_key", (status,)
        ).fetchall()
        return [dict(row) for row in rows]

    def running_commands(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM command_log WHERE status='running' ORDER BY request_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def decisions(self) -> list[dict[str, Any]]:
        return [self._decision_dict(row) for row in self.conn.execute("SELECT * FROM decisions ORDER BY seq").fetchall()]

    def status(self) -> dict[str, Any]:
        capacity = self.capacity_dict() if self.get_meta("capacity_json") else None
        tenants: dict[str, Any] = {}
        for row in self.conn.execute(
            """SELECT t.name,t.max_blocks,t.soft_quota_blocks,t.hard_quota_blocks,
                      t.eviction_priority_weight,u.used_blocks,u.used_bytes
               FROM tenants t JOIN tenant_usage u ON u.tenant=t.name ORDER BY t.name"""
        ).fetchall():
            tenants[row["name"]] = {
                "max_blocks": row["max_blocks"],
                "soft_quota_blocks": row["soft_quota_blocks"],
                "hard_quota_blocks": row["hard_quota_blocks"],
                "used_blocks": row["used_blocks"],
                "used_bytes": row["used_bytes"],
                "available_hard_blocks": int(row["hard_quota_blocks"]) - int(row["used_blocks"]),
                "eviction_priority_weight": row["eviction_priority_weight"],
            }
        layers: dict[str, Any] = {}
        for layer in LAYERS:
            row = self.conn.execute("SELECT * FROM layer_usage WHERE layer=?", (layer,)).fetchone()
            used_blocks = int(row["used_blocks"]) if row else 0
            limit = capacity["layer_max_blocks"][layer] if capacity else None
            layers[layer] = {
                "used_blocks": used_blocks,
                "used_bytes": int(row["used_bytes"]) if row else 0,
                "max_blocks": limit,
                "available_blocks": (limit - used_blocks) if limit is not None else None,
            }
        entries = []
        for row in self.conn.execute("SELECT key,tenant,status,current_layer,policy_generation,updated_decision_seq FROM entries ORDER BY key").fetchall():
            entries.append(dict(row))
        pointer = self.get_meta("policy_pointer_fingerprint")
        policy = None
        if pointer:
            policy = self.conn.execute("SELECT fingerprint,source_version,generation,clock_ms,decision_seq FROM policy_versions WHERE fingerprint=?", (pointer,)).fetchone()
        return {
            "ok": True,
            "clock_watermark_ms": int(self.get_meta("clock_watermark_ms", "0") or 0),
            "policy_pointer": dict(policy) if policy else {"fingerprint": pointer},
            "tenants": tenants,
            "layers": layers,
            "entries": entries,
            "decision_count": int(self.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]),
            "prepared_operations": len(self.pending_operations("prepared")),
            "running_commands": len(self.running_commands()),
        }
