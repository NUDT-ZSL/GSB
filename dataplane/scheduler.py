from __future__ import annotations

import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import connect, get_meta, immediate, set_meta
from .engine import (
    _finalize_materialized,
    active_config_row,
    adjudicate_batch,
    block_lock_path,
    execute_task,
    row_to_dict,
)
from .errors import BizError, ErrorCode
from .runtime import file_lock, host_owner


def recover_stale_tasks(
    conn: sqlite3.Connection,
    db_path: Path,
    data_dir: Path,
    clock_ms: int,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM block_task WHERE state IN ('running', 'materialized') ORDER BY task_uid"
    ).fetchall()
    recovered = []
    for row in rows:
        task = row_to_dict(row)
        lock_path = block_lock_path(data_dir, task["task_uid"])
        try:
            lock = file_lock(lock_path, blocking=False)
            lock_cm = lock.__enter__()
        except BizError as exc:
            if exc.code == ErrorCode.LEASE_BUSY:
                recovered.append(
                    {"task_uid": task["task_uid"], "action": "skipped_live_lease"}
                )
                continue
            raise
        try:
            current = row_to_dict(
                conn.execute(
                    "SELECT * FROM block_task WHERE task_uid=?", (task["task_uid"],)
                ).fetchone()
            )
            if current["state"] == "committed":
                action = "already_committed"
            elif current["state"] == "materialized":
                result = _finalize_materialized(conn, current, data_dir, clock_ms)
                action = "finalized_materialized" if result else "reset_missing_object"
            else:
                with immediate(conn):
                    conn.execute(
                        "UPDATE block_task SET state='pending', lease_owner=NULL, "
                        "lease_nonce=NULL, failure_code='recovered_interrupted_lease', "
                        "failure_message='stale running lease reset by recovery', "
                        "updated_clock_ms=? WHERE task_uid=? AND state='running'",
                        (clock_ms, task["task_uid"]),
                    )
                action = "reset_running_to_pending"
            recovered.append({"task_uid": task["task_uid"], "action": action})
        finally:
            try:
                lock_cm.__exit__(None, None, None)
            except Exception:
                pass
    return recovered


def _candidate_tasks(
    conn: sqlite3.Connection,
    case_id: Optional[str],
    retry_failed: bool,
    max_attempts: int,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM block_task WHERE state='pending'"
    params: List[Any] = []
    if retry_failed:
        sql += " OR (state='failed' AND attempt < ?)"
        params.append(max_attempts)
    if case_id:
        if "OR" in sql:
            sql = f"SELECT * FROM ({sql}) WHERE case_id=?"
        else:
            sql += " AND case_id=?"
        params.append(case_id)
    sql += " ORDER BY case_id ASC, block_index ASC, config_version ASC, revision ASC"
    return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def run_round(
    conn: sqlite3.Connection,
    db_path: Path | str,
    data_dir: Path | str,
    clock_ms: int,
    budget_ms: int = 10_000,
    concurrency: int = 2,
    case_id: Optional[str] = None,
    retry_failed: bool = False,
    max_attempts: int = 2,
    scope: str = "round",
) -> Dict[str, Any]:
    db_path = Path(db_path)
    data_dir = Path(data_dir)
    if not isinstance(budget_ms, int) or isinstance(budget_ms, bool) or budget_ms < 0:
        raise BizError(ErrorCode.COMMAND_FAILED, "budget_ms must be a non-negative integer")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
        raise BizError(ErrorCode.COMMAND_FAILED, "concurrency must be a positive integer")

    candidates = _candidate_tasks(conn, case_id, retry_failed, max_attempts)
    selected: List[Dict[str, Any]] = []
    unselected: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    used_budget = 0
    budget_exhausted = False
    for task in candidates:
        cost = int(task["cost_ms"])
        if budget_exhausted:
            unselected.append((task, "abandoned", "budget_exhausted", "insufficient"))
            continue
        if used_budget + cost > budget_ms:
            budget_exhausted = True
            unselected.append((task, "abandoned", "budget_exhausted", "insufficient"))
            continue
        if len(selected) >= concurrency:
            unselected.append((task, "not_started", "concurrency_limit", "within_budget"))
            continue
        selected.append(task)
        used_budget += cost

    remaining_cost = sum(int(task["cost_ms"]) for task, *_ in unselected)
    round_id = f"round-{clock_ms}-{secrets.token_hex(4)}"
    plan = {
        "selected": [t["task_uid"] for t in selected],
        "unselected": [
            {"task_uid": t["task_uid"], "outcome": outcome, "reason": reason}
            for t, outcome, reason, _ in unselected
        ],
        "used_budget_ms": used_budget,
        "remaining_budget_ms": max(0, budget_ms - used_budget),
    }
    with immediate(conn):
        set_meta(conn, "scheduler_watermark_clock_ms", str(clock_ms))
        set_meta(conn, "last_round_id", round_id)
        conn.execute(
            "INSERT INTO schedule_round("
            "round_id,scope,clock_ms,budget_ms,concurrency_limit,config_version,status,"
            "candidate_count,selected_count,not_started_count,abandoned_count,"
            "remaining_blocks,remaining_cost_ms,report_json,created_clock_ms,updated_clock_ms) "
            "VALUES (?,?,?,?,?,?,'running',?,?,?,?,?,?,?,?,?)",
            (
                round_id,
                scope,
                clock_ms,
                budget_ms,
                concurrency,
                get_meta(conn, "active_config_version"),
                len(candidates),
                len(selected),
                sum(1 for _, o, _, _ in unselected if o == "not_started"),
                sum(1 for _, o, _, _ in unselected if o == "abandoned"),
                len(unselected),
                remaining_cost,
                json.dumps(plan, sort_keys=True),
                clock_ms,
                clock_ms,
            ),
        )

    if selected:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(selected))) as pool:
            futures = {
                task["task_uid"]: pool.submit(
                    execute_task,
                    task["task_uid"],
                    db_path,
                    data_dir,
                    clock_ms,
                    f"{host_owner()}:worker",
                )
                for task in selected
            }
            for task in selected:
                result = futures[task["task_uid"]].result()
                result["cost_ms"] = int(task["cost_ms"])
                decisions.append(result)

    completed = [d for d in decisions if d.get("state") == "committed"]
    failed = [d for d in decisions if d.get("state") == "failed"]
    item_rows = []
    for task in selected:
        result = next(d for d in decisions if d["task_uid"] == task["task_uid"])
        if result.get("state") == "committed":
            outcome = "completed"
            reason = "reused_committed" if result.get("reused") else "materialized"
            budget_state = "consumed"
        else:
            outcome = "failed_after_start"
            reason = result.get("error_code", ErrorCode.COMMAND_FAILED)
            budget_state = "consumed"
        item_rows.append(
            (
                round_id,
                task["task_uid"],
                task["case_id"],
                task["block_index"],
                outcome,
                reason,
                budget_state,
                task["cost_ms"],
                task["config_version"],
                result.get("decision_id"),
                result.get("error_code"),
                json.dumps(result.get("details", {}), sort_keys=True, default=str),
            )
        )
    for task, outcome, reason, budget_state in unselected:
        item_rows.append(
            (
                round_id,
                task["task_uid"],
                task["case_id"],
                task["block_index"],
                outcome,
                reason,
                budget_state,
                task["cost_ms"],
                task["config_version"],
                None,
                None,
                "{}",
            )
        )

    report = {
        "round_id": round_id,
        "scope": scope,
        "clock_ms": clock_ms,
        "budget_ms": budget_ms,
        "concurrency_limit": concurrency,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "completed_count": len(completed),
        "failed_count": len(failed),
        "not_started_count": sum(1 for _, o, _, _ in unselected if o == "not_started"),
        "abandoned_count": sum(1 for _, o, _, _ in unselected if o == "abandoned"),
        "remaining_blocks": len(unselected),
        "remaining_cost_ms": remaining_cost,
        "results": decisions,
        "unselected": [
            {
                "task_uid": task["task_uid"],
                "case_id": task["case_id"],
                "block_index": task["block_index"],
                "outcome": outcome,
                "reason": reason,
                "budget_state": budget_state,
                "cost_ms": task["cost_ms"],
            }
            for task, outcome, reason, budget_state in unselected
        ],
    }
    with immediate(conn):
        conn.executemany(
            "INSERT INTO round_item("
            "round_id,task_uid,case_id,block_index,outcome,reason,budget_state,"
            "cost_ms,config_version,decision_id,error_code,details_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            item_rows,
        )
        conn.execute(
            "UPDATE schedule_round SET status='completed', completed_count=?, failed_count=?, "
            "report_json=?, updated_clock_ms=? WHERE round_id=?",
            (
                len(completed),
                len(failed),
                json.dumps(report, sort_keys=True, separators=(",", ":"), default=str),
                clock_ms,
                round_id,
            ),
        )
    try:
        batch = adjudicate_batch(conn, clock_ms)
        report["batch"] = {
            "batch_id": batch["batch_id"],
            "decision_count": batch["decision_count"],
            "total_error_bound": batch["total_error_bound"],
            "precision_sensitive_count": batch["precision_sensitive_count"],
        }
    except BizError:
        pass
    return report


def run_until_settled(
    conn: sqlite3.Connection,
    db_path: Path | str,
    data_dir: Path | str,
    clock_ms: int,
    budget_ms: int = 10_000,
    concurrency: int = 2,
    case_id: Optional[str] = None,
    retry_failed: bool = False,
    max_attempts: int = 2,
    max_rounds: int = 1_000,
    scope: str = "scheduler",
) -> Dict[str, Any]:
    rounds = []
    for _ in range(max_rounds):
        report = run_round(
            conn,
            db_path,
            data_dir,
            clock_ms,
            budget_ms,
            concurrency,
            case_id,
            retry_failed,
            max_attempts,
            scope,
        )
        rounds.append(report)
        if report["selected_count"] == 0:
            break
        if report["abandoned_count"] > 0:
            break
        if report["not_started_count"] == 0:
            break
    return {
        "round_count": len(rounds),
        "rounds": rounds,
        "last_round_id": rounds[-1]["round_id"] if rounds else None,
        "completed_count": sum(r["completed_count"] for r in rounds),
        "failed_count": sum(r["failed_count"] for r in rounds),
        "not_started_count": rounds[-1]["not_started_count"] if rounds else 0,
        "abandoned_count": rounds[-1]["abandoned_count"] if rounds else 0,
        "remaining_blocks": rounds[-1]["remaining_blocks"] if rounds else 0,
        "remaining_cost_ms": rounds[-1]["remaining_cost_ms"] if rounds else 0,
    }
