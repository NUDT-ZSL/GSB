from __future__ import annotations

import os
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .canonical import sha256_file
from .clock import check_clock
from .errors import StateConflictError
from .heat import evaluate_heat, rank_candidates
from .ledger import build_object_ledger, compare_costs
from .storage import StateStore


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def heat_config_from_state(config: dict[str, Any], policy_entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    heat_config = dict(config.get("heat", {}))
    heat_config.setdefault("recency_window_ms", config.get("recency_window_ms", 600_000))
    heat_config.setdefault("frequency_unit_bp", config.get("frequency_unit_bp", 200))
    heat_config.setdefault("recency_weight", 60)
    heat_config.setdefault("frequency_weight", 25)
    heat_config.setdefault("weight_weight", 15)
    heat_config["hot_threshold"] = int(config["hot_threshold_bp"])
    heat_config["cold_threshold"] = int(config["cold_threshold_bp"])
    mapping = {
        "heat.hot_threshold": "hot_threshold",
        "heat.cold_threshold": "cold_threshold",
        "heat.recency_window_ms": "recency_window_ms",
        "heat.frequency_unit_bp": "frequency_unit_bp",
    }
    for policy_key, config_key in mapping.items():
        entry = policy_entries.get(policy_key)
        if entry is not None and isinstance(entry.get("value"), int):
            heat_config[config_key] = int(entry["value"])
    return heat_config


def adjudicate(store: StateStore, clock_ms: int) -> dict[str, Any]:
    config = store.get_config()
    watermark, tolerance = store.get_clock_watermark()
    clock_check = check_clock(
        clock_ms,
        watermark,
        int(config.get("clock_rollback_tolerance_ms", tolerance)),
    )
    entries = store.current_policy_entries()
    heat_config = heat_config_from_state(config, entries)
    objects = store.get_objects()
    judgements: list[dict[str, Any]] = []
    for row in objects:
        obj = dict(row)
        obj["key"] = obj["object_key"]
        judgements.append(evaluate_heat(obj, clock_ms, heat_config))
    judgements = sorted(judgements, key=lambda item: item["object_key"])
    return {
        "clock_check": clock_check.__dict__,
        "heat_config": heat_config,
        "judgements": judgements,
    }


def _compare_candidate_order(a: dict[str, Any], b: dict[str, Any], tolerance: int) -> int:
    if a["urgency"] != b["urgency"]:
        return -1 if a["urgency"] > b["urgency"] else 1
    cost_a = int(a["ledger"]["total_cost_scaled"])
    cost_b = int(b["ledger"]["total_cost_scaled"])
    if abs(cost_a - cost_b) < tolerance:
        key_cmp = (a["object_key"] > b["object_key"]) - (a["object_key"] < b["object_key"])
        return key_cmp
    return -1 if cost_a < cost_b else 1


def build_candidate_plan(store: StateStore, clock_ms: int) -> dict[str, Any]:
    adjudication = adjudicate(store, clock_ms)
    config = store.get_config()
    cost_config = dict(config["cost_model"])
    candidates: list[dict[str, Any]] = []
    for judgement in rank_candidates(adjudication["judgements"]):
        if judgement["direction"] == "none":
            continue
        obj = next(row for row in store.get_objects() if row["object_key"] == judgement["object_key"])
        obj = dict(obj)
        obj["key"] = obj["object_key"]
        ledger = build_object_ledger(obj, cost_config)
        candidates.append({**judgement, "ledger": ledger})

    tolerance = int(cost_config["difference_tolerance_scaled"])
    raw_order = sorted(
        candidates,
        key=lambda item: (-item["urgency"], int(item["ledger"]["total_cost_scaled"]), item["object_key"]),
    )
    precision_pairs: list[dict[str, Any]] = []
    sensitive_keys: set[str] = set()
    for left, right in zip(raw_order, raw_order[1:]):
        if left["urgency"] != right["urgency"]:
            continue
        comparison = compare_costs(left["ledger"], right["ledger"], tolerance)
        if comparison["precision_sensitive"]:
            comparison["scheduling_dependency"] = (
                "adjacent candidates share urgency; dispatch rank uses fixed-point cost difference"
            )
            comparison["boundary_effect"] = (
                "the first candidate consumes the remaining byte budget when only one fits"
            )
            precision_pairs.append(comparison)
            sensitive_keys.add(left["object_key"])
            sensitive_keys.add(right["object_key"])

    import functools

    safe_order = sorted(
        candidates,
        key=functools.cmp_to_key(lambda a, b: _compare_candidate_order(a, b, tolerance)),
    )
    for rank, candidate in enumerate(safe_order):
        candidate["plan_rank"] = rank
        candidate["precision_sensitive"] = candidate["object_key"] in sensitive_keys
    return {
        "adjudication": adjudication,
        "candidates": safe_order,
        "precision_pairs": precision_pairs,
        "cost_tolerance_scaled": tolerance,
    }


def create_plan(
    store: StateStore,
    clock_ms: int,
    round_id: str,
    *,
    budget_bytes: int | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    plan = build_candidate_plan(store, clock_ms)
    config = store.get_config()
    policy = store.current_policy()
    if policy is None:
        raise StateConflictError("no current policy snapshot; run merge before migrate")
    policy_version = int(policy["manifest_version"])
    final_budget = int(budget_bytes if budget_bytes is not None else config["migration_byte_budget"])
    worker_concurrency = int(concurrency if concurrency is not None else config["max_concurrency"])
    global_concurrency = int(config["max_concurrency"])
    if final_budget <= 0:
        raise ValueError("budget_bytes must be positive")
    if worker_concurrency <= 0 or worker_concurrency > global_concurrency:
        raise ValueError("concurrency must be positive and no greater than configured max_concurrency")

    with store.write() as conn:
        store.advance_clock(conn, clock_ms, round_id)
        conn.execute(
            """
            INSERT INTO rounds(round_id, policy_version, budget_bytes, concurrency_limit,
                               status, created_clock_ms)
            VALUES(?, ?, ?, ?, 'open', ?)
            ON CONFLICT(round_id) DO NOTHING
            """,
            (round_id, policy_version, final_budget, global_concurrency, clock_ms),
        )
        for candidate in plan["candidates"]:
            key = candidate["object_key"]
            job_key = f"{round_id}:v{policy_version}:{key}"
            conn.execute(
                """
                INSERT OR IGNORE INTO migration_jobs(
                    job_key, idempotency_key, round_id, policy_version, object_key,
                    source_tier, target_tier, direction, size_bytes, urgency, plan_rank,
                    cost_total_scaled, cost_error_bound, precision_sensitive,
                    status, outcome_category, planned_clock_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned',
                         'within_budget_unstarted', ?)
                """,
                (
                    job_key,
                    f"migrate:{round_id}:v{policy_version}:{key}",
                    round_id,
                    policy_version,
                    key,
                    candidate["current_tier"],
                    candidate["target_tier"],
                    candidate["direction"],
                    int(next(o for o in store.get_objects() if o["object_key"] == key)["size_bytes"]),
                    int(candidate["urgency"]),
                    int(candidate["plan_rank"]),
                    int(candidate["ledger"]["total_cost_scaled"]),
                    candidate["ledger"]["error_bound_scaled"],
                    1 if candidate["precision_sensitive"] else 0,
                    clock_ms,
                ),
            )
            ledger_key = f"{round_id}:v{policy_version}:{key}"
            conn.execute(
                """
                INSERT OR IGNORE INTO cost_ledgers(
                    ledger_key, round_id, policy_version, object_key,
                    total_cost_scaled, ledger_json, updated_clock_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ledger_key,
                    round_id,
                    policy_version,
                    key,
                    int(candidate["ledger"]["total_cost_scaled"]),
                    __import__("tierflow.canonical", fromlist=["canonical_dumps"]).canonical_dumps(
                        candidate["ledger"], indent=None
                    ),
                    clock_ms,
                ),
            )
        _project_abandonments(conn, round_id, clock_ms)
    plan["round_id"] = round_id
    plan["policy_version"] = policy_version
    plan["budget_bytes"] = final_budget
    plan["worker_concurrency"] = worker_concurrency
    plan["global_concurrency_limit"] = global_concurrency
    return plan


def _project_abandonments(conn: Any, round_id: str, clock_ms: int) -> None:
    round_row = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
    if round_row is None:
        return
    round_info = dict(round_row)
    remaining_budget = int(round_info["budget_bytes"]) - int(round_info["claimed_bytes"])
    available_by_tier = {
        row["tier"]: int(row["capacity_bytes"]) - int(row["used_bytes"]) - int(row["reserved_bytes"])
        for row in conn.execute("SELECT * FROM capacity_accounts")
    }
    rows = conn.execute(
        "SELECT * FROM migration_jobs WHERE round_id=? ORDER BY plan_rank ASC, object_key ASC",
        (round_id,),
    ).fetchall()
    halt_reason: str | None = None
    for row in rows:
        job = dict(row)
        if job["status"] != "planned":
            continue
        size = int(job["size_bytes"])
        target_tier = job["target_tier"]
        if halt_reason is not None:
            reason = halt_reason
        elif size > remaining_budget:
            reason = f"byte_budget_insufficient: size={size} remaining_budget={remaining_budget}"
            halt_reason = "prior_candidate_exhausted_byte_budget"
        elif size > available_by_tier.get(target_tier, 0):
            available = available_by_tier.get(target_tier, 0)
            reason = f"capacity_insufficient: tier={target_tier} size={size} available={available}"
            halt_reason = f"prior_candidate_exhausted_{target_tier}_capacity"
        else:
            reason = None
            remaining_budget -= size
            available_by_tier[target_tier] -= size
        if reason is not None:
            conn.execute(
                """
                UPDATE migration_jobs
                SET status='abandoned', outcome_category='voluntarily_abandoned',
                    fail_reason=?, finished_clock_ms=?
                WHERE job_key=? AND status='planned'
                """,
                (reason, clock_ms, job["job_key"]),
            )


def _live_active_count(conn: Any, round_id: str) -> int:
    rows = conn.execute(
        "SELECT owner_pid, lease_expires_at FROM migration_jobs "
        "WHERE round_id = ? AND status IN ('claimed', 'running', 'committing')",
        (round_id,),
    ).fetchall()
    now_real = time.time()
    live = 0
    for row in rows:
        stale = False
        if row["owner_pid"] and not pid_alive(int(row["owner_pid"])):
            stale = True
        elif row["lease_expires_at"] is not None and float(row["lease_expires_at"]) < now_real:
            stale = True
        if not stale:
            live += 1
    return live


def claim_next(
    store: StateStore,
    round_id: str,
    owner_id: str,
    clock_ms: int,
    *,
    lease_seconds: float = 30.0,
) -> dict[str, Any]:
    with store.write() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE round_id = ?", (round_id,)).fetchone()
        if round_row is None:
            raise StateConflictError(f"unknown round {round_id}")
        round_info = dict(round_row)
        if _live_active_count(conn, round_id) >= int(round_info["concurrency_limit"]):
            return {"status": "concurrency_busy"}

        row = conn.execute(
            "SELECT * FROM migration_jobs WHERE round_id = ? AND status = 'planned' "
            "ORDER BY plan_rank ASC, object_key ASC LIMIT 1",
            (round_id,),
        ).fetchone()
        if row is None:
            return {"status": "no_job"}
        job = dict(row)
        size = int(job["size_bytes"])
        remaining_budget = int(round_info["budget_bytes"]) - int(round_info["claimed_bytes"])
        if size > remaining_budget:
            conn.execute(
                """
                UPDATE migration_jobs
                SET status='abandoned', outcome_category='voluntarily_abandoned',
                    fail_reason=?, finished_clock_ms=?
                WHERE round_id=? AND status='planned' AND plan_rank = ?
                """,
                (
                    f"byte_budget_insufficient: size={size} remaining_budget={remaining_budget}",
                    clock_ms,
                    round_id,
                    job["plan_rank"],
                ),
            )
            conn.execute(
                """
                UPDATE migration_jobs
                SET status='abandoned', outcome_category='voluntarily_abandoned',
                    fail_reason=?, finished_clock_ms=?
                WHERE round_id=? AND status='planned' AND plan_rank > ?
                """,
                (
                    "prior_candidate_exhausted_byte_budget",
                    clock_ms,
                    round_id,
                    job["plan_rank"],
                ),
            )
            store.advance_clock(conn, clock_ms, round_id)
            return {"status": "abandoned", "reason": "byte_budget", "job": job}

        account = conn.execute(
            "SELECT * FROM capacity_accounts WHERE tier = ?",
            (job["target_tier"],),
        ).fetchone()
        available = int(account["capacity_bytes"]) - int(account["used_bytes"]) - int(account["reserved_bytes"])
        if size > available:
            conn.execute(
                """
                UPDATE migration_jobs
                SET status='abandoned', outcome_category='voluntarily_abandoned',
                    fail_reason=?, finished_clock_ms=?
                WHERE round_id=? AND status='planned' AND plan_rank = ?
                """,
                (
                    f"capacity_insufficient: tier={job['target_tier']} size={size} available={available}",
                    clock_ms,
                    round_id,
                    job["plan_rank"],
                ),
            )
            conn.execute(
                """
                UPDATE migration_jobs
                SET status='abandoned', outcome_category='voluntarily_abandoned',
                    fail_reason=?, finished_clock_ms=?
                WHERE round_id=? AND status='planned' AND plan_rank > ?
                """,
                (
                    f"prior_candidate_exhausted_{job['target_tier']}_capacity",
                    clock_ms,
                    round_id,
                    job["plan_rank"],
                ),
            )
            store.advance_clock(conn, clock_ms, round_id)
            return {"status": "abandoned", "reason": "capacity", "job": job}

        conn.execute(
            """
            UPDATE migration_jobs
            SET status='claimed', outcome_category='claimed', claimed_clock_ms=?,
                owner_id=?, owner_pid=?, lease_expires_at=?, attempt_count=attempt_count+1
            WHERE job_key=? AND status='planned'
            """,
            (clock_ms, owner_id, os.getpid(), time.time() + lease_seconds, job["job_key"]),
        )
        updated = dict(
            conn.execute("SELECT * FROM migration_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
        )
        conn.execute(
            "UPDATE rounds SET claimed_bytes = claimed_bytes + ? WHERE round_id=?",
            (size, round_id),
        )
        conn.execute(
            """
            UPDATE capacity_accounts
            SET reserved_bytes = reserved_bytes + ?, updated_clock_ms=?
            WHERE tier=? AND used_bytes + reserved_bytes + ? <= capacity_bytes
            """,
            (size, clock_ms, job["target_tier"], size),
        )
        capacity_row = conn.execute(
            "SELECT changes() AS row_count"
        ).fetchone()
        if int(capacity_row["row_count"]) != 1:
            raise StateConflictError("capacity reservation violated")
        attempt = int(updated["attempt_count"])
        conn.execute(
            """
            INSERT INTO capacity_ledger(entry_key, round_id, job_key, tier,
                                        delta_used, delta_reserved, reason, clock_ms)
            VALUES(?, ?, ?, ?, 0, ?, 'reserve_destination', ?)
            """,
            (f"{job['job_key']}:reserve:{attempt}", round_id, job["job_key"], job["target_tier"], size, clock_ms),
        )
        store.advance_clock(conn, clock_ms, round_id)
        return {"status": "claimed", "job": updated}


def mark_running(store: StateStore, job_key: str, owner_id: str, clock_ms: int) -> None:
    with store.write() as conn:
        conn.execute(
            """
            UPDATE migration_jobs
            SET status='running', outcome_category='running', started_clock_ms=?
            WHERE job_key=? AND (
                status='claimed' AND owner_id=?
                OR (status='running' AND owner_id=?)
            )
            """,
            (clock_ms, job_key, owner_id, owner_id),
        )


def mark_committing(store: StateStore, job_key: str, owner_id: str, clock_ms: int) -> None:
    with store.write() as conn:
        conn.execute(
            """
            UPDATE migration_jobs
            SET status='committing', outcome_category='half_committed', finished_clock_ms=NULL
            WHERE job_key=? AND owner_id=? AND status IN ('running', 'committing')
            """,
            (job_key, owner_id),
        )


def simulate_transfer(obj: dict[str, Any], *, task_delay_seconds: float, fail_objects: set[str]) -> None:
    if task_delay_seconds > 0:
        time.sleep(task_delay_seconds)
    if obj["key"] in fail_objects:
        raise RuntimeError(f"simulated transfer failure for {obj['key']}")
    blob_path = obj.get("blob_path")
    if blob_path and Path(blob_path).is_file():
        actual = sha256_file(blob_path)
        expected = str(obj["content_digest"])
        if expected.startswith("sha256:"):
            expected = expected.split(":", 1)[1]
        if actual != expected.lower():
            raise StateConflictError(f"digest changed during transfer: {obj['key']}")


def finalize_success(
    store: StateStore,
    job: dict[str, Any],
    clock_ms: int,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .canonical import canonical_dumps

    with store.write() as conn:
        current = conn.execute(
            "SELECT * FROM migration_jobs WHERE job_key=?", (job["job_key"],)
        ).fetchone()
        if current is None:
            raise StateConflictError("job disappeared")
        current = dict(current)
        if current["status"] == "succeeded":
            return {"status": "already_succeeded", "job_key": job["job_key"]}
        if current["status"] not in {"claimed", "running", "committing"}:
            raise StateConflictError(f"cannot finalize job in status {current['status']}")

        update = conn.execute(
            """
            UPDATE migration_jobs
            SET status='succeeded', outcome_category='succeeded', finished_clock_ms=?,
                fail_reason=NULL
            WHERE job_key=? AND status IN ('claimed', 'running', 'committing')
            """,
            (clock_ms, job["job_key"]),
        )
        if update.rowcount != 1:
            row = conn.execute("SELECT status FROM migration_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
            if row and row["status"] == "succeeded":
                return {"status": "already_succeeded", "job_key": job["job_key"]}
            raise StateConflictError("job finalize compare-and-set failed")

        object_update = conn.execute(
            "UPDATE objects SET current_tier=? WHERE object_key=? AND current_tier=?",
            (job["target_tier"], job["object_key"], job["source_tier"]),
        )
        if object_update.rowcount != 1:
            raise StateConflictError("object tier changed before atomic migration commit")

        source_update = conn.execute(
            "UPDATE capacity_accounts SET used_bytes=used_bytes-?, updated_clock_ms=? "
            "WHERE tier=? AND used_bytes>=?",
            (job["size_bytes"], clock_ms, job["source_tier"], job["size_bytes"]),
        )
        if source_update.rowcount != 1:
            raise StateConflictError("source capacity update failed")
        dest_update = conn.execute(
            """
            UPDATE capacity_accounts
            SET used_bytes=used_bytes+?, reserved_bytes=reserved_bytes-?, updated_clock_ms=?
            WHERE tier=? AND reserved_bytes>=?
            """,
            (
                job["size_bytes"],
                job["size_bytes"],
                clock_ms,
                job["target_tier"],
                job["size_bytes"],
            ),
        )
        if dest_update.rowcount != 1:
            raise StateConflictError("destination capacity finalization failed")

        attempt = int(current["attempt_count"])
        conn.execute(
            "INSERT OR IGNORE INTO capacity_ledger(entry_key, round_id, job_key, tier, "
            "delta_used, delta_reserved, reason, clock_ms) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{job['job_key']}:commit-source:{attempt}",
                job["round_id"],
                job["job_key"],
                job["source_tier"],
                -int(job["size_bytes"]),
                0,
                "commit_source_used",
                clock_ms,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO capacity_ledger(entry_key, round_id, job_key, tier, "
            "delta_used, delta_reserved, reason, clock_ms) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{job['job_key']}:commit-dest:{attempt}",
                job["round_id"],
                job["job_key"],
                job["target_tier"],
                int(job["size_bytes"]),
                -int(job["size_bytes"]),
                "commit_destination_used_and_reservation",
                clock_ms,
            ),
        )
        conn.execute(
            "UPDATE rounds SET spent_bytes=spent_bytes+? WHERE round_id=?",
            (job["size_bytes"], job["round_id"]),
        )
        conn.execute(
            """
            INSERT INTO watermarks(name, value_integer, updated_clock_ms)
            VALUES('migration_committed', 1, ?)
            ON CONFLICT(name) DO UPDATE SET
                value_integer=watermarks.value_integer+1,
                updated_clock_ms=excluded.updated_clock_ms
            """,
            (clock_ms,),
        )
        if ledger is not None:
            conn.execute(
                """
                INSERT INTO cost_ledgers(ledger_key, round_id, policy_version, object_key,
                                        total_cost_scaled, ledger_json, updated_clock_ms)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ledger_key) DO UPDATE SET
                    ledger_json=excluded.ledger_json,
                    updated_clock_ms=excluded.updated_clock_ms
                """,
                (
                    f"{job['round_id']}:v{job['policy_version']}:{job['object_key']}",
                    job["round_id"],
                    job["policy_version"],
                    job["object_key"],
                    int(ledger["total_cost_scaled"]),
                    canonical_dumps(ledger, indent=None),
                    clock_ms,
                ),
            )
        else:
            conn.execute(
                "UPDATE cost_ledgers SET updated_clock_ms=? "
                "WHERE ledger_key=? AND round_id=? AND policy_version=? AND object_key=?",
                (
                    clock_ms,
                    f"{job['round_id']}:v{job['policy_version']}:{job['object_key']}",
                    job["round_id"],
                    job["policy_version"],
                    job["object_key"],
                ),
            )
        store.advance_clock(conn, clock_ms, job["round_id"])
        return {"status": "succeeded", "job_key": job["job_key"]}


def finalize_failure(
    store: StateStore,
    job: dict[str, Any],
    error: BaseException,
    clock_ms: int,
    backoff_config: dict[str, Any],
) -> dict[str, Any]:
    message = f"{type(error).__name__}: {error}"
    with store.write() as conn:
        current = conn.execute(
            "SELECT * FROM migration_jobs WHERE job_key=?", (job["job_key"],)
        ).fetchone()
        if current is None:
            raise StateConflictError("job disappeared")
        current = dict(current)
        if current["status"] == "failed":
            return {"status": "already_failed", "job_key": job["job_key"]}
        attempt = int(current["attempt_count"])
        initial = int(backoff_config.get("initial_delay_ms", 10))
        maximum = int(backoff_config.get("max_delay_ms", 10_000))
        multiplier = float(backoff_config.get("multiplier", 2.0))
        backoff = min(maximum, int(initial * (multiplier ** max(0, attempt - 1))))
        update = conn.execute(
            """
            UPDATE migration_jobs
            SET status='failed', outcome_category='started_failed', finished_clock_ms=?,
                fail_reason=?, backoff_until_clock_ms=?
            WHERE job_key=? AND status IN ('claimed', 'running', 'committing')
            """,
            (clock_ms, message, clock_ms + backoff, job["job_key"]),
        )
        if update.rowcount != 1:
            row = conn.execute("SELECT status FROM migration_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
            if row and row["status"] == "failed":
                return {"status": "already_failed", "job_key": job["job_key"]}
            raise StateConflictError("failure finalize compare-and-set failed")
        conn.execute(
            "UPDATE capacity_accounts SET reserved_bytes=reserved_bytes-?, updated_clock_ms=? "
            "WHERE tier=? AND reserved_bytes>=?",
            (job["size_bytes"], clock_ms, job["target_tier"], job["size_bytes"]),
        )
        release_row = conn.execute("SELECT changes() AS row_count").fetchone()
        if int(release_row["row_count"]) != 1:
            raise StateConflictError("capacity release failed")
        conn.execute(
            "UPDATE rounds SET claimed_bytes=MAX(0, claimed_bytes-?) WHERE round_id=?",
            (job["size_bytes"], job["round_id"]),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO capacity_ledger(entry_key, round_id, job_key, tier,
                delta_used, delta_reserved, reason, clock_ms)
            VALUES(?, ?, ?, ?, 0, ?, 'release_failed_reservation', ?)
            """,
            (
                f"{job['job_key']}:release:{attempt}",
                job["round_id"],
                job["job_key"],
                job["target_tier"],
                -int(job["size_bytes"]),
                clock_ms,
            ),
        )
        store.advance_clock(conn, clock_ms, job["round_id"])
        return {
            "status": "failed",
            "job_key": job["job_key"],
            "error": message,
            "backoff_ms": backoff,
            "backoff_until_clock_ms": clock_ms + backoff,
        }


def execute_claimed_job(
    store: StateStore,
    job: dict[str, Any],
    objects_by_key: dict[str, dict[str, Any]],
    config: dict[str, Any],
    clock_ms: int,
    *,
    task_delay_seconds: float = 0.0,
    fail_objects: set[str] | None = None,
    crash_after_commit_object: str | None = None,
    crash_after_prepare_object: str | None = None,
) -> dict[str, Any]:
    fail_objects = fail_objects or set()
    owner_id = str(job["owner_id"])
    mark_running(store, job["job_key"], owner_id, clock_ms)
    latest = dict(
        store.conn.execute("SELECT * FROM migration_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
    )
    obj = objects_by_key[job["object_key"]]
    try:
        simulate_transfer(obj, task_delay_seconds=task_delay_seconds, fail_objects=fail_objects)
    except Exception as exc:  # deterministic failure injection and transfer errors
        return finalize_failure(store, latest, exc, clock_ms, config.get("backoff", {}))
    mark_committing(store, job["job_key"], owner_id, clock_ms)
    if crash_after_prepare_object == job["object_key"]:
        os.kill(os.getpid(), signal.SIGKILL)
    result = finalize_success(store, latest, clock_ms)
    if crash_after_commit_object == job["object_key"] and result["status"] == "succeeded":
        os.kill(os.getpid(), signal.SIGKILL)
    return result


def _worker_loop(
    store: StateStore,
    round_id: str,
    clock_ms: int,
    objects_by_key: dict[str, dict[str, Any]],
    config: dict[str, Any],
    *,
    task_delay_seconds: float,
    fail_objects: set[str],
    crash_after_claim_object: str | None,
    crash_after_commit_object: str | None,
    crash_after_prepare_object: str | None,
    max_launch: int | None,
) -> list[dict[str, Any]]:
    owner_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    outcomes: list[dict[str, Any]] = []
    launched = 0
    try:
        while max_launch is None or launched < max_launch:
            claim = claim_next(store, round_id, owner_id, clock_ms)
            status = claim["status"]
            if status == "claimed":
                launched += 1
                job = claim["job"]
                if crash_after_claim_object == job["object_key"]:
                    os.kill(os.getpid(), signal.SIGKILL)
                outcomes.append(
                    execute_claimed_job(
                        store,
                        job,
                        objects_by_key,
                        config,
                        clock_ms,
                        task_delay_seconds=task_delay_seconds,
                        fail_objects=fail_objects,
                        crash_after_commit_object=crash_after_commit_object,
                        crash_after_prepare_object=crash_after_prepare_object,
                    )
                )
            elif status == "concurrency_busy":
                time.sleep(0.02)
            else:
                if status == "abandoned":
                    outcomes.append({"status": "abandoned", "job_key": claim["job"]["job_key"]})
                break
        return outcomes
    finally:
        store.close()


def run_migration(
    store: StateStore,
    clock_ms: int,
    round_id: str,
    *,
    budget_bytes: int | None = None,
    concurrency: int | None = None,
    task_delay_seconds: float = 0.0,
    fail_objects: set[str] | None = None,
    crash_after_plan: bool = False,
    crash_after_claim_object: str | None = None,
    crash_after_commit_object: str | None = None,
    crash_after_prepare_object: str | None = None,
    max_launch: int | None = None,
) -> dict[str, Any]:
    plan = create_plan(
        store,
        clock_ms,
        round_id,
        budget_bytes=budget_bytes,
        concurrency=concurrency,
    )
    if crash_after_plan:
        os.kill(os.getpid(), signal.SIGKILL)

    config = store.get_config()
    objects_by_key = {row["object_key"]: dict(row, key=row["object_key"]) for row in store.get_objects()}
    workers = int(concurrency if concurrency is not None else config["max_concurrency"])
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _worker_loop,
                store,
                round_id,
                clock_ms,
                objects_by_key,
                config,
                task_delay_seconds=task_delay_seconds,
                fail_objects=fail_objects or set(),
                crash_after_claim_object=crash_after_claim_object,
                crash_after_commit_object=crash_after_commit_object,
                crash_after_prepare_object=crash_after_prepare_object,
                max_launch=max_launch,
            )
            for _ in range(workers)
        ]
        worker_outcomes = [outcome for future in futures for outcome in future.result()]

    complete_round_if_done(store, round_id, clock_ms)
    report = round_report(store, round_id)
    report["worker_outcomes"] = worker_outcomes
    report["precision_pairs"] = plan["precision_pairs"]
    report["plan_candidates"] = [
        {
            "object_key": item["object_key"],
            "plan_rank": item["plan_rank"],
            "urgency": item["urgency"],
            "direction": item["direction"],
            "heat_score_bp": item["heat_score_bp"],
            "target_tier": item["target_tier"],
            "cost_total_scaled": item["ledger"]["total_cost_scaled"],
            "precision_sensitive": item["precision_sensitive"],
        }
        for item in plan["candidates"]
    ]
    return report


def complete_round_if_done(store: StateStore, round_id: str, clock_ms: int) -> None:
    with store.write() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS count FROM migration_jobs "
            "WHERE round_id=? AND status IN ('planned','claimed','running','committing')",
            (round_id,),
        ).fetchone()["count"]
        if int(active) == 0:
            conn.execute(
                "UPDATE rounds SET status='completed', finished_clock_ms=? WHERE round_id=?",
                (clock_ms, round_id),
            )
            store.advance_clock(conn, clock_ms, round_id)


def _stable_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_key": job["job_key"],
        "round_id": job["round_id"],
        "policy_version": job["policy_version"],
        "object_key": job["object_key"],
        "source_tier": job["source_tier"],
        "target_tier": job["target_tier"],
        "direction": job["direction"],
        "size_bytes": job["size_bytes"],
        "urgency": job["urgency"],
        "plan_rank": job["plan_rank"],
        "cost_total_scaled": job["cost_total_scaled"],
        "cost_error_bound": job["cost_error_bound"],
        "precision_sensitive": bool(job["precision_sensitive"]),
        "status": job["status"],
        "outcome_category": job["outcome_category"],
        "attempt_count": job["attempt_count"],
        "backoff_until_clock_ms": job["backoff_until_clock_ms"],
        "fail_reason": job["fail_reason"],
        "planned_clock_ms": job["planned_clock_ms"],
        "claimed_clock_ms": job["claimed_clock_ms"],
        "started_clock_ms": job["started_clock_ms"],
        "finished_clock_ms": job["finished_clock_ms"],
    }


def round_report(store: StateStore, round_id: str) -> dict[str, Any]:
    jobs = [_stable_job(job) for job in store.list_jobs(round_id)]
    jobs.sort(key=lambda job: (job["plan_rank"], job["object_key"]))
    status_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    for job in jobs:
        status_counts[job["status"]] = status_counts.get(job["status"], 0) + 1
        outcome_counts[job["outcome_category"]] = outcome_counts.get(job["outcome_category"], 0) + 1
    round_row = store.conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
    return {
        "round_id": round_id,
        "round": dict(round_row) if round_row else None,
        "status_counts": dict(sorted(status_counts.items())),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "jobs": jobs,
        "accounts": store.accounts(),
        "watermarks": store.watermarks(),
    }


def recover_interrupted(
    store: StateStore,
    clock_ms: int,
    round_id: str | None = None,
    *,
    force: bool = False,
    task_delay_seconds: float = 0.0,
    fail_objects: set[str] | None = None,
    retry_failed: bool = False,
) -> dict[str, Any]:
    config = store.get_config()
    if round_id is None:
        row = store.conn.execute(
            "SELECT round_id FROM rounds ORDER BY created_clock_ms DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise StateConflictError("no migration round exists to resume")
        round_id = row["round_id"]

    if retry_failed:
        with store.write() as conn:
            conn.execute(
                """
                UPDATE migration_jobs
                SET status='planned', outcome_category='within_budget_unstarted',
                    fail_reason=NULL
                WHERE round_id=? AND status='failed'
                """,
                (round_id,),
            )
            store.advance_clock(conn, clock_ms, round_id)

    active_rows = store.conn.execute(
        "SELECT * FROM migration_jobs WHERE round_id=? AND status IN ('claimed','running','committing') "
        "ORDER BY plan_rank, object_key",
        (round_id,),
    ).fetchall()
    recovered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    objects_by_key = {row["object_key"]: dict(row, key=row["object_key"]) for row in store.get_objects()}
    for row in active_rows:
        job = dict(row)
        stale = force
        if not stale and job["owner_pid"] and not pid_alive(int(job["owner_pid"])):
            stale = True
        if not stale and job["lease_expires_at"] and float(job["lease_expires_at"]) < time.time():
            stale = True
        if not stale:
            skipped.append({"job_key": job["job_key"], "reason": "owner_still_live"})
            continue
        owner_id = f"resume-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        with store.write() as conn:
            adopt = conn.execute(
                """
                UPDATE migration_jobs
                SET owner_id=?, owner_pid=?, lease_expires_at=?, status='running',
                    attempt_count=attempt_count+1
                WHERE job_key=? AND status IN ('claimed','running','committing')
                """,
                (owner_id, os.getpid(), time.time() + 30.0, job["job_key"]),
            )
            if adopt.rowcount != 1:
                continue
            store.advance_clock(conn, clock_ms, round_id)
        adopted = dict(
            store.conn.execute("SELECT * FROM migration_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
        )
        recovered.append(
            execute_claimed_job(
                store,
                adopted,
                objects_by_key,
                config,
                clock_ms,
                task_delay_seconds=task_delay_seconds,
                fail_objects=fail_objects or set(),
            )
        )

    remaining = run_migration(
        store,
        clock_ms,
        round_id,
        task_delay_seconds=task_delay_seconds,
        fail_objects=fail_objects,
    )
    return {
        "round_id": round_id,
        "recovered_active_jobs": recovered,
        "skipped_live_jobs": skipped,
        "report": remaining,
    }
