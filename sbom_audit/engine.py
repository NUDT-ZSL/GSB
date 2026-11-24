"""SQLite ledger, idempotent batches, incremental recomputation and scheduling."""

from __future__ import annotations

import copy
import hashlib
import json
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .codes import (
    E_LEDGER,
    E_QUEUE_FULL,
    E_RECOMPUTE_DIVERGED,
    E_TIMEOUT,
    RULESET_VERSIONS,
)
from .graph import DependencyGraph
from .merge import assemble_integrity_report, build_integrity_report, canonical_report_digest, merge_views
from .model import AuditError, ComponentRecord, canonical_json, digest_object
from .parser import parse_dependency_file, parse_v1_file, parse_v2_snapshot


RECORD_FIELDS = {f.name for f in ComponentRecord.__dataclass_fields__.values()}


@dataclass
class Budget:
    concurrency: int = 2
    batch_timeout_ms: int = 10_000
    queue_limit: int = 16
    max_digest_bytes_per_component: int = 64 * 1024 * 1024
    chunk_size: int = 64 * 1024


@dataclass
class Batch:
    batch_id: str
    records: list[Any] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    source_kind: str | None = None
    source_path: str | Path | None = None
    db_path: str | Path | None = None
    work_items: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    started_event: Any = None
    proceed_event: Any = None


def coerce_record(item: Any) -> ComponentRecord:
    if isinstance(item, ComponentRecord):
        return item
    if not isinstance(item, dict):
        raise AuditError(E_LEDGER, "record must be an object")
    filtered = {key: value for key, value in item.items() if key in RECORD_FIELDS}
    missing = [key for key in ("canonical_key", "component_id", "signature_status", "generation", "snapshot_id", "source_ref", "record_index") if key not in filtered]
    if missing:
        raise AuditError(E_LEDGER, f"record missing fields: {', '.join(missing)}")
    return ComponentRecord(**filtered)


def rejection_dicts(items: list[Any]) -> list[dict[str, Any]]:
    return [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in items]


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        target = ":memory:" if str(self.path) == ":memory:" else str(self.path)
        conn = sqlite3.connect(target, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        if target != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    code TEXT,
                    batch_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_key TEXT NOT NULL UNIQUE,
                    natural_key TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    batch_id TEXT NOT NULL,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    record_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_key ON records(canonical_key);
                CREATE TABLE IF NOT EXISTS edges (
                    edge_key TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    edge_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    seq INTEGER PRIMARY KEY,
                    checkpoint_json TEXT NOT NULL,
                    view_digest TEXT NOT NULL,
                    created_source TEXT NOT NULL DEFAULT 'wall'
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()

    def meta_set(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def next_seq(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS next FROM batches").fetchone()
        return int(row["next"])

    def stored_records(self, conn: sqlite3.Connection | None = None) -> list[ComponentRecord]:
        close = conn is None
        conn = conn or self.connect()
        try:
            rows = conn.execute("SELECT record_json FROM records ORDER BY id").fetchall()
            return [coerce_record(json.loads(row["record_json"])) for row in rows]
        finally:
            if close:
                conn.close()

    def stored_edges(self, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        close = conn is None
        conn = conn or self.connect()
        try:
            rows = conn.execute("SELECT edge_json FROM edges ORDER BY edge_key").fetchall()
            return [json.loads(row["edge_json"]) for row in rows]
        finally:
            if close:
                conn.close()

    def batch_summaries(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT result_json FROM batches ORDER BY seq").fetchall()
            summaries = []
            for row in rows:
                result = json.loads(row["result_json"])
                summaries.append(stable_batch_summary(result))
            return summaries
        finally:
            conn.close()


def stable_batch_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": result.get("batch_id"),
        "status": result.get("status"),
        "code": result.get("code"),
        "codes": result.get("codes", []),
        "applied_record_count": result.get("applied_record_count", 0),
        "duplicate_count": result.get("duplicate_count", 0),
        "duplicates": result.get("duplicates", []),
        "rejection_count": len(result.get("rejections", [])),
        "incremental": result.get("incremental"),
    }


def prepare_batch(batch: Batch, budget: Budget | None = None, deadline: float | None = None) -> tuple[list[ComponentRecord], list[dict[str, Any]]]:
    records = list(batch.records)
    rejections = list(batch.rejections)
    if batch.source_kind and batch.source_path:
        path = Path(batch.source_path)
        if batch.source_kind == "v1":
            parsed = parse_v1_file(path)
            records.extend(parsed.records)
            rejections.extend(rejection_dicts(parsed.rejections))
        elif batch.source_kind == "v2":
            parsed = parse_v2_snapshot(path, batch.db_path)
            records.extend(parsed.records)
            rejections.extend(rejection_dicts(parsed.rejections))
        elif batch.source_kind == "dependencies":
            edges, edge_rejections, _ = parse_dependency_file(path)
            batch.edges.extend(edges)
            rejections.extend(rejection_dicts(edge_rejections))
        else:
            raise AuditError(E_LEDGER, f"unknown source_kind {batch.source_kind}")

    component_sizes: dict[str, int] = {}
    for work in batch.work_items:
        key = str(work.get("canonical_key", "work-item"))
        size = int(work.get("bytes", work.get("size_bytes", 0)))
        component_sizes[key] = component_sizes.get(key, 0) + size
        max_bytes = budget.max_digest_bytes_per_component if budget else 64 * 1024 * 1024
        if component_sizes[key] > max_bytes:
            raise AuditError("E_HASH_WORK_LIMIT", f"{key} digest work exceeds {max_bytes} bytes")
        algorithm = str(work.get("algorithm", "sha256"))
        hasher = hashlib.sha256() if algorithm == "sha256" else hashlib.new("sha256")
        chunk_size = int((budget.chunk_size if budget else 64 * 1024))
        remaining = size
        seed = int(work.get("seed", 7)) & 255
        while remaining > 0:
            if deadline is not None and time.perf_counter() >= deadline:
                raise AuditError(E_TIMEOUT, "real wall-clock batch deadline exceeded")
            count = min(chunk_size, remaining)
            chunk = bytes([seed]) * count
            hasher.update(chunk)
            remaining -= count
        if deadline is not None and time.perf_counter() >= deadline:
            raise AuditError(E_TIMEOUT, "real wall-clock batch deadline exceeded after hashing")

    coerced = [coerce_record(item) for item in records]
    return coerced, rejections


def _delivery_key(record: ComponentRecord) -> str:
    if record.delivery_id:
        return "delivery:" + record.delivery_id
    natural = {
        "canonical_key": record.canonical_key,
        "generation": record.generation,
        "version": record.version,
        "algorithm": record.algorithm,
        "digest": record.digest,
        "snapshot_id": record.snapshot_id,
        "record_index": record.record_index,
        "published_at_ms": record.published_at_ms,
    }
    return "natural:" + digest_object(natural)


def _natural_key(record: ComponentRecord) -> str:
    return digest_object(record.to_dict())


def _insert_records(conn: sqlite3.Connection, batch: Batch, records: list[ComponentRecord]) -> tuple[int, list[dict[str, Any]]]:
    applied = 0
    duplicate_count = 0
    duplicates: list[dict[str, Any]] = []
    for record in records:
        delivery_key = _delivery_key(record)
        natural_key = _natural_key(record)
        existing = conn.execute("SELECT id, duplicate_count FROM records WHERE delivery_key=? OR natural_key=?", (delivery_key, natural_key)).fetchone()
        if existing is not None:
            count = int(existing["duplicate_count"]) + 1
            conn.execute("UPDATE records SET duplicate_count=? WHERE id=?", (count, existing["id"]))
            duplicate_count += 1
            duplicates.append({"canonical_key": record.canonical_key, "delivery_key": delivery_key, "duplicate_count": count})
            continue
        conn.execute(
            "INSERT INTO records(delivery_key,natural_key,canonical_key,generation,batch_id,duplicate_count,record_json) VALUES(?,?,?,?,?,0,?)",
            (delivery_key, natural_key, record.canonical_key, record.generation, batch.batch_id, json.dumps(record.to_dict(), sort_keys=True)),
        )
        applied += 1
    return applied, duplicates


def _insert_edges(conn: sqlite3.Connection, batch: Batch) -> int:
    inserted = 0
    for edge in batch.edges:
        edge_key = digest_object({k: edge.get(k) for k in ("from", "to", "constraint")})
        cursor = conn.execute(
            "INSERT OR IGNORE INTO edges(edge_key,batch_id,edge_json) VALUES(?,?,?)",
            (edge_key, batch.batch_id, json.dumps(edge, sort_keys=True)),
        )
        inserted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    return inserted


class AuditEngine:
    """Stateful engine with cached merge views and equivalence verification."""

    def __init__(self, ledger: str | Path | Ledger, sync_markers: dict[str, dict[str, Any]] | None = None):
        self.ledger = ledger if isinstance(ledger, Ledger) else Ledger(ledger)
        self.sync_markers = sync_markers or {}
        self.records = self.ledger.stored_records()
        self.edges = self.ledger.stored_edges()
        self.batch_summaries = self.ledger.batch_summaries()
        self.view_cache: dict[str, dict[str, Any]] = {}
        self.conflict_cache: dict[str, list[dict[str, Any]]] = {}
        self.report: dict[str, Any] | None = None
        if self.records or self.edges:
            self.report = self.full_report()
            for view in self.report["integrity_view"]:
                self.view_cache[view["canonical_key"]] = view
            for conflict in self.report["conflicts"]:
                self.conflict_cache.setdefault(conflict["canonical_key"], []).append(conflict)

    def full_report(self, batch_reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return build_integrity_report(
            self.records,
            self.edges,
            rejections=self._all_rejections(),
            sync_markers=self.sync_markers,
            batch_reports=batch_reports if batch_reports is not None else self.batch_summaries,
            policy_versions=RULESET_VERSIONS,
        )

    def _all_rejections(self) -> list[dict[str, Any]]:
        conn = self.ledger.connect()
        try:
            rows = conn.execute("SELECT result_json FROM batches ORDER BY seq").fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                result.extend(json.loads(row["result_json"]).get("rejections", []))
            return result
        finally:
            conn.close()

    def _incremental_report(
        self,
        changed_keys: set[str],
        all_records: list[ComponentRecord],
        all_edges: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        rejections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        records_by_key: dict[str, list[ComponentRecord]] = {}
        for record in all_records:
            records_by_key.setdefault(record.canonical_key, []).append(record)
        changed_records = [r for key in changed_keys for r in records_by_key.get(key, [])]
        if changed_records:
            views, conflicts, _ = merge_views(changed_records, self.sync_markers)
            for view in views:
                self.view_cache[view["canonical_key"]] = view
            for key in changed_keys:
                self.conflict_cache[key] = []
            for conflict in conflicts:
                self.conflict_cache.setdefault(conflict["canonical_key"], []).append(conflict)
        graph = DependencyGraph(all_edges)
        all_conflicts = [conflict for key in sorted(self.conflict_cache) for conflict in sorted(self.conflict_cache[key], key=lambda c: (c["field"], str(c.get("business_code", ""))))]
        return assemble_integrity_report(
            list(self.view_cache.values()),
            all_conflicts,
            graph,
            records=all_records,
            rejections=rejections,
            batch_reports=summaries,
            policy_versions=RULESET_VERSIONS,
        )

    def apply_batch(self, batch: Batch, budget: Budget | None = None) -> dict[str, Any]:
        budget = budget or Budget()
        deadline = time.perf_counter() + budget.batch_timeout_ms / 1000.0
        started = time.perf_counter()
        try:
            prepared, parse_rejections = prepare_batch(batch, budget, deadline)
        except AuditError as exc:
            return {
                "batch_id": batch.batch_id,
                "status": "error",
                "code": exc.code,
                "codes": [exc.code],
                "message": exc.message,
                "rejections": [],
                "duplicates": [],
                "duplicate_count": 0,
                "applied_record_count": 0,
                "edge_count": len(batch.edges),
                "transaction_committed": False,
                "elapsed_ms_real": round((time.perf_counter() - started) * 1000, 3),
            }
        return self.apply_prepared(batch, prepared, parse_rejections, started)

    def apply_prepared(
        self,
        batch: Batch,
        prepared: list[ComponentRecord],
        parse_rejections: list[dict[str, Any]],
        started: float,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "batch_id": batch.batch_id,
            "status": "applied",
            "code": None,
            "codes": [],
            "rejections": parse_rejections,
            "duplicates": [],
            "duplicate_count": 0,
            "applied_record_count": 0,
            "edge_count": len(batch.edges),
            "transaction_committed": False,
        }
        conn = self.ledger.connect()
        cache_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT result_json FROM batches WHERE batch_id=?", (batch.batch_id,)).fetchone()
            if existing:
                prior = json.loads(existing["result_json"])
                conn.rollback()
                prior["redelivered_batch"] = True
                return prior
            seq = self.ledger.next_seq(conn)
            applied, duplicates = _insert_records(conn, batch, prepared)
            edge_inserted = _insert_edges(conn, batch)
            all_records = self.ledger.stored_records(conn)
            all_edges = self.ledger.stored_edges(conn)
            summaries = self.ledger.batch_summaries() + [stable_batch_summary(result)]
            changed_keys = {r.canonical_key for r in prepared}
            changed_keys.update({edge["from"] for edge in batch.edges} | {edge["to"] for edge in batch.edges})

            tx_rejections = self._tx_rejections(conn, result["rejections"])
            full_report = build_integrity_report(
                all_records,
                all_edges,
                rejections=tx_rejections,
                sync_markers=self.sync_markers,
                batch_reports=summaries,
                policy_versions=RULESET_VERSIONS,
            )
            incremental_info = None
            if self.report is not None:
                cache_snapshot = (
                    {key: copy.deepcopy(self.view_cache.get(key)) for key in changed_keys},
                    {key: copy.deepcopy(self.conflict_cache.get(key)) for key in changed_keys},
                )
                inc_report = self._incremental_report(changed_keys, all_records, all_edges, summaries, tx_rejections)
                if canonical_json(inc_report) != canonical_json(full_report):
                    conn.rollback()
                    self._restore_cache_snapshot(cache_snapshot)
                    result.update(status="error", code=E_RECOMPUTE_DIVERGED, codes=[E_RECOMPUTE_DIVERGED])
                    result["equivalence"] = {"equal": False, "difference": _first_difference(inc_report, full_report)}
                    return result
                closure = set(inc_report["closure"])
                incremental_info = {
                    "recomputed_keys": sorted(changed_keys | closure),
                    "closure_size": len(closure),
                    "incoming_edges": len(DependencyGraph(all_edges).incoming_edges(changed_keys | closure)),
                    "equivalent_to_full_recompute": True,
                }
            else:
                for view in full_report["integrity_view"]:
                    self.view_cache[view["canonical_key"]] = view
                for conflict in full_report["conflicts"]:
                    self.conflict_cache.setdefault(conflict["canonical_key"], []).append(conflict)

            result.update(
                applied_record_count=applied,
                duplicate_count=len(duplicates),
                duplicates=duplicates,
                edge_count=edge_inserted,
                incremental=incremental_info,
                view_digest=full_report["view_digest"],
                watermark=seq,
            )
            if result["rejections"]:
                result["status"] = "partial"
            result["elapsed_ms_real"] = round((time.perf_counter() - started) * 1000, 3)
            conn.execute(
                "INSERT INTO batches(batch_id,seq,status,code,batch_json,result_json) VALUES(?,?,?,?,?,?)",
                (batch.batch_id, seq, result["status"], result.get("code"), json.dumps(batch.to_dict() if hasattr(batch, "to_dict") else batch.__dict__, default=str, sort_keys=True), json.dumps(result, sort_keys=True)),
            )
            self.ledger.meta_set(conn, "watermark", seq)
            self.ledger.meta_set(conn, "view_digest", full_report["view_digest"])
            conn.commit()
            result["transaction_committed"] = True
            self.records = all_records
            self.edges = all_edges
            self.batch_summaries = summaries
            self.report = full_report
            return result
        except Exception as exc:  # pragma: no cover - defensive transaction boundary
            conn.rollback()
            if cache_snapshot is not None:
                self._restore_cache_snapshot(cache_snapshot)
            result.update(status="error", code=E_LEDGER, message=str(exc))
            return result
        finally:
            conn.close()

    def _restore_cache_snapshot(self, snapshot: tuple[dict[str, Any], dict[str, Any]]) -> None:
        views, conflicts = snapshot
        for key, value in views.items():
            if value is None:
                self.view_cache.pop(key, None)
            else:
                self.view_cache[key] = value
        for key, value in conflicts.items():
            if value is None:
                self.conflict_cache.pop(key, None)
            else:
                self.conflict_cache[key] = value

    def _tx_rejections(self, conn: sqlite3.Connection, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT result_json FROM batches ORDER BY seq").fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            items.extend(json.loads(row["result_json"]).get("rejections", []))
        return items + current


def _first_difference(left: Any, right: Any, path: str = "$") -> dict[str, str]:
    if type(left) is not type(right):
        return {"path": path, "kind": "type", "left": type(left).__name__, "right": type(right).__name__}
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return {"path": f"{path}.{key}", "kind": "missing_key"}
            diff = _first_difference(left[key], right[key], f"{path}.{key}")
            if diff:
                return diff
    elif isinstance(left, list):
        if len(left) != len(right):
            return {"path": path, "kind": "length", "left": len(left), "right": len(right)}
        for index, (a, b) in enumerate(zip(left, right)):
            diff = _first_difference(a, b, f"{path}[{index}]")
            if diff:
                return diff
    elif left != right:
        return {"path": path, "kind": "value", "left": repr(left)[:200], "right": repr(right)[:200]}
    return {}


class BatchScheduler:
    """Bounded in-process scheduler with real worker concurrency and queue budget."""

    def __init__(self, ledger_path: str | Path, budget: Budget | None = None, sync_markers: dict[str, dict[str, Any]] | None = None):
        self.ledger_path = ledger_path
        self.budget = budget or Budget()
        self.sync_markers = sync_markers or {}
        self.engine = AuditEngine(ledger_path, self.sync_markers)
        self.apply_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, self.budget.concurrency + self.budget.queue_limit))
        self._executor = None
        self._active = 0
        self._peak_active = 0
        self._lock = threading.Lock()

    def _run(self, batch: Batch) -> dict[str, Any]:
        with self._lock:
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
        try:
            started = time.perf_counter()
            deadline = started + self.budget.batch_timeout_ms / 1000.0
            if batch.started_event is not None:
                batch.started_event.set()
            if batch.proceed_event is not None:
                batch.proceed_event.wait(timeout=30)
            try:
                prepared, rejections = prepare_batch(batch, self.budget, deadline)
            except AuditError as exc:
                return {
                    "batch_id": batch.batch_id,
                    "status": "error",
                    "code": exc.code,
                    "codes": [exc.code],
                    "message": exc.message,
                    "rejections": [],
                    "transaction_committed": False,
                    "elapsed_ms_real": round((time.perf_counter() - started) * 1000, 3),
                }
            with self.apply_lock:
                return self.engine.apply_prepared(batch, prepared, rejections, started)
        finally:
            with self._lock:
                self._active -= 1
            self._slots.release()

    def submit(self, batch: Batch) -> tuple[bool, Any]:
        if not self._slots.acquire(blocking=False):
            return False, {
                "batch_id": batch.batch_id,
                "status": "rejected",
                "code": E_QUEUE_FULL,
                "codes": [E_QUEUE_FULL],
                "message": f"concurrency+queue capacity {self.budget.concurrency + self.budget.queue_limit} exceeded",
            }
        from concurrent.futures import ThreadPoolExecutor

        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=max(1, self.budget.concurrency), thread_name_prefix="sbom-batch")
        future = self._executor.submit(self._run, batch)
        return True, future

    def run_all(self, batches: list[Batch]) -> list[dict[str, Any]]:
        futures: list[tuple[str, Any]] = []
        reports: list[dict[str, Any]] = []
        for batch in batches:
            accepted, value = self.submit(batch)
            if accepted:
                futures.append((batch.batch_id, value))
            else:
                reports.append(value)
        for _, future in futures:
            reports.append(future.result())
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None
        reports.sort(key=lambda r: r.get("batch_id", ""))
        return reports

    @property
    def peak_active(self) -> int:
        return self._peak_active
