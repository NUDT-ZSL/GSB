from __future__ import annotations

import json
import sqlite3
from typing import Any

from quota_ledger.db import Store
from quota_ledger.policy import policy_params_from_json
from quota_ledger.util import canonical_json, digest_json, micros_to_quota


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _fetch_all(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [_row_dict(row) for row in connection.execute(sql).fetchall()]


def _latest_policy_context(connection: sqlite3.Connection) -> dict[str, Any]:
    pipeline = connection.execute(
        "SELECT * FROM pipeline_rounds ORDER BY round_no DESC LIMIT 1"
    ).fetchone()
    if pipeline is not None:
        return _row_dict(pipeline)
    policy = connection.execute(
        "SELECT * FROM policies ORDER BY effective_clock_ms DESC, generation DESC, "
        "version_rank DESC, snapshot_id DESC LIMIT 1"
    ).fetchone()
    if policy is not None:
        params = policy_params_from_json(policy["params_json"])
        return {
            "global_budget_micros": params.global_quota_budget_micros,
            "round_budget_micros": params.round_threshold_budget_micros,
            "per_tenant_cap_micros": params.per_tenant_adjustment_cap_micros,
            "safety_watermark_micros": params.safety_watermark_micros,
            "drift_threshold_text": params.drift_threshold_text,
            "danger_threshold_text": params.danger_threshold_text,
            "policy_id": policy["policy_id"],
        }
    default_json = connection.execute(
        "SELECT value FROM meta WHERE key='default_params_json'"
    ).fetchone()
    if default_json is not None:
        params = policy_params_from_json(default_json["value"])
        return {
            "global_budget_micros": params.global_quota_budget_micros,
            "round_budget_micros": params.round_threshold_budget_micros,
            "per_tenant_cap_micros": params.per_tenant_adjustment_cap_micros,
            "safety_watermark_micros": params.safety_watermark_micros,
            "drift_threshold_text": params.drift_threshold_text,
            "danger_threshold_text": params.danger_threshold_text,
            "policy_id": "",
        }
    return {
        "global_budget_micros": 0,
        "round_budget_micros": 0,
        "per_tenant_cap_micros": 0,
        "safety_watermark_micros": 0,
        "drift_threshold_text": "0",
        "danger_threshold_text": "0",
        "policy_id": "",
    }


def _ledger_summary(connection: sqlite3.Connection, tenant: str) -> dict[str, Any]:
    app_counts = connection.execute(
        "SELECT state, COUNT(*) AS count FROM applications WHERE tenant=? "
        "GROUP BY state",
        (tenant,),
    ).fetchall()
    state_counts = {row["state"]: int(row["count"]) for row in app_counts}
    entry_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE tenant=?", (tenant,)
        ).fetchone()[0]
    )
    return {
        "booked_applications": int(
            connection.execute(
                "SELECT COUNT(*) FROM applications WHERE tenant=? "
                "AND state IN ('accepted','released')",
                (tenant,),
            ).fetchone()[0]
        ),
        "active_applications": state_counts.get("accepted", 0),
        "released_applications": state_counts.get("released", 0),
        "rejected_applications": state_counts.get("rejected", 0),
        "pending_applications": state_counts.get("pending", 0),
        "entries": entry_count,
    }


def _tenant_view(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    threshold = int(row["threshold_micros"])
    occupancy = int(row["occupancy_micros"])
    remaining = threshold - occupancy
    cost = int(row["shave_cost_micros"])
    safety = int(row["safety_watermark_micros"])
    decisions = connection.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='abandoned' THEN 1 ELSE 0 END) AS abandoned "
        "FROM drift_decisions WHERE tenant=?",
        (row["tenant"],),
    ).fetchone()
    ledger = _ledger_summary(connection, row["tenant"])
    ledger.update(
        {
            "reserved": micros_to_quota(int(row["reserved_micros"])),
            "reserved_micros": int(row["reserved_micros"]),
            "occupancy": micros_to_quota(occupancy),
            "occupancy_micros": occupancy,
            "remaining": micros_to_quota(remaining),
            "remaining_micros": remaining,
            "shave_backlog": micros_to_quota(int(row["shave_backlog_micros"])),
            "shave_backlog_micros": int(row["shave_backlog_micros"]),
            "shave_cost": micros_to_quota(cost),
            "shave_cost_micros": cost,
            "cumulative_actual": micros_to_quota(
                int(row["cumulative_actual_micros"])
            ),
            "cumulative_released": micros_to_quota(
                int(row["cumulative_released_micros"])
            ),
            "cumulative_error_bound": micros_to_quota(
                int(row["cumulative_error_bound_micros"])
            ),
            "safety_watermark": micros_to_quota(safety),
            "safe_remaining_micros": max(0, remaining - cost - safety),
        }
    )
    return {
        "tenant": row["tenant"],
        "resource_key": row["resource_key"],
        "threshold": micros_to_quota(threshold),
        "threshold_micros": threshold,
        "threshold_generation": int(row["threshold_generation"]),
        "quota_generation": int(row["quota_generation"]),
        "policy_generation": int(row["policy_generation"]),
        "source_version": row["source_version"],
        "source_snapshot_id": row["source_snapshot_id"],
        "policy_id": row["policy_id"],
        "effective_clock_ms": int(row["effective_clock_ms"]),
        "ledger": ledger,
        "scheduling": {
            "last_adjust_round": int(row["last_adjust_round"]),
            "cumulative_adjustment": micros_to_quota(
                int(row["cumulative_adjustment_micros"])
            ),
            "cumulative_adjustment_micros": int(row["cumulative_adjustment_micros"]),
            "decisions": int(decisions["total"] or 0),
            "abandoned_decisions": int(decisions["abandoned"] or 0),
        },
    }


def compute_state_digest(connection: sqlite3.Connection) -> str:
    payload: dict[str, Any] = {
        "meta": _fetch_all(
            connection,
            "SELECT key, value FROM meta WHERE key IN ("
            "'dataset_digest','round_span_ms','process_round_watermark',"
            "'adjust_round_watermark','logical_clock_watermark_ms','target_round',"
            "'state_generation'"
            ") ORDER BY key",
        ),
        "tenants": _fetch_all(connection, "SELECT * FROM tenants ORDER BY tenant"),
        "applications": _fetch_all(
            connection,
            "SELECT application_seq, tenant, resource_key, requested_micros, "
            "requested_error_micros, requested_rounding, logical_clock_ms, priority, "
            "state, reserved_micros, estimate_error_bound_micros, actual_micros, "
            "applied_round, reject_reason FROM applications ORDER BY application_seq",
        ),
        "releases": _fetch_all(
            connection,
            "SELECT release_seq, application_seq, tenant, actual_micros, "
            "logical_clock_ms, state, processed_round, difference_micros, shaved_micros "
            "FROM releases ORDER BY release_seq",
        ),
        "observations": _fetch_all(
            connection,
            "SELECT observation_seq, tenant, actual_occupancy_micros, logical_clock_ms, "
            "state, processed_round, quota_fixedpoint_micros, deviation_micros, "
            "deviation_ratio_text, is_danger, policy_generation, decision_status "
            "FROM observations ORDER BY observation_seq",
        ),
        "pipeline_rounds": _fetch_all(
            connection, "SELECT * FROM pipeline_rounds ORDER BY round_no"
        ),
        "ledger_entries": _fetch_all(
            connection,
            "SELECT event_key, event_kind, application_seq, release_seq, tenant, "
            "resource_key, quota_generation, policy_generation, round_no, "
            "logical_clock_ms, amount_micros, estimated_micros, actual_micros, "
            "difference_micros, estimate_scale, actual_scale, rounding_direction, "
            "error_bound_micros, cumulative_error_bound_micros, reserved_after_micros, "
            "occupancy_after_micros, available_after_micros, shaved_micros, details_json "
            "FROM ledger_entries ORDER BY event_key",
        ),
        "drift_decisions": _fetch_all(
            connection,
            "SELECT round_no, tenant, observation_seq, source_version, policy_generation, "
            "quota_fixedpoint_micros, observed_micros, deviation_micros, "
            "deviation_ratio_text, drift_threshold_text, direction, dangerous, fair_tag, "
            "desired_delta_micros, applied_delta_micros, threshold_before_micros, "
            "threshold_after_micros, remaining_after_micros, budget_before_micros, "
            "budget_after_micros, candidate_order, status, reason "
            "FROM drift_decisions ORDER BY round_no, tenant",
        ),
        "threshold_changes": _fetch_all(
            connection,
            "SELECT round_no, tenant, old_threshold_micros, new_threshold_micros, "
            "delta_micros, generation_before, generation_after, reason, source_version, "
            "policy_id FROM threshold_changes ORDER BY round_no, change_id",
        ),
        "shave_logs": _fetch_all(
            connection,
            "SELECT event_key, round_no, tenant, application_seq, release_seq, kind, "
            "original_difference_micros, cap_micros, returned_micros, deferred_micros, "
            "drained_micros, cost_micros, remaining_after_micros, "
            "safety_watermark_after_micros, safe_watermark_preserved, details_json "
            "FROM shave_logs ORDER BY round_no, event_key",
        ),
        "scheduling_rounds": _fetch_all(
            connection, "SELECT * FROM scheduling_rounds ORDER BY round_no"
        ),
        "applied_policies": _fetch_all(
            connection,
            "SELECT policy_id, round_no, effective_clock_ms, fingerprint "
            "FROM applied_policies ORDER BY policy_id",
        ),
        "policy_merge": _fetch_all(
            connection,
            "SELECT tenant, policy_id, source_version, source_snapshot_id, "
            "effective_clock_ms, policy_generation, threshold_micros, invalidated "
            "FROM policy_merge ORDER BY tenant",
        ),
        "policy_invalidations": _fetch_all(
            connection,
            "SELECT root_snapshot_id, root_tenant, tenant, resource_key, depth, direct, reason "
            "FROM policy_invalidations ORDER BY root_snapshot_id, root_tenant, tenant",
        ),
    }
    return digest_json(payload)


def status_report(store: Store) -> dict[str, Any]:
    with store.read_conn() as connection:
        context = _latest_policy_context(connection)
        tenant_rows = connection.execute(
            "SELECT * FROM tenants ORDER BY tenant"
        ).fetchall()
        tenants = [_tenant_view(connection, row) for row in tenant_rows]
        threshold_sum = sum(item["threshold_micros"] for item in tenants)
        occupancy = sum(item["ledger"]["occupancy_micros"] for item in tenants)
        reserved = sum(item["ledger"]["reserved_micros"] for item in tenants)
        backlog = sum(item["ledger"]["shave_backlog_micros"] for item in tenants)
        budget = int(context["global_budget_micros"])
        merge_rows = _fetch_all(
            connection,
            "SELECT tenant, policy_id, source_version, source_snapshot_id, "
            "effective_clock_ms, policy_generation, threshold_micros, invalidated, "
            "recomputed FROM policy_merge ORDER BY tenant",
        )
        for row in merge_rows:
            row["threshold"] = micros_to_quota(int(row.pop("threshold_micros")))
        invalidations = _fetch_all(
            connection,
            "SELECT root_snapshot_id, root_tenant, tenant, resource_key, depth, direct, reason "
            "FROM policy_invalidations ORDER BY root_snapshot_id, root_tenant, tenant",
        )
        scheduling_total = connection.execute(
            "SELECT COUNT(*) AS rounds, COALESCE(SUM(used_micros),0) AS used, "
            "COALESCE(SUM(abandoned_count),0) AS abandoned "
            "FROM scheduling_rounds"
        ).fetchone()
        decision_counts = connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END) AS applied, "
            "SUM(CASE WHEN status='partial' THEN 1 ELSE 0 END) AS partial, "
            "SUM(CASE WHEN status='abandoned' THEN 1 ELSE 0 END) AS abandoned, "
            "SUM(CASE WHEN dangerous=1 THEN 1 ELSE 0 END) AS dangerous "
            "FROM drift_decisions"
        ).fetchone()
        app_counts = connection.execute(
            "SELECT state, COUNT(*) AS count FROM applications GROUP BY state"
        ).fetchall()
        release_counts = connection.execute(
            "SELECT state, COUNT(*) AS count FROM releases GROUP BY state"
        ).fetchall()
        process_watermark = store.meta_get_int(connection, "process_round_watermark")
        adjust_watermark = store.meta_get_int(connection, "adjust_round_watermark")
        logical_watermark = store.meta_get_int(connection, "logical_clock_watermark_ms")
        target_round = store.meta_get_int(connection, "target_round")
        round_span_ms = store.meta_get_int(connection, "round_span_ms")
        dataset_digest = store.meta_get(connection, "dataset_digest")
        digest = compute_state_digest(connection)
    return {
        "schema_version": "1",
        "database": str(store.path),
        "dataset_digest": dataset_digest,
        "round_span_ms": round_span_ms,
        "logical_clock_watermark_ms": logical_watermark,
        "process_round_watermark": process_watermark,
        "adjust_round_watermark": adjust_watermark,
        "scheduling_round_watermark": adjust_watermark,
        "target_round": target_round,
        "pending_adjustment_rounds": max(0, process_watermark - adjust_watermark),
        "global_quota_budget": micros_to_quota(budget),
        "global_quota_budget_micros": budget,
        "round_threshold_budget": micros_to_quota(int(context["round_budget_micros"])),
        "round_threshold_budget_micros": int(context["round_budget_micros"]),
        "aggregate_threshold": micros_to_quota(threshold_sum),
        "aggregate_threshold_micros": threshold_sum,
        "occupancy": micros_to_quota(occupancy),
        "occupancy_micros": occupancy,
        "reserved": micros_to_quota(reserved),
        "reserved_micros": reserved,
        "shave_backlog": micros_to_quota(backlog),
        "shave_backlog_micros": backlog,
        "over_global_budget": occupancy > budget,
        "threshold_pool_over_budget": threshold_sum > budget,
        "tenants": tenants,
        "policy_merge": merge_rows,
        "policy_invalidations": invalidations,
        "applications": {
            "by_state": {row["state"]: int(row["count"]) for row in app_counts}
        },
        "releases": {
            "by_state": {row["state"]: int(row["count"]) for row in release_counts}
        },
        "scheduling": {
            "rounds": int(scheduling_total["rounds"]),
            "used_micros": int(scheduling_total["used"]),
            "abandoned": int(scheduling_total["abandoned"]),
            "decisions": int(decision_counts["total"] or 0),
            "applied": int(decision_counts["applied"] or 0),
            "partial": int(decision_counts["partial"] or 0),
            "abandoned_decisions": int(decision_counts["abandoned"] or 0),
            "dangerous_candidates": int(decision_counts["dangerous"] or 0),
        },
        "state_digest": digest,
    }


def _check(name: str, ok: bool, **details: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "ok": bool(ok)}
    if details:
        item["details"] = details
    return item


def verify_report(store: Store) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    with store.read_conn() as connection:
        process_watermark = store.meta_get_int(connection, "process_round_watermark")
        adjust_watermark = store.meta_get_int(connection, "adjust_round_watermark")
        target_round = store.meta_get_int(connection, "target_round")
        context = _latest_policy_context(connection)
        budget = int(context["global_budget_micros"])

        app_reserved_ok = True
        app_reserved_details: list[dict[str, Any]] = []
        for tenant in connection.execute("SELECT * FROM tenants ORDER BY tenant"):
            active_sum = int(
                connection.execute(
                    "SELECT COALESCE(SUM(reserved_micros),0) FROM applications "
                    "WHERE tenant=? AND state='accepted'",
                    (tenant["tenant"],),
                ).fetchone()[0]
            )
            if active_sum != int(tenant["reserved_micros"]):
                app_reserved_ok = False
                app_reserved_details.append(
                    {
                        "tenant": tenant["tenant"],
                        "active_reservations": active_sum,
                        "tenant_reserved": int(tenant["reserved_micros"]),
                    }
                )
        checks.append(
            _check(
                "active_application_reservations_match_tenant_ledger",
                app_reserved_ok,
                mismatches=app_reserved_details,
            )
        )

        occupancy_ok = True
        occupancy_details: list[dict[str, Any]] = []
        for tenant in connection.execute("SELECT * FROM tenants ORDER BY tenant"):
            expected = int(tenant["reserved_micros"])
            if int(tenant["occupancy_micros"]) != expected:
                occupancy_ok = False
                occupancy_details.append(
                    {
                        "tenant": tenant["tenant"],
                        "expected": expected,
                        "actual": int(tenant["occupancy_micros"]),
                    }
                )
        checks.append(
            _check(
                "occupancy_equals_active_reservations",
                occupancy_ok,
                mismatches=occupancy_details,
            )
        )

        nonnegative_ok = True
        nonnegative_details: list[dict[str, Any]] = []
        for tenant in connection.execute("SELECT * FROM tenants ORDER BY tenant"):
            remaining = int(tenant["threshold_micros"]) - int(
                tenant["occupancy_micros"]
            )
            values = {
                "threshold_micros": int(tenant["threshold_micros"]),
                "reserved_micros": int(tenant["reserved_micros"]),
                "occupancy_micros": int(tenant["occupancy_micros"]),
                "shave_backlog_micros": int(tenant["shave_backlog_micros"]),
                "remaining_micros": remaining,
            }
            if any(value < 0 for value in values.values()):
                nonnegative_ok = False
                nonnegative_details.append({"tenant": tenant["tenant"], **values})
        checks.append(
            _check(
                "tenant_quota_values_nonnegative",
                nonnegative_ok,
                violations=nonnegative_details,
            )
        )

        current_occupancy = _fetch_all(
            connection,
            "SELECT COALESCE(SUM(occupancy_micros),0) AS value FROM tenants",
        )[0]["value"]
        checks.append(
            _check(
                "global_occupancy_within_current_budget",
                int(current_occupancy) <= budget,
                occupancy_micros=int(current_occupancy),
                budget_micros=budget,
            )
        )
        current_threshold = _fetch_all(
            connection,
            "SELECT COALESCE(SUM(threshold_micros),0) AS value FROM tenants",
        )[0]["value"]
        checks.append(
            _check(
                "aggregate_threshold_within_current_budget",
                int(current_threshold) <= budget,
                threshold_micros=int(current_threshold),
                budget_micros=budget,
            )
        )

        round_ok = True
        round_details: dict[str, Any] = {}
        if process_watermark < 0 or process_watermark > target_round:
            round_ok = False
            round_details["process_watermark"] = process_watermark
            round_details["target_round"] = target_round
        if adjust_watermark > process_watermark:
            round_ok = False
            round_details["adjust_watermark"] = adjust_watermark
            round_details["process_watermark"] = process_watermark
        pipeline_rounds = connection.execute(
            "SELECT round_no FROM pipeline_rounds ORDER BY round_no"
        ).fetchall()
        expected_rounds = list(range(1, process_watermark + 1))
        actual_rounds = [int(row["round_no"]) for row in pipeline_rounds]
        if actual_rounds != expected_rounds:
            round_ok = False
            round_details["actual_rounds"] = actual_rounds
            round_details["expected_rounds"] = expected_rounds
        checks.append(_check("round_watermarks_consistent", round_ok, **round_details))

        scheduling_ok = True
        scheduling_details: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM scheduling_rounds ORDER BY round_no"
        ).fetchall():
            if (
                int(row["used_micros"]) > int(row["budget_micros"])
                or int(row["remaining_micros"])
                != int(row["budget_micros"]) - int(row["used_micros"])
            ):
                scheduling_ok = False
                scheduling_details.append(_row_dict(row))
        checks.append(
            _check(
                "round_threshold_budget_not_exceeded",
                scheduling_ok,
                violations=scheduling_details,
            )
        )

        decision_sum_ok = True
        decision_details: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM scheduling_rounds ORDER BY round_no"
        ).fetchall():
            applied_sum = int(
                connection.execute(
                    "SELECT COALESCE(SUM(applied_delta_micros),0) "
                    "FROM drift_decisions WHERE round_no=?",
                    (row["round_no"],),
                ).fetchone()[0]
            )
            if applied_sum != int(row["used_micros"]):
                decision_sum_ok = False
                decision_details.append(
                    {
                        "round_no": int(row["round_no"]),
                        "decision_sum": applied_sum,
                        "scheduled_used": int(row["used_micros"]),
                    }
                )
        checks.append(
            _check(
                "decision_usage_equals_scheduling_usage",
                decision_sum_ok,
                mismatches=decision_details,
            )
        )

        shave_ok = True
        shave_details: list[dict[str, Any]] = []
        for row in connection.execute("SELECT * FROM shave_logs ORDER BY shave_id"):
            values = [
                int(row["original_difference_micros"]),
                int(row["cap_micros"]),
                int(row["returned_micros"]),
                int(row["deferred_micros"]),
                int(row["drained_micros"]),
                int(row["cost_micros"]),
                int(row["remaining_after_micros"]),
                int(row["safety_watermark_after_micros"]),
            ]
            if any(value < 0 for value in values):
                shave_ok = False
                shave_details.append(_row_dict(row))
            if int(row["cost_micros"]) > int(row["remaining_after_micros"]):
                shave_ok = False
                shave_details.append(_row_dict(row))
            if (
                row["kind"] == "release_shave"
                and int(row["original_difference_micros"]) <= int(row["cap_micros"])
            ):
                shave_ok = False
                shave_details.append(_row_dict(row))
        checks.append(
            _check(
                "shave_accounting_safe_and_nonnegative",
                shave_ok,
                violations=shave_details,
            )
        )

        threshold_trace_ok = True
        threshold_details: list[dict[str, Any]] = []
        for tenant in connection.execute("SELECT * FROM tenants ORDER BY tenant"):
            changes = connection.execute(
                "SELECT * FROM threshold_changes WHERE tenant=? "
                "ORDER BY round_no, change_id",
                (tenant["tenant"],),
            ).fetchall()
            if changes:
                if int(changes[-1]["new_threshold_micros"]) != int(
                    tenant["threshold_micros"]
                ):
                    threshold_trace_ok = False
                    threshold_details.append(
                        {
                            "tenant": tenant["tenant"],
                            "reason": "final_threshold_mismatch",
                        }
                    )
                for previous, current in zip(changes, changes[1:]):
                    if (
                        int(previous["new_threshold_micros"])
                        != int(current["old_threshold_micros"])
                        or int(current["generation_before"])
                        < int(previous["generation_after"])
                        or int(current["generation_after"])
                        <= int(current["generation_before"])
                    ):
                        threshold_trace_ok = False
                        threshold_details.append(
                            {
                                "tenant": tenant["tenant"],
                                "previous_round": int(previous["round_no"]),
                                "current_round": int(current["round_no"]),
                            }
                        )
        checks.append(
            _check(
                "threshold_changes_are_monotonic",
                threshold_trace_ok,
                violations=threshold_details,
            )
        )

        release_state_ok = True
        release_state_details: list[dict[str, Any]] = []
        for release in connection.execute("SELECT * FROM releases ORDER BY release_seq"):
            application = connection.execute(
                "SELECT state FROM applications WHERE application_seq=?",
                (release["application_seq"],),
            ).fetchone()
            if application is None:
                release_state_ok = False
                release_state_details.append(_row_dict(release))
                continue
            if release["state"] == "processed" and application["state"] != "released":
                release_state_ok = False
                release_state_details.append(_row_dict(release))
            if release["state"] == "pending" and application["state"] == "released":
                release_state_ok = False
                release_state_details.append(_row_dict(release))
        checks.append(
            _check(
                "releases_and_applications_final_states_consistent",
                release_state_ok,
                violations=release_state_details,
            )
        )

        pending_watermark_ok = adjust_watermark <= process_watermark
        pending_sched_count = int(
            connection.execute("SELECT COUNT(*) FROM scheduling_rounds").fetchone()[0]
        )
        if adjust_watermark > 0 and pending_sched_count != adjust_watermark:
            pending_watermark_ok = False
        checks.append(
            _check(
                "scheduling_round_watermark_matches_records",
                pending_watermark_ok,
                scheduling_rounds=pending_sched_count,
                adjust_watermark=adjust_watermark,
            )
        )

        completed_processing = process_watermark >= target_round
        observations_ok = True
        if completed_processing:
            pending_observations = int(
                connection.execute(
                    "SELECT COUNT(*) FROM observations WHERE state='pending'"
                ).fetchone()[0]
            )
            observations_ok = pending_observations == 0
        checks.append(
            _check(
                "all_observations_processed_when_target_reached",
                observations_ok,
                target_reached=completed_processing,
            )
        )

        digest = compute_state_digest(connection)
    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "state_digest": digest,
    }

