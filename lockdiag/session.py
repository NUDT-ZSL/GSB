"""High-level run/resume orchestration."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .engine import DiagnosisEngine, recover_state
from .errors import DiagnosticError, E_SAMPLE_MISMATCH, E_STATE_EXISTS, E_STATE_CORRUPT
from .filelock import file_lock
from .model import SampleBundle
from .store import StateStore
from .util import canonical_json
from .validation import load_and_validate, rehydrate_bundle
from .worker import probe_all


def _operation_records(bundle: SampleBundle) -> tuple[list[dict], list[dict]]:
    transactions: list[dict] = []
    operations: list[dict] = []
    for tx in bundle.transactions:
        transactions.append(
            {
                "tx_id": tx.tx_id,
                "start_order": tx.start_order,
                "version": tx.version,
                "isolation": tx.isolation,
                "budget_ms": tx.budget_ms,
                "depends_on": list(tx.depends_on),
            }
        )
        for operation in tx.operations:
            data = dict(operation.data)
            data.update(
                {
                    "tx_id": tx.tx_id,
                    "op_seq": operation.op_seq,
                    "op_version": operation.op_version,
                    "resource_key": operation.resource_key,
                    "resource_type": operation.resource_type,
                    "table_name": operation.table,
                    "row_id": operation.row_id,
                    "range_start": operation.range_start,
                    "range_end": operation.range_end,
                    "requested_mode": operation.mode,
                    "index_path": None,
                }
            )
            operations.append(data)
    return transactions, operations


def prepare_plans(store: StateStore, bundle: SampleBundle) -> int:
    """Probe all operations that do not yet have a persisted index path."""

    connection = store.open()
    rows = connection.execute(
        """
        SELECT * FROM operations
        WHERE plan_json IS NULL
        ORDER BY tx_id, op_seq
        """
    ).fetchall()
    if not rows:
        return 0
    tasks: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload.update(
            {
                "tx_id": row["tx_id"],
                "op_seq": row["op_seq"],
                "op_version": row["op_version"],
            }
        )
        tasks.append(payload)
    manifest = store.get_manifest()
    results = probe_all(
        manifest["run_db_path"], tasks, bundle.config.worker_processes
    )
    by_key = {(item["tx_id"], item["op_seq"], item["op_version"]): item for item in results}
    with store.bundle(
        "probe-plans",
        "SYSTEM",
        "PLAN_PROBE",
        None,
        {"operations": len(tasks)},
    ) as connection:
        for row in rows:
            key = (row["tx_id"], row["op_seq"], row["op_version"])
            result = by_key.get(key)
            if result is None:
                raise DiagnosticError(
                    E_STATE_CORRUPT, f"worker probe did not return {key}"
                )
            connection.execute(
                """
                UPDATE operations
                SET index_path=?, plan_json=?, worker_pid=?
                WHERE tx_id=? AND op_seq=? AND op_version=?
                """,
                (
                    result["index_path"],
                    canonical_json(
                        {
                            "sql": result.get("sql", ""),
                            "plan": result.get("plan", []),
                            "rows": result.get("rows", []),
                        }
                    ),
                    result.get("worker_pid"),
                    row["tx_id"],
                    row["op_seq"],
                    row["op_version"],
                ),
            )
    return len(rows)


def _bundle_for_state(
    store: StateStore, sample_dir: str | Path | None
) -> SampleBundle:
    manifest = store.get_manifest()
    if sample_dir is not None:
        bundle = load_and_validate(sample_dir)
        if bundle.fingerprint != manifest["fingerprint"]:
            raise DiagnosticError(
                E_SAMPLE_MISMATCH,
                "sample input does not match the state fingerprint",
                {
                    "state_fingerprint": manifest["fingerprint"],
                    "sample_fingerprint": bundle.fingerprint,
                },
            )
        return bundle
    return rehydrate_bundle(manifest["manifest"], manifest["run_db_path"])


def create_run(
    sample_dir: str | Path,
    state_dir: str | Path,
    crash_at: str | None = None,
) -> dict[str, Any]:
    bundle = load_and_validate(sample_dir)
    state = Path(state_dir).resolve()
    if StateStore.exists(state):
        raise DiagnosticError(E_STATE_EXISTS, f"state already exists: {state}")
    if state.exists() and any(state.iterdir()):
        raise DiagnosticError(E_STATE_EXISTS, f"state directory is not empty: {state}")
    state.mkdir(parents=True, exist_ok=True)
    with file_lock(state / "state.lock"):
        if StateStore.exists(state):
            raise DiagnosticError(E_STATE_EXISTS, f"state already exists: {state}")
        run_db = state / "sample_run.db"
        shutil.copy2(bundle.db_path, run_db)
        transactions, operations = _operation_records(bundle)
        store = StateStore(state)
        try:
            store.create(bundle, run_db, transactions, operations)
            prepare_plans(store, bundle)
            engine = DiagnosisEngine(store, bundle, run_db, crash_at=crash_at)
            engine.run()
            return summarize_state(store)
        finally:
            store.close()


def resume_run(
    state_dir: str | Path,
    sample_dir: str | Path | None = None,
    crash_at: str | None = None,
) -> dict[str, Any]:
    state = Path(state_dir).resolve()
    store = StateStore(state)
    with file_lock(state / "state.lock"):
        store.open()
        try:
            bundle = _bundle_for_state(store, sample_dir)
            manifest = store.get_manifest()
            run_db = Path(manifest["run_db_path"])
            recover_state(store, bundle, run_db)
            prepare_plans(store, bundle)
            engine = DiagnosisEngine(store, bundle, run_db, crash_at=crash_at)
            engine.run()
            return summarize_state(store)
        finally:
            store.close()


def summarize_state(store: StateStore) -> dict[str, Any]:
    payload = store.status_payload()
    transactions = payload["transactions"]
    return {
        "ok": True,
        "run_status": payload["run"]["status"] if payload["run"] else None,
        "scheduler_steps": payload["schedule"]["steps"] if payload["schedule"] else None,
        "wait_count": sum(int(tx["wait_count"]) for tx in transactions),
        "transaction_statuses": {
            tx["tx_id"]: tx["status"] for tx in transactions
        },
        "deadlocks": len(payload["deadlocks"]),
        "queue_depth": sum(1 for item in payload["queue"] if item["status"] == "QUEUED"),
    }
