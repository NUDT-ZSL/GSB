from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Iterable

from .db import WorkbenchDB, connect
from .drift import attribute_drift
from .errors import WorkbenchError, bad_plan
from .plans import parse_plan
from .probes import execute_probe
from .util import canonical_json, sha256_json, weight_for_health


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: plan[key]
        for key in (
            "schema",
            "plan_id",
            "interval_ms",
            "timeout_ms",
            "window_size",
            "failure_threshold",
            "success_threshold",
            "rounds",
            "plan_digest",
        )
    }


def _round_id(plan_id: str, round_index: int, snapshot_version: str) -> str:
    return sha256_json(
        {
            "plan_id": plan_id,
            "round_index": round_index,
            "snapshot_version": snapshot_version,
        }
    )[:32]


def _round_dict(
    conn: sqlite3.Connection,
    round_id: str,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM rounds WHERE round_id = ?", (round_id,)).fetchone()
    if row is None:
        raise WorkbenchError(
            "E_ROUND_NOT_FOUND",
            "check round does not exist",
            {"round_id": round_id},
            http_status=404,
        )
    result = dict(row)
    result["expected_instance_ids"] = json.loads(
        result["expected_instance_ids_json"]
    )
    result["round_plan"] = json.loads(result["round_plan_json"])
    return result


def _start_round(
    db: WorkbenchDB,
    plan: dict[str, Any],
    round_index: int,
) -> dict[str, Any]:
    selected = next(
        (item for item in plan["rounds"] if item["round_index"] == round_index),
        None,
    )
    if selected is None:
        raise bad_plan("round_index is not present in plan", round_index=round_index)
    expected_ids = sorted(probe["instance_id"] for probe in selected["probes"])
    public_plan = _public_plan(plan)
    with db.lock:
        conn = connect(db.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_version = db.get_meta(conn, "current_snapshot_version")
            if not current_version:
                raise WorkbenchError(
                    "E_NO_SNAPSHOT",
                    "no snapshot is loaded",
                    http_status=409,
                )
            plan_row = conn.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (plan["plan_id"],)
            ).fetchone()
            if plan_row is not None and plan_row["plan_digest"] != plan["plan_digest"]:
                raise WorkbenchError(
                    "E_PLAN_CONFLICT",
                    "plan_id was already used with different plan content",
                    {
                        "plan_id": plan["plan_id"],
                        "stored_digest": plan_row["plan_digest"],
                        "incoming_digest": plan["plan_digest"],
                    },
                    http_status=409,
                )
            if plan_row is None:
                conn.execute(
                    "INSERT INTO plans(plan_id, plan_digest, plan_json) VALUES(?, ?, ?)",
                    (
                        plan["plan_id"],
                        plan["plan_digest"],
                        canonical_json(public_plan),
                    ),
                )
            round_id = _round_id(plan["plan_id"], round_index, current_version)
            existing = conn.execute(
                "SELECT * FROM rounds WHERE round_id = ?", (round_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["plan_digest"] != plan["plan_digest"]
                    or existing["logical_clock_ms"] != selected["logical_clock_ms"]
                ):
                    raise WorkbenchError(
                        "E_ROUND_CONFLICT",
                        "round identity is already bound to different plan content",
                        {"round_id": round_id},
                        http_status=409,
                    )
                conn.execute("COMMIT")
                return _round_dict(conn, round_id)

            merged_rows = {
                row["instance_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM merged_instances WHERE snapshot_version = ?",
                    (current_version,),
                )
            }
            for instance_id in expected_ids:
                if instance_id not in merged_rows:
                    raise WorkbenchError(
                        "E_UNKNOWN_INSTANCE",
                        "plan references an instance absent from the current snapshot",
                        {
                            "instance_id": instance_id,
                            "snapshot_version": current_version,
                        },
                        http_status=400,
                    )
                if merged_rows[instance_id]["manual"]:
                    raise WorkbenchError(
                        "E_MANUAL_REVIEW",
                        "plan references an instance awaiting manual adjudication",
                        {
                            "instance_id": instance_id,
                            "snapshot_version": current_version,
                        },
                        http_status=409,
                    )
            if round_index > 1:
                previous = conn.execute(
                    """
                    SELECT status FROM rounds
                    WHERE plan_id = ? AND round_index = ? AND snapshot_version = ?
                    """,
                    (plan["plan_id"], round_index - 1, current_version),
                ).fetchone()
                if previous is None or previous["status"] != "committed":
                    raise WorkbenchError(
                        "E_ROUND_ORDER",
                        "previous round must be committed before this round starts",
                        {
                            "required_round_index": round_index - 1,
                            "snapshot_version": current_version,
                        },
                        http_status=409,
                    )
            watermark = int(db.get_meta(conn, "clock_waterline_ms", "0"))
            if selected["logical_clock_ms"] < watermark:
                raise WorkbenchError(
                    "E_CLOCK_REGRESSION",
                    "round logical clock is below persisted clock waterline",
                    {
                        "round_logical_clock_ms": selected["logical_clock_ms"],
                        "clock_waterline_ms": watermark,
                    },
                    http_status=409,
                )
            conn.execute(
                """
                INSERT INTO rounds(
                    round_id, plan_id, round_index, snapshot_version,
                    logical_clock_ms, plan_digest, round_plan_json, status,
                    expected_instance_ids_json, expected_count, completed_count,
                    finalization_revision
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, 0, 0)
                """,
                (
                    round_id,
                    plan["plan_id"],
                    round_index,
                    current_version,
                    selected["logical_clock_ms"],
                    plan["plan_digest"],
                    canonical_json(public_plan),
                    canonical_json(expected_ids),
                    len(expected_ids),
                ),
            )
            conn.execute("COMMIT")
            return _round_dict(conn, round_id)
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


def _read_observation(
    conn: sqlite3.Connection,
    round_id: str,
    instance_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM probe_observations
        WHERE round_id = ? AND instance_id = ?
        """,
        (round_id, instance_id),
    ).fetchone()
    if row is None:
        return None
    result = json.loads(row["result_json"])
    result["error_code"] = row["error_code"]
    result["observed_health"] = row["observed_health"]
    return result


def _insert_observation(
    db: WorkbenchDB,
    round_id: str,
    instance_id: str,
    observation: dict[str, Any],
) -> bool:
    with db.lock:
        conn = connect(db.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO probe_observations(
                    round_id, instance_id, error_code, observed_health, result_json
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    round_id,
                    instance_id,
                    observation["error_code"],
                    observation["observed_health"],
                    canonical_json(observation),
                ),
            )
            conn.execute("COMMIT")
            return cursor.rowcount == 1
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


def _window_streak(
    conn: sqlite3.Connection,
    *,
    snapshot_version: str,
    instance_id: str,
    round_clock_ms: int,
    window_ms: int,
    current_health: str,
) -> tuple[int, list[dict[str, Any]]]:
    cutoff = round_clock_ms - window_ms
    history = [
        dict(row)
        for row in conn.execute(
            """
            SELECT r.logical_clock_ms, r.round_index, o.observed_health
            FROM probe_observations o
            JOIN rounds r ON r.round_id = o.round_id
            WHERE o.instance_id = ?
              AND r.snapshot_version = ?
              AND r.status = 'committed'
              AND r.logical_clock_ms >= ?
              AND r.logical_clock_ms < ?
            ORDER BY r.logical_clock_ms DESC, r.round_index DESC
            """,
            (
                instance_id,
                snapshot_version,
                cutoff,
                round_clock_ms,
            ),
        )
    ]
    sequence = [current_health] + [row["observed_health"] for row in history]
    streak = 0
    for health in sequence:
        if health != current_health:
            break
        streak += 1
    return streak, history


def _finalize_round(db: WorkbenchDB, round_id: str) -> dict[str, Any]:
    with db.lock:
        conn = connect(db.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            round_row = _round_dict(conn, round_id)
            if round_row["status"] == "committed":
                conn.execute("COMMIT")
                return {
                    "round_id": round_id,
                    "status": "committed",
                    "finalized": True,
                    "idempotent": True,
                    "applied_instance_ids": [],
                    "missing_instance_ids": [],
                }
            expected_ids = round_row["expected_instance_ids"]
            observations = {
                instance_id: _read_observation(conn, round_id, instance_id)
                for instance_id in expected_ids
            }
            missing = sorted(
                instance_id
                for instance_id, observation in observations.items()
                if observation is None
            )
            if missing:
                conn.execute("COMMIT")
                return {
                    "round_id": round_id,
                    "status": "started",
                    "finalized": False,
                    "idempotent": False,
                    "applied_instance_ids": [],
                    "missing_instance_ids": missing,
                }

            prior_started = conn.execute(
                """
                SELECT round_id FROM rounds
                WHERE snapshot_version = ?
                  AND status = 'started'
                  AND round_id != ?
                  AND (
                    logical_clock_ms < ?
                    OR (logical_clock_ms = ? AND round_index < ?)
                  )
                ORDER BY logical_clock_ms, round_index
                LIMIT 1
                """,
                (
                    round_row["snapshot_version"],
                    round_id,
                    round_row["logical_clock_ms"],
                    round_row["logical_clock_ms"],
                    round_row["round_index"],
                ),
            ).fetchone()
            if prior_started is not None:
                conn.execute("COMMIT")
                return {
                    "round_id": round_id,
                    "status": "started",
                    "finalized": False,
                    "idempotent": False,
                    "applied_instance_ids": [],
                    "missing_instance_ids": [],
                    "blocked_by_round_id": prior_started["round_id"],
                }

            max_committed = conn.execute(
                """
                SELECT MAX(logical_clock_ms) AS max_clock
                FROM rounds
                WHERE snapshot_version = ? AND status = 'committed'
                """,
                (round_row["snapshot_version"],),
            ).fetchone()["max_clock"]
            if max_committed is not None and round_row["logical_clock_ms"] < max_committed:
                raise WorkbenchError(
                    "E_ROUND_ORDER",
                    "cannot finalize an older round after a newer round committed",
                    {
                        "round_logical_clock_ms": round_row["logical_clock_ms"],
                        "max_committed_clock_ms": max_committed,
                    },
                    http_status=409,
                )

            plan = round_row["round_plan"]
            round_cfg = next(
                item
                for item in plan["rounds"]
                if item["round_index"] == round_row["round_index"]
            )
            window_ms = int(plan["interval_ms"]) * (int(plan["window_size"]) - 1)
            snapshot_version = round_row["snapshot_version"]
            merged_rows = {
                row["instance_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM merged_instances WHERE snapshot_version = ?",
                    (snapshot_version,),
                )
            }
            state_rows = {
                row["instance_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM instance_state WHERE snapshot_version = ?",
                    (snapshot_version,),
                )
            }
            applied: list[str] = []
            for instance_id in expected_ids:
                observation = observations[instance_id]
                state = state_rows.get(instance_id)
                merged = merged_rows.get(instance_id)
                if state is None or merged is None:
                    raise WorkbenchError(
                        "E_STATE_CORRUPT",
                        "round references an instance without state",
                        {
                            "instance_id": instance_id,
                            "snapshot_version": snapshot_version,
                        },
                        http_status=500,
                    )
                if merged["manual"]:
                    continue
                success = observation["observed_health"] == "healthy"
                if success:
                    consecutive_successes = int(state["consecutive_successes"]) + 1
                    consecutive_failures = 0
                else:
                    consecutive_failures = int(state["consecutive_failures"]) + 1
                    consecutive_successes = 0
                window_streak, _ = _window_streak(
                    conn,
                    snapshot_version=snapshot_version,
                    instance_id=instance_id,
                    round_clock_ms=round_row["logical_clock_ms"],
                    window_ms=window_ms,
                    current_health=observation["observed_health"],
                )
                previous_health = state["health_status"]
                if success:
                    if window_streak >= int(plan["success_threshold"]):
                        health_status = "healthy"
                    elif previous_health in {"suspect", "unhealthy"}:
                        health_status = "suspect"
                    else:
                        health_status = "healthy"
                else:
                    if window_streak >= int(plan["failure_threshold"]):
                        health_status = "unhealthy"
                    else:
                        health_status = "suspect"
                base_weight = int(state["base_weight"])
                effective_weight = weight_for_health(base_weight, health_status)
                if state["tombstone"]:
                    effective_weight = 0
                failure_at_ms = state["failure_at_ms"]
                if not success:
                    failure_at_ms = int(round_row["logical_clock_ms"])
                conn.execute(
                    """
                    UPDATE instance_state
                    SET health_status = ?, consecutive_failures = ?,
                        consecutive_successes = ?, effective_weight = ?,
                        failure_at_ms = ?, revision = revision + 1,
                        last_round_id = ?
                    WHERE snapshot_version = ? AND instance_id = ?
                    """,
                    (
                        health_status,
                        consecutive_failures,
                        consecutive_successes,
                        effective_weight,
                        failure_at_ms,
                        round_id,
                        snapshot_version,
                        instance_id,
                    ),
                )
                merged_for_drift = {
                    "instance_id": instance_id,
                    "selected_source": merged["selected_source"],
                    "health_status": merged["health_status"],
                    "state_generation": merged["state_generation"],
                    "tombstone": bool(merged["tombstone"]),
                    "manual": bool(merged["manual"]),
                    "decision": json.loads(merged["decision_json"]),
                }
                evidence = attribute_drift(
                    snapshot_version=snapshot_version,
                    merged=merged_for_drift,
                    sources=json.loads(merged["sources_json"]),
                    observation=observation,
                    round_row=round_row,
                )
                if evidence is not None:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO drift_attributions(
                            snapshot_version, instance_id, round_id,
                            round_index, logical_clock_ms, cause, evidence_json
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_version,
                            instance_id,
                            round_id,
                            round_row["round_index"],
                            round_row["logical_clock_ms"],
                            evidence["cause"],
                            canonical_json(evidence),
                        ),
                    )
                applied.append(instance_id)

            if os.environ.get("LB_CRASH_DURING_FINALIZE") == "1":
                os._exit(99)

            conn.execute(
                """
                UPDATE rounds
                SET status = 'committed', completed_count = expected_count,
                    finalization_revision = finalization_revision + 1
                WHERE round_id = ?
                """,
                (round_id,),
            )
            watermark = int(db.get_meta(conn, "clock_waterline_ms", "0"))
            new_watermark = max(watermark, int(round_row["logical_clock_ms"]))
            db.set_meta(conn, "clock_waterline_ms", str(new_watermark))
            conn.execute("COMMIT")
            return {
                "round_id": round_id,
                "status": "committed",
                "finalized": True,
                "idempotent": False,
                "applied_instance_ids": sorted(applied),
                "missing_instance_ids": [],
                "clock_waterline_ms": new_watermark,
            }
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


def _probe_round_instances(
    db: WorkbenchDB,
    *,
    round_row: dict[str, Any],
    plan: dict[str, Any],
    base_url: str | None,
    instances: Iterable[str] | None,
    crash_after_observations: int | None,
) -> dict[str, Any]:
    round_cfg = next(
        item
        for item in plan["rounds"]
        if item["round_index"] == round_row["round_index"]
    )
    probe_by_id = {probe["instance_id"]: probe for probe in round_cfg["probes"]}
    target_ids = sorted(probe_by_id) if instances is None else sorted(set(instances))
    unknown = sorted(set(target_ids) - set(probe_by_id))
    if unknown:
        raise WorkbenchError(
            "E_UNKNOWN_PROBE_INSTANCE",
            "requested instance has no probe in this round",
            {"round_id": round_row["round_id"], "instance_ids": unknown},
            http_status=400,
        )

    executed: list[str] = []
    reused: list[str] = []
    persisted_in_process = 0
    for instance_id in target_ids:
        with db.connection() as conn:
            existing = _read_observation(conn, round_row["round_id"], instance_id)
        if existing is not None:
            reused.append(instance_id)
            continue
        observation = execute_probe(probe_by_id[instance_id], base_url)
        inserted = _insert_observation(
            db, round_row["round_id"], instance_id, observation
        )
        if inserted:
            executed.append(instance_id)
            persisted_in_process += 1
            if (
                crash_after_observations is not None
                and persisted_in_process >= crash_after_observations
            ):
                os._exit(97)
        else:
            reused.append(instance_id)
    return {
        "executed": sorted(executed),
        "reused": sorted(reused),
        "persisted_count": persisted_in_process,
    }


def run_check(
    db: WorkbenchDB,
    *,
    plan_path: str,
    probe_base_url: str | None,
    round_index: int | None = None,
    instances: Iterable[str] | None = None,
) -> dict[str, Any]:
    plan = parse_plan(plan_path)
    db.initialize()
    selected_rounds = (
        plan["rounds"]
        if round_index is None
        else [item for item in plan["rounds"] if item["round_index"] == round_index]
    )
    if not selected_rounds:
        raise bad_plan("round_index is not present in plan", round_index=round_index)

    crash_after_raw = os.environ.get("LB_CRASH_AFTER_OBSERVATIONS")
    crash_after = int(crash_after_raw) if crash_after_raw else None
    results = []
    for round_cfg in selected_rounds:
        round_row = _start_round(db, plan, round_cfg["round_index"])
        if round_row["status"] == "committed":
            results.append(
                {
                    "round_id": round_row["round_id"],
                    "round_index": round_row["round_index"],
                    "logical_clock_ms": round_row["logical_clock_ms"],
                    "status": "committed",
                    "finalized": True,
                    "idempotent": True,
                    "executed_instance_ids": [],
                    "reused_instance_ids": round_row["expected_instance_ids"],
                    "missing_instance_ids": [],
                }
            )
            continue
        probe_result = _probe_round_instances(
            db,
            round_row=round_row,
            plan=plan,
            base_url=probe_base_url,
            instances=instances,
            crash_after_observations=crash_after,
        )
        finalized = _finalize_round(db, round_row["round_id"])
        results.append(
            {
                "round_id": round_row["round_id"],
                "round_index": round_row["round_index"],
                "logical_clock_ms": round_row["logical_clock_ms"],
                **probe_result,
                **{
                    key: finalized[key]
                    for key in (
                        "status",
                        "finalized",
                        "idempotent",
                        "missing_instance_ids",
                        "applied_instance_ids",
                    )
                },
            }
        )
        if not finalized["finalized"]:
            break
        if crash_after is not None:
            crash_after -= probe_result["persisted_count"]
            if crash_after <= 0:
                crash_after = None
    return {
        "ok": all(item["status"] == "committed" for item in results),
        "schema": "lb-check-result/v1",
        "plan_id": plan["plan_id"],
        "plan_digest": plan["plan_digest"],
        "rounds": results,
    }


def resume_checks(db: WorkbenchDB, *, probe_base_url: str | None) -> dict[str, Any]:
    db.initialize()
    with db.connection() as conn:
        started = [
            _round_dict(conn, row["round_id"])
            for row in conn.execute(
                """
                SELECT round_id FROM rounds
                WHERE status = 'started'
                ORDER BY logical_clock_ms, plan_id, round_index, snapshot_version
                """
            )
        ]
    reports = []
    plan_ids: set[str] = set()
    crash_after_raw = os.environ.get("LB_CRASH_AFTER_OBSERVATIONS")
    crash_after = int(crash_after_raw) if crash_after_raw else None
    for round_row in started:
        with db.connection() as conn:
            current = _round_dict(conn, round_row["round_id"])
        if current["status"] == "committed":
            continue
        plan_ids.add(current["plan_id"])
        plan = current["round_plan"]
        missing = []
        with db.connection() as conn:
            for instance_id in current["expected_instance_ids"]:
                if _read_observation(conn, current["round_id"], instance_id) is None:
                    missing.append(instance_id)
        probe_result = {"executed": [], "reused": [], "persisted_count": 0}
        if missing:
            probe_result = _probe_round_instances(
                db,
                round_row=current,
                plan=plan,
                base_url=probe_base_url,
                instances=missing,
                crash_after_observations=crash_after,
            )
        finalized = _finalize_round(db, current["round_id"])
        reports.append(
            {
                "round_id": current["round_id"],
                "plan_id": current["plan_id"],
                "round_index": current["round_index"],
                "logical_clock_ms": current["logical_clock_ms"],
                "snapshot_version": current["snapshot_version"],
                "previously_missing_instance_ids": missing,
                "executed_instance_ids": probe_result["executed"],
                "reused_instance_ids": probe_result["reused"],
                **{
                    key: finalized[key]
                    for key in (
                        "status",
                        "finalized",
                        "idempotent",
                        "missing_instance_ids",
                        "applied_instance_ids",
                    )
                },
            }
        )
        if crash_after is not None:
            crash_after -= probe_result["persisted_count"]
            if crash_after <= 0:
                crash_after = None

    # A crashed driver may not yet have created later rounds. Continue those
    # rounds only for plans that had an in-flight round, while reusing every
    # observation already persisted for completed rounds and current rounds.
    with db.connection() as conn:
        current_version = db.get_meta(conn, "current_snapshot_version")
        if current_version:
            plan_ids.update(
                row["plan_id"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT plan_id FROM rounds
                    WHERE snapshot_version = ?
                    """,
                    (current_version,),
                )
            )
        stored_plans = [
            json.loads(row["plan_json"])
            for row in conn.execute(
                "SELECT plan_json FROM plans ORDER BY plan_id"
            )
            if row
        ]
    for plan in stored_plans:
        if plan["plan_id"] not in plan_ids:
            continue
        for round_cfg in plan["rounds"]:
            if not current_version:
                break
            round_id = _round_id(plan["plan_id"], round_cfg["round_index"], current_version)
            with db.connection() as conn:
                existing = conn.execute(
                    "SELECT * FROM rounds WHERE round_id = ?", (round_id,)
                ).fetchone()
            if existing is not None and existing["status"] == "committed":
                continue
            round_row = _start_round(db, plan, round_cfg["round_index"])
            if round_row["status"] == "committed":
                continue
            with db.connection() as conn:
                missing = [
                    instance_id
                    for instance_id in round_row["expected_instance_ids"]
                    if _read_observation(conn, round_row["round_id"], instance_id) is None
                ]
            probe_result = {"executed": [], "reused": [], "persisted_count": 0}
            if missing:
                probe_result = _probe_round_instances(
                    db,
                    round_row=round_row,
                    plan=plan,
                    base_url=probe_base_url,
                    instances=missing,
                    crash_after_observations=crash_after,
                )
            finalized = _finalize_round(db, round_row["round_id"])
            reports.append(
                {
                    "round_id": round_row["round_id"],
                    "plan_id": round_row["plan_id"],
                    "round_index": round_row["round_index"],
                    "logical_clock_ms": round_row["logical_clock_ms"],
                    "snapshot_version": round_row["snapshot_version"],
                    "previously_missing_instance_ids": missing,
                    "executed_instance_ids": probe_result["executed"],
                    "reused_instance_ids": probe_result["reused"],
                    "continued_after_started_round": True,
                    **{
                        key: finalized[key]
                        for key in (
                            "status",
                            "finalized",
                            "idempotent",
                            "missing_instance_ids",
                            "applied_instance_ids",
                        )
                    },
                }
            )
            if crash_after is not None:
                crash_after -= probe_result["persisted_count"]
                if crash_after <= 0:
                    crash_after = None
            if not finalized["finalized"]:
                break
    return {
        "ok": all(item["status"] == "committed" for item in reports),
        "schema": "lb-resume-result/v1",
        "resumed_round_count": len(reports),
        "rounds": reports,
    }
