"""SQLite persistence with trace-level transactions and resumable processing."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .engine import (
    RESULT_VERSION,
    assemble_result,
    build_trace_object,
    prepare_contexts,
    recheck_policy,
)
from .sampling import apply_policy_patch
from .util import canonical_clone, compact_json, sha256_json


STORE_FORMAT = "observatory-telemetry-store"
STORE_VERSION = "1.0"
SCHEMA_VERSION = "2"


class StoreError(RuntimeError):
    """Raised for an incompatible or incomplete telemetry store."""


class ProcessingInterrupted(KeyboardInterrupt):
    def __init__(self, committed_count: int) -> None:
        super().__init__(f"processing interrupted after {committed_count} commits")
        self.committed_count = committed_count


def _base_workload_hash(workload: dict[str, Any]) -> str:
    return sha256_json(
        {
            "projects": workload.get("projects", []),
            "seed": int(workload["seed"]),
            "version": workload.get("version"),
        }
    )


def _history_hash(history: list[dict[str, Any]]) -> str:
    return sha256_json(history)


class TelemetryStore:
    def __init__(self, path: str, *, reset: bool = False) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()
        self.reset = reset

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "TelemetryStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_traces (
                store_id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                trace_start_us INTEGER NOT NULL,
                span_count INTEGER NOT NULL,
                attribute_bytes INTEGER NOT NULL,
                retained INTEGER NOT NULL,
                head_kept INTEGER NOT NULL,
                tail_kept INTEGER NOT NULL,
                final_kept INTEGER NOT NULL,
                decision_sha256 TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                trace_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_processed_project_job
                ON processed_traces(project_id, job_id);
            CREATE INDEX IF NOT EXISTS idx_processed_time
                ON processed_traces(trace_start_us);
            """
        )

    def _get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def initialize(self, workload: dict[str, Any], *, reset: bool = False) -> None:
        seed = int(workload["seed"])
        base_hash = _base_workload_hash(workload)
        existing_seed = self._get_meta("seed")
        existing_hash = self._get_meta("base_workload_sha256")
        if reset:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute("DELETE FROM processed_traces")
                self.connection.execute("DELETE FROM meta")
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
            existing_seed = None
            existing_hash = None
        if existing_seed is not None:
            if existing_seed != str(seed):
                raise StoreError(
                    f"store seed mismatch: expected {existing_seed}, got {seed}"
                )
            if existing_hash != base_hash:
                raise StoreError("store workload hash mismatch; use a new store or --reset")
            if self._get_meta("schema_version") != SCHEMA_VERSION:
                raise StoreError("store schema version mismatch")
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._set_meta("format", STORE_FORMAT)
            self._set_meta("store_version", STORE_VERSION)
            self._set_meta("schema_version", SCHEMA_VERSION)
            self._set_meta("seed", str(seed))
            self._set_meta("base_workload_sha256", base_hash)
            self._set_meta("policy_patches_json", "[]")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def committed_ids(self) -> set[str]:
        return {
            str(row["trace_id"])
            for row in self.connection.execute(
                "SELECT trace_id FROM processed_traces"
            )
        }

    def committed_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM processed_traces"
        ).fetchone()
        return int(row["count"])

    def commit_trace(self, trace: dict[str, Any]) -> bool:
        decision = trace["sample_decision"]
        trace_json = compact_json(trace)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO processed_traces(
                    trace_id, project_id, job_id, trace_start_us,
                    span_count, attribute_bytes, retained,
                    head_kept, tail_kept, final_kept,
                    decision_sha256, decision_json, trace_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace["trace_id"],
                    trace["project_id"],
                    trace["job_id"],
                    int(trace["trace_start_us"]),
                    int(trace["span_count"]),
                    int(trace["attribute_bytes"]),
                    1 if trace["retained"] else 0,
                    1 if decision["head"]["kept"] else 0,
                    1 if decision["tail"]["kept"] else 0,
                    1 if decision["final"]["kept"] else 0,
                    sha256_json(decision),
                    compact_json(decision),
                    trace_json,
                ),
            )
            inserted = cursor.rowcount > 0
            self.connection.commit()
            return inserted
        except BaseException:
            self.connection.rollback()
            raise

    def load_traces(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in self.connection.execute(
            "SELECT trace_id, trace_json FROM processed_traces ORDER BY store_id"
        ):
            result[str(row["trace_id"])] = json.loads(str(row["trace_json"]))
        return result

    def policy_history(self) -> list[dict[str, Any]]:
        raw = self._get_meta("policy_patches_json") or "[]"
        history = json.loads(raw)
        if not isinstance(history, list):
            raise StoreError("policy patch history is corrupt")
        return history

    def _load_workload_with_history(
        self,
        base_workload: dict[str, Any],
        history: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        patches = self.policy_history() if history is None else history
        adjusted = canonical_clone(base_workload)
        for patch in patches:
            adjusted = apply_policy_patch(adjusted, patch)
        return adjusted, patches

    def update_traces_and_history(
        self,
        traces: Iterable[dict[str, Any]],
        history: list[dict[str, Any]],
    ) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for trace in traces:
                cursor = self.connection.execute(
                    """
                    UPDATE processed_traces
                       SET job_id = ?,
                           trace_start_us = ?,
                           span_count = ?,
                           attribute_bytes = ?,
                           retained = ?,
                           head_kept = ?,
                           tail_kept = ?,
                           final_kept = ?,
                           decision_sha256 = ?,
                           decision_json = ?,
                           trace_json = ?
                     WHERE trace_id = ?
                    """,
                    (
                        trace["job_id"],
                        int(trace["trace_start_us"]),
                        int(trace["span_count"]),
                        int(trace["attribute_bytes"]),
                        1 if trace["retained"] else 0,
                        1 if trace["sample_decision"]["head"]["kept"] else 0,
                        1 if trace["sample_decision"]["tail"]["kept"] else 0,
                        1 if trace["sample_decision"]["final"]["kept"] else 0,
                        sha256_json(trace["sample_decision"]),
                        compact_json(trace["sample_decision"]),
                        compact_json(trace),
                        trace["trace_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise StoreError(
                        f"cannot update uncommitted trace {trace['trace_id']}"
                    )
            self._set_meta("policy_patches_json", compact_json(history))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def append_policy_patch(self, patch: dict[str, Any]) -> None:
        history = self.policy_history() + [canonical_clone(patch)]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._set_meta("policy_patches_json", compact_json(history))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def storage_summary(
        self,
        *,
        newly_committed: int = 0,
        skipped_committed: int = 0,
    ) -> dict[str, Any]:
        history = self.policy_history()
        return {
            "base_workload_sha256": self._get_meta("base_workload_sha256"),
            "committed_trace_count": self.committed_count(),
            "policy_history_length": len(history),
            "policy_history_sha256": _history_hash(history),
            "store_format": STORE_FORMAT,
            "store_version": STORE_VERSION,
        }


def process_workload(
    store: TelemetryStore,
    workload: dict[str, Any],
    *,
    interrupt_after: int | None = None,
    commit_delay_ms: int = 0,
) -> dict[str, Any]:
    if interrupt_after is not None and interrupt_after < 0:
        raise ValueError("interrupt_after cannot be negative")
    contexts = prepare_contexts(workload)
    expected_ids = {
        descriptor["trace_id"]
        for context in contexts
        for descriptor in context["descriptors"]
    }
    already_committed = store.committed_ids()
    skipped = len(expected_ids & already_committed)
    newly_committed = 0
    if interrupt_after == 0 and expected_ids - already_committed:
        raise ProcessingInterrupted(0)
    for context in contexts:
        for descriptor in context["descriptors"]:
            trace_id = descriptor["trace_id"]
            if trace_id in already_committed:
                continue
            trace = build_trace_object(context, descriptor)
            inserted = store.commit_trace(trace)
            if inserted:
                newly_committed += 1
                already_committed.add(trace_id)
            if commit_delay_ms > 0:
                time.sleep(commit_delay_ms / 1_000)
            if interrupt_after is not None and newly_committed >= interrupt_after:
                remaining = expected_ids - already_committed
                if remaining:
                    raise ProcessingInterrupted(newly_committed)
    return {
        "expected_trace_count": len(expected_ids),
        "newly_committed_trace_count": newly_committed,
        "skipped_committed_trace_count": skipped,
    }


def build_store_report(
    store: TelemetryStore,
    base_workload: dict[str, Any],
    *,
    newly_committed: int = 0,
    skipped_committed: int = 0,
) -> dict[str, Any]:
    adjusted_workload, history = store._load_workload_with_history(base_workload)
    contexts = prepare_contexts(adjusted_workload)
    expected_ids = {
        descriptor["trace_id"]
        for context in contexts
        for descriptor in context["descriptors"]
    }
    traces = store.load_traces()
    missing = sorted(expected_ids - set(traces))
    if missing:
        raise StoreError(
            f"store is incomplete: {len(missing)} trace(s) remain, first={missing[0]}"
        )
    extra = sorted(set(traces) - expected_ids)
    if extra:
        raise StoreError(f"store contains unexpected traces: {extra[:3]}")
    result = assemble_result(adjusted_workload, contexts, traces)
    storage = store.storage_summary()
    storage["expected_trace_count"] = len(expected_ids)
    storage["recovery_idempotent"] = True
    return {
        "format": "observatory-persistent-report",
        "result": result,
        "storage": storage,
        "version": RESULT_VERSION,
    }


def apply_persistent_recheck(
    store: TelemetryStore,
    base_workload: dict[str, Any],
    patch: dict[str, Any],
    *,
    job_ids: Iterable[str] | None = None,
    project_ids: Iterable[str] | None = None,
    from_us: int | None = None,
    to_us: int | None = None,
    now_us_override: int | None = None,
) -> dict[str, Any]:
    current_workload, history = store._load_workload_with_history(base_workload)
    base_report = build_store_report(store, base_workload)
    recheck = recheck_policy(
        current_workload,
        patch,
        base_result=base_report["result"],
        job_ids=job_ids,
        project_ids=project_ids,
        from_us=from_us,
        to_us=to_us,
        now_us_override=now_us_override,
    )
    if not recheck["consistency"]["all_equal"]:
        raise StoreError(
            "incremental re-evaluation diverged from the full re-evaluation; "
            "refusing to persist"
        )
    trace_ids_to_update = set(recheck["evaluated_trace_ids"]) | set(
        recheck["retention_updated_trace_ids"]
    )
    adjusted_traces = {
        trace["trace_id"]: trace
        for project in recheck["adjusted_result"]["projects"]
        for trace in project["traces"]
    }
    updates = [adjusted_traces[trace_id] for trace_id in sorted(trace_ids_to_update)]
    new_history = history + [canonical_clone(patch)]
    store.update_traces_and_history(updates, new_history)

    stored_report = build_store_report(store, base_workload)
    stored_projection = {
        trace["trace_id"]: trace
        for project in stored_report["result"]["projects"]
        for trace in project["traces"]
    }
    local_projection = adjusted_traces
    stored_equals_local = compact_json(stored_projection) == compact_json(local_projection)
    recheck["adjusted_result"] = stored_report["result"]
    recheck["consistency"]["stored_equals_local"] = stored_equals_local
    recheck["consistency"]["all_equal"] = bool(
        recheck["consistency"]["all_equal"] and stored_equals_local
    )
    recheck["storage"] = stored_report["storage"]
    return recheck
