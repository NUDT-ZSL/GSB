from __future__ import annotations

import json
import os
import sqlite3
import sys
from decimal import Decimal
from fractions import Fraction
from typing import Any

from quota_ledger.db import Store
from quota_ledger.errors import fail
from quota_ledger.models import Dataset, PolicyParams
from quota_ledger.policy import (
    ThresholdSelection,
    policy_params_from_json,
    select_runtime_policy,
)
from quota_ledger.util import (
    MONEY_SCALE,
    canonical_json,
    ceil_fraction,
    micros_to_quota,
    ratio_text,
)


def _split_crash_spec() -> tuple[str | None, int | None]:
    spec = os.environ.get("QUOTA_LEDGER_CRASH_AT", "").strip()
    if not spec:
        return None, None
    try:
        phase, round_text = spec.split(":", 1)
        return phase, int(round_text)
    except (ValueError, TypeError):
        return None, None


def _maybe_crash(phase: str, round_no: int) -> None:
    crash_phase, crash_round = _split_crash_spec()
    if crash_phase == phase and crash_round == round_no:
        print(
            f"intentional crash after committed {phase} round {round_no}",
            file=sys.stderr,
            flush=True,
        )
        os._exit(75)


def _params_for_round(
    connection: sqlite3.Connection,
    store: Store,
    global_policy: dict[str, Any] | None,
    round_start_ms: int,
) -> PolicyParams:
    if global_policy is not None and int(global_policy["effective_clock_ms"]) <= round_start_ms:
        return policy_params_from_json(global_policy["params_json"])
    value = store.meta_get(connection, "default_params_json")
    if value is None:
        fail("E_STATE", "default policy parameters are missing", str(store.path))
    return policy_params_from_json(value)


def _sum_thresholds(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(SUM(threshold_micros), 0) FROM tenants"
        ).fetchone()[0]
    )


def _sum_occupancy(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(SUM(occupancy_micros), 0) FROM tenants"
        ).fetchone()[0]
    )


def _apply_round_policies(
    connection: sqlite3.Connection,
    store: Store,
    round_no: int,
    round_start_ms: int,
    global_policy: dict[str, Any] | None,
    selections: dict[str, ThresholdSelection],
    params: PolicyParams,
) -> list[str]:
    applied: list[str] = []
    if global_policy is not None and global_policy["effective_clock_ms"] <= round_start_ms:
        connection.execute(
            "INSERT OR IGNORE INTO applied_policies("
            "policy_id, round_no, effective_clock_ms, fingerprint"
            ") VALUES(?,?,?,?)",
            (
                global_policy["policy_id"],
                round_no,
                global_policy["effective_clock_ms"],
                global_policy["fingerprint"],
            ),
        )
    for tenant, selection in sorted(selections.items()):
        row = connection.execute(
            "SELECT * FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()
        if row is None:
            continue
        if row["policy_id"] == selection.policy_id:
            continue
        if selection.effective_clock_ms > round_start_ms:
            continue
        old_threshold = int(row["threshold_micros"])
        old_generation = int(row["threshold_generation"])
        new_threshold = max(selection.threshold_micros, int(row["occupancy_micros"]))
        new_generation = max(old_generation + 1, selection.policy_generation)
        new_quota_generation = max(
            int(row["quota_generation"]), selection.policy_generation, new_generation
        )
        event = connection.execute(
            "SELECT id FROM policy_events WHERE policy_id=? AND tenant=?",
            (selection.policy_id, tenant),
        ).fetchone()
        if event is not None:
            continue
        connection.execute(
            "INSERT INTO policy_events("
            "policy_id, tenant, round_no, old_threshold_micros, new_threshold_micros, "
            "source_version, effective_clock_ms"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                selection.policy_id,
                tenant,
                round_no,
                old_threshold,
                new_threshold,
                selection.source_version,
                selection.effective_clock_ms,
            ),
        )
        connection.execute(
            "UPDATE tenants SET threshold_micros=?, threshold_generation=?, "
            "quota_generation=?, source_version=?, source_snapshot_id=?, policy_id=?, "
            "effective_clock_ms=?, policy_generation=?, safety_watermark_micros=? "
            "WHERE tenant=?",
            (
                new_threshold,
                new_generation,
                new_quota_generation,
                selection.source_version,
                selection.source_snapshot_id,
                selection.policy_id,
                selection.effective_clock_ms,
                selection.policy_generation,
                params.safety_watermark_micros,
                tenant,
            ),
        )
        if new_threshold != old_threshold:
            connection.execute(
                "INSERT INTO threshold_changes("
                "round_no, tenant, old_threshold_micros, new_threshold_micros, "
                "delta_micros, generation_before, generation_after, reason, "
                "source_version, policy_id"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    round_no,
                    tenant,
                    old_threshold,
                    new_threshold,
                    new_threshold - old_threshold,
                    old_generation,
                    new_generation,
                    "policy_apply",
                    selection.source_version,
                    selection.policy_id,
                ),
            )
        applied.append(tenant)
    connection.execute(
        "UPDATE tenants SET safety_watermark_micros=?",
        (params.safety_watermark_micros,),
    )
    return applied


def _drain_backlog(
    connection: sqlite3.Connection,
    round_no: int,
    round_start_ms: int,
) -> int:
    drained_count = 0
    rows = connection.execute(
        "SELECT * FROM tenants WHERE shave_backlog_micros>0 "
        "ORDER BY tenant"
    ).fetchall()
    for row in rows:
        backlog_before = int(row["shave_backlog_micros"])
        threshold = int(row["threshold_micros"])
        occupancy = int(row["occupancy_micros"])
        safety = int(row["safety_watermark_micros"])
        drain = min(backlog_before, safety)
        if drain <= 0:
            continue
        backlog_after = backlog_before - drain
        occupancy_after = occupancy
        connection.execute(
            "UPDATE tenants SET shave_backlog_micros=?, occupancy_micros=?, "
            "cumulative_released_micros=cumulative_released_micros+? WHERE tenant=?",
            (backlog_after, occupancy_after, drain, row["tenant"]),
        )
        remaining = threshold - occupancy_after
        cost = 0
        safety_after = min(safety, max(0, remaining))
        event_key = f"drain:{round_no}:{row['tenant']}"
        connection.execute(
            "INSERT INTO ledger_entries("
            "event_key, event_kind, tenant, resource_key, quota_generation, "
            "policy_generation, round_no, logical_clock_ms, amount_micros, "
            "estimated_micros, actual_micros, difference_micros, estimate_scale, "
            "actual_scale, rounding_direction, error_bound_micros, "
            "cumulative_error_bound_micros, reserved_after_micros, "
            "occupancy_after_micros, available_after_micros, shaved_micros, details_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_key,
                "shave_drain",
                row["tenant"],
                row["resource_key"],
                row["quota_generation"],
                row["policy_generation"],
                round_no,
                round_start_ms,
                0,
                0,
                None,
                0,
                MONEY_SCALE,
                MONEY_SCALE,
                "none",
                0,
                row["cumulative_error_bound_micros"],
                row["reserved_micros"],
                occupancy_after,
                remaining,
                drain,
                canonical_json(
                    {
                        "kind": "backlog_drain",
                        "backlog_before_micros": backlog_before,
                        "backlog_after_micros": backlog_after,
                        "occupancy_unchanged_micros": occupancy_after,
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO shave_logs("
            "event_key, round_no, tenant, application_seq, release_seq, kind, "
            "original_difference_micros, cap_micros, returned_micros, deferred_micros, "
            "drained_micros, cost_micros, remaining_after_micros, "
            "safety_watermark_after_micros, safe_watermark_preserved, details_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_key,
                round_no,
                row["tenant"],
                None,
                None,
                "backlog_drain",
                backlog_before,
                drain,
                drain,
                backlog_after,
                drain,
                cost,
                remaining,
                safety_after,
                1,
                canonical_json({"drain": drain, "cost": cost}),
            ),
        )
        drained_count += 1
    return drained_count


def _record_rejected_application(
    connection: sqlite3.Connection,
    application: sqlite3.Row,
    tenant: sqlite3.Row,
    round_no: int,
    estimated_micros: int,
    error_bound_micros: int,
    reason: str,
) -> None:
    connection.execute(
        "UPDATE applications SET state='rejected', reserved_micros=0, "
        "estimate_error_bound_micros=?, applied_round=?, reject_reason=? "
        "WHERE application_seq=?",
        (
            error_bound_micros,
            round_no,
            reason,
            application["application_seq"],
        ),
    )
    connection.execute(
        "INSERT INTO ledger_entries("
        "event_key, event_kind, application_seq, tenant, resource_key, "
        "quota_generation, policy_generation, round_no, logical_clock_ms, "
        "amount_micros, estimated_micros, actual_micros, difference_micros, "
        "estimate_scale, actual_scale, rounding_direction, error_bound_micros, "
        "cumulative_error_bound_micros, reserved_after_micros, "
        "occupancy_after_micros, available_after_micros, shaved_micros, details_json"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"apply:{application['application_seq']}",
            "apply_rejected",
            application["application_seq"],
            application["tenant"],
            application["resource_key"],
            tenant["quota_generation"],
            tenant["policy_generation"],
            round_no,
            application["logical_clock_ms"],
            0,
            estimated_micros,
            None,
            0,
            MONEY_SCALE,
            MONEY_SCALE,
            "up",
            error_bound_micros,
            tenant["cumulative_error_bound_micros"],
            tenant["reserved_micros"],
            tenant["occupancy_micros"],
            tenant["threshold_micros"] - tenant["occupancy_micros"],
            0,
            canonical_json({"status": "rejected", "reason": reason}),
        ),
    )


def _process_applications(
    connection: sqlite3.Connection,
    round_no: int,
    round_start_ms: int,
    round_end_ms: int,
    global_budget_micros: int,
) -> int:
    rows = connection.execute(
        "SELECT * FROM applications WHERE state='pending' "
        "AND logical_clock_ms BETWEEN ? AND ? "
        "ORDER BY priority DESC, logical_clock_ms, application_seq",
        (round_start_ms, round_end_ms),
    ).fetchall()
    processed = 0
    global_occupancy = _sum_occupancy(connection)
    for application in rows:
        if application["state"] != "pending":
            continue
        tenant_row = connection.execute(
            "SELECT * FROM tenants WHERE tenant=?", (application["tenant"],)
        ).fetchone()
        if tenant_row is None:
            fail("E_STATE", "application tenant is missing from state", str(application["tenant"]))
        elasticity = Fraction(tenant_row["elasticity_text"])
        exact_peak = Fraction(application["requested_micros"]) * elasticity
        reserve = ceil_fraction(exact_peak)
        estimate_rounding_error = ceil_fraction(Fraction(reserve) - exact_peak)
        estimate_error = int(application["requested_error_micros"]) + estimate_rounding_error
        threshold = int(tenant_row["threshold_micros"])
        occupancy = int(tenant_row["occupancy_micros"])
        reason = ""
        if occupancy + reserve > threshold:
            reason = "tenant_threshold_exceeded"
        elif global_occupancy + reserve > global_budget_micros:
            reason = "global_quota_budget_exceeded"
        if reason:
            _record_rejected_application(
                connection,
                application,
                tenant_row,
                round_no,
                reserve,
                estimate_error,
                reason,
            )
            processed += 1
            continue
        new_reserved = int(tenant_row["reserved_micros"]) + reserve
        new_occupancy = occupancy + reserve
        new_error = int(tenant_row["cumulative_error_bound_micros"]) + estimate_error
        connection.execute(
            "UPDATE applications SET state='accepted', reserved_micros=?, "
            "estimate_error_bound_micros=?, applied_round=? WHERE application_seq=?",
            (reserve, estimate_error, round_no, application["application_seq"]),
        )
        connection.execute(
            "UPDATE tenants SET reserved_micros=?, occupancy_micros=?, "
            "cumulative_error_bound_micros=? WHERE tenant=?",
            (
                new_reserved,
                new_occupancy,
                new_error,
                application["tenant"],
            ),
        )
        connection.execute(
            "INSERT INTO ledger_entries("
            "event_key, event_kind, application_seq, tenant, resource_key, "
            "quota_generation, policy_generation, round_no, logical_clock_ms, "
            "amount_micros, estimated_micros, actual_micros, difference_micros, "
            "estimate_scale, actual_scale, rounding_direction, error_bound_micros, "
            "cumulative_error_bound_micros, reserved_after_micros, "
            "occupancy_after_micros, available_after_micros, shaved_micros, details_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"apply:{application['application_seq']}",
                "apply",
                application["application_seq"],
                application["tenant"],
                application["resource_key"],
                tenant_row["quota_generation"],
                tenant_row["policy_generation"],
                round_no,
                application["logical_clock_ms"],
                reserve,
                reserve,
                None,
                0,
                MONEY_SCALE,
                MONEY_SCALE,
                "up",
                estimate_error,
                new_error,
                new_reserved,
                new_occupancy,
                threshold - new_occupancy,
                0,
                canonical_json(
                    {
                        "elasticity": tenant_row["elasticity_text"],
                        "requested_micros": application["requested_micros"],
                        "priority": application["priority"],
                    }
                ),
            ),
        )
        global_occupancy += reserve
        processed += 1
    return processed


def _release_shave_log(
    connection: sqlite3.Connection,
    *,
    event_key: str,
    round_no: int,
    tenant_row: sqlite3.Row,
    application_seq: int | None,
    release_seq: int | None,
    kind: str,
    original_difference: int,
    cap: int,
    returned: int,
    deferred: int,
    drained: int,
    cost: int,
    remaining_after: int,
    safety_after: int,
    details: dict[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO shave_logs("
        "event_key, round_no, tenant, application_seq, release_seq, kind, "
        "original_difference_micros, cap_micros, returned_micros, deferred_micros, "
        "drained_micros, cost_micros, remaining_after_micros, "
        "safety_watermark_after_micros, safe_watermark_preserved, details_json"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_key,
            round_no,
            tenant_row["tenant"],
            application_seq,
            release_seq,
            kind,
            original_difference,
            cap,
            returned,
            deferred,
            drained,
            cost,
            remaining_after,
            safety_after,
            1,
            canonical_json(details),
        ),
    )


def _process_releases(
    connection: sqlite3.Connection,
    round_no: int,
    round_start_ms: int,
    round_end_ms: int,
    global_budget_micros: int,
) -> int:
    rows = connection.execute(
        "SELECT * FROM releases WHERE state='pending' "
        "AND logical_clock_ms BETWEEN ? AND ? "
        "ORDER BY logical_clock_ms, release_seq",
        (round_start_ms, round_end_ms),
    ).fetchall()
    processed = 0
    global_occupancy = _sum_occupancy(connection)
    for release in rows:
        application = connection.execute(
            "SELECT * FROM applications WHERE application_seq=?",
            (release["application_seq"],),
        ).fetchone()
        tenant_row = connection.execute(
            "SELECT * FROM tenants WHERE tenant=?", (release["tenant"],)
        ).fetchone()
        if application is None or tenant_row is None:
            fail("E_STATE", "release references missing state", str(release["release_seq"]))
        if application["state"] != "accepted":
            connection.execute(
                "UPDATE releases SET state='ignored', processed_round=? WHERE release_seq=?",
                (round_no, release["release_seq"]),
            )
            connection.execute(
                "INSERT INTO ledger_entries("
                "event_key, event_kind, application_seq, release_seq, tenant, resource_key, "
                "quota_generation, policy_generation, round_no, logical_clock_ms, "
                "amount_micros, estimated_micros, actual_micros, difference_micros, "
                "estimate_scale, actual_scale, rounding_direction, error_bound_micros, "
                "cumulative_error_bound_micros, reserved_after_micros, "
                "occupancy_after_micros, available_after_micros, shaved_micros, details_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"release:{release['release_seq']}",
                    "release_ignored",
                    application["application_seq"],
                    release["release_seq"],
                    release["tenant"],
                    release["resource_key"],
                    tenant_row["quota_generation"],
                    tenant_row["policy_generation"],
                    round_no,
                    release["logical_clock_ms"],
                    0,
                    0,
                    None,
                    0,
                    MONEY_SCALE,
                    MONEY_SCALE,
                    "none",
                    0,
                    tenant_row["cumulative_error_bound_micros"],
                    tenant_row["reserved_micros"],
                    tenant_row["occupancy_micros"],
                    tenant_row["threshold_micros"] - tenant_row["occupancy_micros"],
                    0,
                    canonical_json({"reason": "application_not_active"}),
                ),
            )
            processed += 1
            continue

        reserve = int(application["reserved_micros"])
        requested_actual = int(release["actual_micros"])
        threshold = int(tenant_row["threshold_micros"])
        occupancy = int(tenant_row["occupancy_micros"])
        extra = max(0, requested_actual - reserve)
        actual_effective = requested_actual
        overrun_shaved = extra
        difference = reserve - actual_effective
        safety = int(tenant_row["safety_watermark_micros"])
        deferred = 0
        if difference > safety:
            deferred = difference - safety
            new_backlog = int(tenant_row["shave_backlog_micros"]) + deferred
            kind = "release_shave"
        else:
            deferred = 0
            new_backlog = int(tenant_row["shave_backlog_micros"])
            kind = ""
        returned = reserve
        new_occupancy = occupancy - reserve
        if new_occupancy < 0 or new_backlog < 0:
            fail("E_STATE", "release would make occupancy or backlog negative", str(release["release_seq"]))
        remaining_after = threshold - new_occupancy
        if remaining_after < 0:
            fail(
                "E_STATE",
                "release would make tenant remaining quota negative",
                str(release["release_seq"]),
            )
        safety_floor = min(safety, remaining_after)
        shave_cost_capacity = max(0, remaining_after - safety_floor)
        shave_cost = min(deferred, shave_cost_capacity)
        safety_after = min(safety, max(0, remaining_after - shave_cost))
        new_reserved = int(tenant_row["reserved_micros"]) - reserve
        if new_reserved < 0:
            fail("E_STATE", "release would make reserved quota negative", str(release["release_seq"]))
        error_bound = int(application["estimate_error_bound_micros"]) + int(
            release["actual_error_micros"]
        )
        new_error = int(tenant_row["cumulative_error_bound_micros"]) + error_bound
        connection.execute(
            "UPDATE tenants SET reserved_micros=?, occupancy_micros=?, "
            "shave_backlog_micros=?, shave_cost_micros=shave_cost_micros+?, "
            "cumulative_actual_micros=cumulative_actual_micros+?, "
            "cumulative_released_micros=cumulative_released_micros+?, "
            "cumulative_error_bound_micros=? WHERE tenant=?",
            (
                new_reserved,
                new_occupancy,
                new_backlog,
                shave_cost,
                actual_effective,
                reserve,
                new_error,
                release["tenant"],
            ),
        )
        connection.execute(
            "UPDATE applications SET state='released', actual_micros=? WHERE application_seq=?",
            (actual_effective, application["application_seq"]),
        )
        connection.execute(
            "UPDATE releases SET state='processed', processed_round=?, "
            "difference_micros=?, shaved_micros=? WHERE release_seq=?",
            (
                round_no,
                difference,
                deferred + overrun_shaved,
                release["release_seq"],
            ),
        )
        connection.execute(
            "INSERT INTO ledger_entries("
            "event_key, event_kind, application_seq, release_seq, tenant, resource_key, "
            "quota_generation, policy_generation, round_no, logical_clock_ms, "
            "amount_micros, estimated_micros, actual_micros, difference_micros, "
            "estimate_scale, actual_scale, rounding_direction, error_bound_micros, "
            "cumulative_error_bound_micros, reserved_after_micros, "
            "occupancy_after_micros, available_after_micros, shaved_micros, details_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"release:{release['release_seq']}",
                "release",
                application["application_seq"],
                release["release_seq"],
                release["tenant"],
                release["resource_key"],
                tenant_row["quota_generation"],
                tenant_row["policy_generation"],
                round_no,
                release["logical_clock_ms"],
                new_occupancy - occupancy,
                reserve,
                actual_effective,
                difference,
                MONEY_SCALE,
                MONEY_SCALE,
                release["actual_rounding"],
                error_bound,
                new_error,
                new_reserved,
                new_occupancy,
                remaining_after,
                deferred + overrun_shaved,
                canonical_json(
                    {
                        "requested_actual_micros": requested_actual,
                        "actual_effective_micros": actual_effective,
                "overrun_shaved_micros": overrun_shaved,
                        "deferred_micros": deferred,
                        "safety_watermark_micros": safety,
                        "shave_cost_micros": shave_cost,
                    }
                ),
            ),
        )
        global_occupancy += new_occupancy - occupancy
        if kind:
            _release_shave_log(
                connection,
                event_key=f"shave:{release['release_seq']}",
                round_no=round_no,
                tenant_row=tenant_row,
                application_seq=application["application_seq"],
                release_seq=release["release_seq"],
                kind=kind,
                original_difference=difference,
                cap=safety,
                returned=returned,
                deferred=deferred,
                drained=0,
                cost=shave_cost,
                remaining_after=remaining_after,
                safety_after=safety_after,
                details={
                    "requested_actual_micros": requested_actual,
                    "actual_effective_micros": actual_effective,
                    "released_reservation_micros": returned,
                    "backlog_after_micros": new_backlog,
                },
            )
        if overrun_shaved:
            _release_shave_log(
                connection,
                event_key=f"overrun:{release['release_seq']}",
                round_no=round_no,
                tenant_row=tenant_row,
                application_seq=application["application_seq"],
                release_seq=release["release_seq"],
                kind="overrun_shave",
                original_difference=extra,
                cap=reserve,
                returned=0,
                deferred=overrun_shaved,
                drained=0,
                cost=0,
                remaining_after=remaining_after,
                safety_after=safety_after,
                details={
                    "requested_actual_micros": requested_actual,
                    "reserved_micros": reserve,
                    "reason": "actual_exceeded_estimated_peak",
                },
            )
        processed += 1
    return processed


def _process_observations(
    connection: sqlite3.Connection,
    round_no: int,
    round_start_ms: int,
    round_end_ms: int,
    params: PolicyParams,
) -> int:
    rows = connection.execute(
        "SELECT * FROM observations WHERE state='pending' "
        "AND logical_clock_ms BETWEEN ? AND ? "
        "ORDER BY logical_clock_ms, observation_seq",
        (round_start_ms, round_end_ms),
    ).fetchall()
    for observation in rows:
        tenant = connection.execute(
            "SELECT * FROM tenants WHERE tenant=?", (observation["tenant"],)
        ).fetchone()
        if tenant is None:
            fail("E_STATE", "observation tenant is missing", str(observation["observation_seq"]))
        threshold = int(tenant["threshold_micros"])
        observed = int(observation["actual_occupancy_micros"])
        deviation = observed - threshold
        if threshold > 0:
            ratio = Fraction(abs(deviation), threshold)
            ratio_value_text = ratio_text(ratio)
        else:
            ratio = Fraction(0) if deviation == 0 else None
            ratio_value_text = "0" if deviation == 0 else "inf"
        dangerous = bool(
            deviation > 0
            and (
                threshold == 0
                or (ratio is not None and ratio >= params.danger_threshold_ratio)
            )
        )
        connection.execute(
            "UPDATE observations SET state='processed', processed_round=?, "
            "quota_fixedpoint_micros=?, deviation_micros=?, deviation_ratio_text=?, "
            "drift_threshold_text=?, is_danger=?, policy_generation=?, "
            "policy_id=?, decision_status='pending' WHERE observation_seq=?",
            (
                round_no,
                threshold,
                deviation,
                ratio_value_text,
                params.drift_threshold_text,
                1 if dangerous else 0,
                tenant["policy_generation"],
                tenant["policy_id"],
                observation["observation_seq"],
            ),
        )
    return len(rows)


def _sum_drained_by_tenant(
    connection: sqlite3.Connection, round_no: int
) -> dict[str, int]:
    rows = connection.execute(
        "SELECT tenant, COALESCE(SUM(drained_micros), 0) AS drained "
        "FROM shave_logs WHERE round_no=? AND kind='backlog_drain' "
        "GROUP BY tenant",
        (round_no,),
    ).fetchall()
    return {row["tenant"]: int(row["drained"]) for row in rows if int(row["drained"]) > 0}


def _candidate_decision(
    connection: sqlite3.Connection,
    *,
    round_no: int,
    candidate: dict[str, Any],
    status: str,
    reason: str,
    budget_before: int,
    budget_after: int,
    remaining_after: int,
    applied_delta: int,
    threshold_after: int,
    candidate_order: int,
) -> None:
    connection.execute(
        "INSERT INTO drift_decisions("
        "round_no, tenant, observation_seq, source_version, policy_generation, "
        "quota_fixedpoint_micros, observed_micros, deviation_micros, "
        "deviation_ratio_text, drift_threshold_text, direction, dangerous, fair_tag, "
        "desired_delta_micros, applied_delta_micros, threshold_before_micros, "
        "threshold_after_micros, remaining_after_micros, budget_before_micros, "
        "budget_after_micros, candidate_order, status, reason"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            round_no,
            candidate["tenant"],
            candidate["observation_seq"],
            candidate["source_version"],
            candidate["policy_generation"],
            candidate["quota_fixedpoint_micros"],
            candidate["observed_micros"],
            candidate["deviation_micros"],
            candidate["deviation_ratio_text"],
            candidate["drift_threshold_text"],
            candidate["direction"],
            candidate["dangerous"],
            candidate["fair_tag"],
            candidate["desired_delta_micros"],
            applied_delta,
            candidate["threshold_before_micros"],
            threshold_after,
            remaining_after,
            budget_before,
            budget_after,
            candidate_order,
            status,
            reason,
        ),
    )
    if candidate["observation_seq"]:
        connection.execute(
            "UPDATE observations SET decision_status=? WHERE observation_seq=?",
            (status, candidate["observation_seq"]),
        )


def _adjust_round(
    connection: sqlite3.Connection,
    store: Store,
    round_no: int,
) -> dict[str, Any]:
    pipeline = connection.execute(
        "SELECT * FROM pipeline_rounds WHERE round_no=? AND process_status='committed'",
        (round_no,),
    ).fetchone()
    if pipeline is None:
        fail("E_ROUND_NOT_PROCESSED", f"round {round_no} has not been processed")
    existing = connection.execute(
        "SELECT * FROM scheduling_rounds WHERE round_no=?", (round_no,)
    ).fetchone()
    if existing is not None:
        return {
            "round_no": round_no,
            "status": "already_committed",
            "used_micros": int(existing["used_micros"]),
            "remaining_micros": int(existing["remaining_micros"]),
        }
    budget = int(pipeline["round_budget_micros"])
    per_cap = int(pipeline["per_tenant_cap_micros"])
    global_budget = int(pipeline["global_budget_micros"])
    safety = int(pipeline["safety_watermark_micros"])
    drift_threshold = Fraction(pipeline["drift_threshold_text"])
    danger_threshold = Fraction(pipeline["danger_threshold_text"])
    rows = connection.execute(
        "SELECT * FROM observations WHERE processed_round=? "
        "ORDER BY logical_clock_ms, observation_seq",
        (round_no,),
    ).fetchall()
    latest_by_tenant: dict[str, sqlite3.Row] = {}
    older_by_tenant: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        tenant = row["tenant"]
        if tenant in latest_by_tenant:
            older_by_tenant.setdefault(tenant, []).append(latest_by_tenant[tenant])
        latest_by_tenant[tenant] = row
    for tenant, older in older_by_tenant.items():
        for row in older:
            connection.execute(
                "UPDATE observations SET decision_status='superseded' "
                "WHERE observation_seq=?",
                (row["observation_seq"],),
            )
    claimants: dict[str, dict[str, Any]] = {}
    no_drift: dict[str, sqlite3.Row] = {}
    for tenant, observation in sorted(latest_by_tenant.items()):
        tenant_row = connection.execute(
            "SELECT * FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()
        threshold = int(observation["quota_fixedpoint_micros"])
        deviation = int(observation["deviation_micros"])
        if threshold > 0:
            ratio = Fraction(abs(deviation), threshold)
        else:
            ratio = Fraction(0) if deviation == 0 else None
        exceeds = (
            (ratio is None and deviation != 0)
            or (ratio is not None and ratio > drift_threshold)
        )
        if not exceeds:
            no_drift[tenant] = observation
            continue
        direction = "up" if deviation > 0 else "down"
        dangerous = bool(
            deviation > 0
            and (
                threshold == 0
                or (ratio is not None and ratio >= danger_threshold)
            )
        )
        fair_tag = (
            "danger"
            if dangerous
            else ("converge_up" if direction == "up" else "converge_down")
        )
        claimants[tenant] = {
            "tenant": tenant,
            "observation_seq": int(observation["observation_seq"]),
            "source_version": tenant_row["source_version"],
            "policy_generation": int(observation["policy_generation"]),
            "quota_fixedpoint_micros": threshold,
            "observed_micros": int(observation["actual_occupancy_micros"]),
            "deviation_micros": deviation,
            "deviation_ratio_text": observation["deviation_ratio_text"],
            "drift_threshold_text": observation["drift_threshold_text"],
            "direction": direction,
            "dangerous": 1 if dangerous else 0,
            "fair_tag": fair_tag,
            "desired_delta_micros": abs(deviation),
            "threshold_before_micros": int(tenant_row["threshold_micros"]),
            "occupancy_micros": int(tenant_row["occupancy_micros"]),
            "last_adjust_round": int(tenant_row["last_adjust_round"]),
            "rollback": False,
        }
    drained = _sum_drained_by_tenant(connection, round_no)
    for tenant, drained_micros in sorted(drained.items()):
        tenant_row = connection.execute(
            "SELECT * FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()
        if tenant in claimants:
            claim = claimants[tenant]
            if claim["direction"] == "down":
                claim["desired_delta_micros"] = max(
                    claim["desired_delta_micros"], drained_micros
                )
                claim["rollback"] = True
                claim["fair_tag"] = "rollback"
            continue
        claimants[tenant] = {
            "tenant": tenant,
            "observation_seq": 0,
            "source_version": tenant_row["source_version"],
            "policy_generation": int(tenant_row["policy_generation"]),
            "quota_fixedpoint_micros": int(tenant_row["threshold_micros"]),
            "observed_micros": int(tenant_row["occupancy_micros"]),
            "deviation_micros": -drained_micros,
            "deviation_ratio_text": "rollback",
            "drift_threshold_text": pipeline["drift_threshold_text"],
            "direction": "down",
            "dangerous": 0,
            "fair_tag": "rollback",
            "desired_delta_micros": drained_micros,
            "threshold_before_micros": int(tenant_row["threshold_micros"]),
            "occupancy_micros": int(tenant_row["occupancy_micros"]),
            "last_adjust_round": int(tenant_row["last_adjust_round"]),
            "rollback": True,
        }

    rank = {"danger": 0, "converge_up": 1, "rollback": 2, "converge_down": 3}
    ordered = sorted(
        claimants.values(),
        key=lambda item: (
            rank[item["fair_tag"]],
            item["last_adjust_round"],
            -item["desired_delta_micros"],
            item["tenant"],
        ),
    )
    threshold_sum = _sum_thresholds(connection)
    used = 0
    abandoned: list[dict[str, Any]] = []
    candidate_summaries: list[dict[str, Any]] = []
    for order, candidate in enumerate(ordered, start=1):
        remaining_budget = budget - used
        desired = int(candidate["desired_delta_micros"])
        direction = candidate["direction"]
        tenant_row = connection.execute(
            "SELECT * FROM tenants WHERE tenant=?", (candidate["tenant"],)
        ).fetchone()
        threshold_before = int(tenant_row["threshold_micros"])
        max_allowed = min(desired, per_cap, remaining_budget)
        reason = ""
        if direction == "up":
            global_room = max(0, global_budget - threshold_sum)
            if global_room <= 0:
                max_allowed = 0
                reason = "global_threshold_budget_exhausted"
            elif max_allowed > global_room:
                max_allowed = global_room
                reason = "global_budget_partial"
        else:
            safety_floor = int(tenant_row["occupancy_micros"]) + safety
            down_room = max(0, threshold_before - safety_floor)
            if down_room <= 0:
                max_allowed = 0
                reason = "safety_floor"
            elif max_allowed > down_room:
                max_allowed = down_room
                reason = "safety_floor_partial"
        if max_allowed <= 0:
            applied = 0
            status = "abandoned"
            if not reason:
                reason = "round_budget_exhausted"
            threshold_after = threshold_before
        else:
            applied = max_allowed
            threshold_after = (
                threshold_before + applied
                if direction == "up"
                else threshold_before - applied
            )
            if applied < desired:
                status = "partial"
                if not reason:
                    if remaining_budget <= applied:
                        reason = "round_budget_partial"
                    elif desired > per_cap:
                        reason = "per_tenant_cap"
            else:
                status = "applied"
                reason = "fair_schedule"
            new_generation = int(tenant_row["threshold_generation"]) + 1
            new_quota_generation = int(tenant_row["quota_generation"]) + 1
            connection.execute(
                "UPDATE tenants SET threshold_micros=?, threshold_generation=?, "
                "quota_generation=?, cumulative_adjustment_micros="
                "cumulative_adjustment_micros+?, last_adjust_round=? WHERE tenant=?",
                (
                    threshold_after,
                    new_generation,
                    new_quota_generation,
                    applied,
                    round_no,
                    candidate["tenant"],
                ),
            )
            connection.execute(
                "INSERT INTO threshold_changes("
                "round_no, tenant, old_threshold_micros, new_threshold_micros, "
                "delta_micros, generation_before, generation_after, reason, "
                "source_version, policy_id"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    round_no,
                    candidate["tenant"],
                    threshold_before,
                    threshold_after,
                    applied if direction == "up" else -applied,
                    int(tenant_row["threshold_generation"]),
                    new_generation,
                    reason,
                    candidate["source_version"],
                    tenant_row["policy_id"],
                ),
            )
            threshold_sum += applied if direction == "up" else -applied
            used += applied
        remaining_after = budget - used
        candidate["threshold_after_micros"] = threshold_after
        _candidate_decision(
            connection,
            round_no=round_no,
            candidate=candidate,
            status=status,
            reason=reason,
            budget_before=remaining_budget,
            budget_after=remaining_after,
            remaining_after=remaining_after,
            applied_delta=applied,
            threshold_after=threshold_after,
            candidate_order=order,
        )
        summary = {
            "candidate_order": order,
            "tenant": candidate["tenant"],
            "fair_tag": candidate["fair_tag"],
            "dangerous": bool(candidate["dangerous"]),
            "direction": direction,
            "desired_delta_micros": desired,
            "applied_delta_micros": applied,
            "status": status,
            "reason": reason,
            "budget_remaining_micros": remaining_after,
        }
        candidate_summaries.append(summary)
        if applied == 0 or status == "partial":
            abandoned.append(
                {
                    "tenant": candidate["tenant"],
                    "reason": reason,
                    "desired_delta_micros": desired,
                    "applied_delta_micros": applied,
                    "remaining_budget_micros": remaining_after,
                }
            )
    for tenant, observation in sorted(no_drift.items()):
        tenant_row = connection.execute(
            "SELECT * FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()
        candidate = {
            "tenant": tenant,
            "observation_seq": int(observation["observation_seq"]),
            "source_version": tenant_row["source_version"],
            "policy_generation": int(observation["policy_generation"]),
            "quota_fixedpoint_micros": int(observation["quota_fixedpoint_micros"]),
            "observed_micros": int(observation["actual_occupancy_micros"]),
            "deviation_micros": int(observation["deviation_micros"]),
            "deviation_ratio_text": observation["deviation_ratio_text"],
            "drift_threshold_text": observation["drift_threshold_text"],
            "direction": "none",
            "dangerous": 0,
            "fair_tag": "none",
            "desired_delta_micros": 0,
            "threshold_before_micros": int(tenant_row["threshold_micros"]),
            "occupancy_micros": int(tenant_row["occupancy_micros"]),
            "last_adjust_round": int(tenant_row["last_adjust_round"]),
            "rollback": False,
        }
        _candidate_decision(
            connection,
            round_no=round_no,
            candidate=candidate,
            status="no_drift",
            reason="within_drift_threshold",
            budget_before=budget - used,
            budget_after=budget - used,
            remaining_after=budget - used,
            applied_delta=0,
            threshold_after=int(tenant_row["threshold_micros"]),
            candidate_order=0,
        )
    connection.execute(
        "INSERT INTO scheduling_rounds("
        "round_no, budget_micros, used_micros, remaining_micros, candidate_count, "
        "danger_candidate_count, abandoned_count, candidates_json, abandoned_json, status"
        ") VALUES(?,?,?,?,?,?,?,?,?, 'committed')",
        (
            round_no,
            budget,
            used,
            budget - used,
            len(ordered),
            sum(1 for item in ordered if item["dangerous"]),
            len(abandoned),
            canonical_json(candidate_summaries),
            canonical_json(abandoned),
        ),
    )
    connection.execute(
        "UPDATE pipeline_rounds SET adjust_status='committed' WHERE round_no=?",
        (round_no,),
    )
    store.meta_set(
        connection,
        "adjust_round_watermark",
        max(store.meta_get_int(connection, "adjust_round_watermark"), round_no),
    )
    store.meta_set(
        connection,
        "state_generation",
        store.meta_get_int(connection, "state_generation") + 1,
    )
    return {
        "round_no": round_no,
        "status": "committed",
        "used_micros": used,
        "remaining_micros": budget - used,
        "candidates": candidate_summaries,
        "abandoned": abandoned,
    }


def process_round(store: Store, round_no: int) -> dict[str, Any]:
    with store.write_tx() as connection:
        process_watermark = store.meta_get_int(connection, "process_round_watermark")
        if round_no <= process_watermark:
            return {
                "round_no": round_no,
                "status": "already_processed",
                "process_round_watermark": process_watermark,
            }
        if round_no != process_watermark + 1:
            fail(
                "E_ROUND_ORDER",
                "rounds must be processed sequentially",
                expected=process_watermark + 1,
                actual=round_no,
            )
        target_round = store.meta_get_int(connection, "target_round")
        if round_no > target_round:
            return {
                "round_no": round_no,
                "status": "beyond_target",
                "target_round": target_round,
            }
        round_span = store.meta_get_int(connection, "round_span_ms")
        round_start = (round_no - 1) * round_span
        round_end = round_no * round_span - 1
        global_policy, selections = select_runtime_policy(connection, round_start)
        params = _params_for_round(connection, store, global_policy, round_start)
        applied_tenants = _apply_round_policies(
            connection,
            store,
            round_no,
            round_start,
            global_policy,
            selections,
            params,
        )
        drained = _drain_backlog(connection, round_no, round_start)
        applications = _process_applications(
            connection,
            round_no,
            round_start,
            round_end,
            params.global_quota_budget_micros,
        )
        releases = _process_releases(
            connection,
            round_no,
            round_start,
            round_end,
            params.global_quota_budget_micros,
        )
        observations = _process_observations(
            connection, round_no, round_start, round_end, params
        )
        processed_clock_max = connection.execute(
            "SELECT COALESCE(MAX(logical_clock_ms), 0) FROM ("
            "SELECT logical_clock_ms FROM applications WHERE logical_clock_ms BETWEEN ? AND ? "
            "UNION ALL SELECT logical_clock_ms FROM releases WHERE logical_clock_ms BETWEEN ? AND ? "
            "UNION ALL SELECT logical_clock_ms FROM observations WHERE logical_clock_ms BETWEEN ? AND ?"
            ")",
            (round_start, round_end, round_start, round_end, round_start, round_end),
        ).fetchone()[0]
        policy_id = "" if global_policy is None else global_policy["policy_id"]
        connection.execute(
            "INSERT INTO pipeline_rounds("
            "round_no, round_start_ms, round_end_ms, process_status, adjust_status, "
            "global_budget_micros, round_budget_micros, per_tenant_cap_micros, "
            "safety_watermark_micros, drift_threshold_text, danger_threshold_text, "
            "policy_id, processed_applications, processed_releases, "
            "processed_observations, processed_clock_max_ms"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                round_no,
                round_start,
                round_end,
                "committed",
                "pending",
                params.global_quota_budget_micros,
                params.round_threshold_budget_micros,
                params.per_tenant_adjustment_cap_micros,
                params.safety_watermark_micros,
                params.drift_threshold_text,
                params.danger_threshold_text,
                policy_id,
                applications,
                releases,
                observations,
                int(processed_clock_max),
            ),
        )
        store.meta_set(connection, "process_round_watermark", round_no)
        store.meta_set(
            connection,
            "logical_clock_watermark_ms",
            max(
                store.meta_get_int(connection, "logical_clock_watermark_ms"),
                int(processed_clock_max),
                round_start,
            ),
        )
        store.meta_set(
            connection,
            "state_generation",
            store.meta_get_int(connection, "state_generation") + 1,
        )
        return {
            "round_no": round_no,
            "status": "committed",
            "round_start_ms": round_start,
            "round_end_ms": round_end,
            "applications": applications,
            "releases": releases,
            "observations": observations,
            "backlog_drains": drained,
            "policy_id": policy_id,
            "policy_applied_tenants": applied_tenants,
        }


def adjust_round(store: Store, round_no: int) -> dict[str, Any]:
    with store.write_tx() as connection:
        adjust_watermark = store.meta_get_int(connection, "adjust_round_watermark")
        process_watermark = store.meta_get_int(connection, "process_round_watermark")
        if round_no <= adjust_watermark:
            return {
                "round_no": round_no,
                "status": "already_adjusted",
                "adjust_round_watermark": adjust_watermark,
            }
        if round_no > process_watermark:
            fail(
                "E_ROUND_NOT_PROCESSED",
                "cannot adjust a round that has not been processed",
                round_no=round_no,
                process_round_watermark=process_watermark,
            )
        if round_no != adjust_watermark + 1:
            fail(
                "E_ROUND_ORDER",
                "adjustments must be applied sequentially",
                expected=adjust_watermark + 1,
                actual=round_no,
            )
        return _adjust_round(connection, store, round_no)


def adjust_pending(store: Store) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    while True:
        with store.read_conn() as connection:
            process_watermark = store.meta_get_int(connection, "process_round_watermark")
            adjust_watermark = store.meta_get_int(connection, "adjust_round_watermark")
        if adjust_watermark >= process_watermark:
            break
        result = adjust_round(store, adjust_watermark + 1)
        results.append(result)
        _maybe_crash("adjust", int(result["round_no"]))
    return results


def run_pipeline(
    store: Store,
    *,
    until_round: int | None = None,
    no_adjust: bool = False,
) -> dict[str, Any]:
    with store.read_conn() as connection:
        target_round = store.meta_get_int(connection, "target_round")
        process_watermark = store.meta_get_int(connection, "process_round_watermark")
    final_round = target_round if until_round is None else min(until_round, target_round)
    adjusted = adjust_pending(store) if not no_adjust else []
    processed: list[dict[str, Any]] = []
    while True:
        with store.read_conn() as connection:
            process_watermark = store.meta_get_int(connection, "process_round_watermark")
            adjust_watermark = store.meta_get_int(connection, "adjust_round_watermark")
        if process_watermark >= final_round:
            break
        next_round = process_watermark + 1
        if not no_adjust and adjust_watermark < process_watermark:
            adjusted.append(adjust_round(store, adjust_watermark + 1))
            _maybe_crash("adjust", int(adjusted[-1]["round_no"]))
        result = process_round(store, next_round)
        processed.append(result)
        _maybe_crash("process", next_round)
        if not no_adjust and result.get("status") == "committed":
            adjustment = adjust_round(store, next_round)
            adjusted.append(adjustment)
            _maybe_crash("adjust", next_round)
    if not no_adjust:
        adjusted.extend(adjust_pending(store))
    with store.read_conn() as connection:
        final_process = store.meta_get_int(connection, "process_round_watermark")
        final_adjust = store.meta_get_int(connection, "adjust_round_watermark")
    return {
        "ok": True,
        "target_round": target_round,
        "final_round": final_round,
        "process_round_watermark": final_process,
        "adjust_round_watermark": final_adjust,
        "processed_rounds": [item["round_no"] for item in processed],
        "adjusted_rounds": [item["round_no"] for item in adjusted],
    }


def resume_pipeline(store: Store, *, until_round: int | None = None) -> dict[str, Any]:
    return run_pipeline(store, until_round=until_round, no_adjust=False)

