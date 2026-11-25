from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .aggregation import aggregate_trace
from .evaluation import compare_pair
from .metrics import decimal_truth_metric
from .storage import write_transaction
from .versions import metric_version, window_version


def _scope_rate(scenario: Any, scope_type: str, scope_id: str) -> Decimal:
    if scope_type == "service":
        return scenario.service(scope_id).expected_request_rate
    for service in scenario.services:
        for instance in service.instances:
            if instance.instance_id == scope_id:
                return instance.expected_request_rate
    raise KeyError(scope_id)


def full_verify(
    *,
    conn: Any,
    scenario: Any,
    records: list[Any],
    metrics_config: dict[str, Any],
    threshold_version: str = "unversioned",
    persist_sensitivity: bool = True,
) -> dict[str, Any]:
    origin = scenario.origin_logical_ms
    definition0 = next(iter(metrics_config["metrics"].values()))
    length = int(definition0["window_length_ms"])
    step = int(definition0["window_step_ms"])
    wv = window_version(metrics_config, origin)
    aggregates = aggregate_trace(
        records,
        origin_ms=origin,
        length_ms=length,
        step_ms=step,
        expected_window_count=metrics_config.get("window_count"),
    )

    rows = conn.execute(
        """
        SELECT mv.window_pk, mv.metric, mv.metric_version, mv.scale,
               mv.fixed_value, mv.truth_value, mv.absolute_deviation,
               mv.relative_deviation, mv.error_bound, mv.within_budget,
               mv.chain_json, mv.raw_value_json,
               w.scope_type, w.scope_id, w.window_index, w.summary_json
        FROM metric_values mv JOIN windows w ON w.window_pk=mv.window_pk
        WHERE w.window_version=?
        ORDER BY w.scope_id, w.window_index, mv.metric
        """,
        (wv,),
    ).fetchall()

    budget_violations: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    chains: list[dict[str, Any]] = []
    checked = 0
    current_versions = {
        name: metric_version(definition)
        for name, definition in metrics_config["metrics"].items()
    }

    for row in rows:
        if row["metric_version"] != current_versions.get(row["metric"]):
            continue
        scope_key = f"{row['scope_type']}:{row['scope_id']}"
        aggregate = aggregates[scope_key][int(row["window_index"])]
        expected_rate = _scope_rate(scenario, row["scope_type"], row["scope_id"])
        truth = decimal_truth_metric(
            row["metric"],
            metrics_config["metrics"][row["metric"]],
            aggregate,
            expected_rate,
        )
        stored_truth = Decimal(row["truth_value"])
        recomputed_abs = abs(Decimal(row["fixed_value"]) / Decimal(row["scale"]) - truth)
        bound = Decimal(row["error_bound"])
        truth_drift = abs(truth - stored_truth)
        ok = recomputed_abs <= bound + Decimal("1e-70") and truth_drift <= Decimal("1e-60")
        checked += 1

        item = {
            "scope_id": row["scope_id"],
            "scope_type": row["scope_type"],
            "window_index": row["window_index"],
            "metric": row["metric"],
            "metric_version": row["metric_version"],
            "fixed_value": row["fixed_value"],
            "fixed_decimal": format(Decimal(row["fixed_value"]) / Decimal(row["scale"]), "f"),
            "stored_truth_decimal": row["truth_value"],
            "recomputed_truth_decimal": format(truth, "f"),
            "absolute_deviation": format(recomputed_abs, "f"),
            "stored_absolute_deviation": row["absolute_deviation"],
            "relative_deviation": row["relative_deviation"],
            "error_bound": row["error_bound"],
            "truth_recompute_drift": format(truth_drift, "f"),
            "within_error_budget": ok and bool(row["within_budget"]),
            "request_ranges": aggregate.summary()["request_ranges"],
            "request_count": aggregate.request_count,
        }
        comparisons.append(item)
        if not ok or not row["within_budget"]:
            chain = json.loads(row["chain_json"])
            budget_violations.append(
                {
                    **item,
                    "error_code": "E_PRECISION_BUDGET",
                    "register_ids": chain.get("register_ids", []),
                }
            )

        chain_payload = json.loads(row["chain_json"])
        ledger_row = conn.execute(
            """
            SELECT carry_in_num, carry_in_den, carry_out_num, carry_out_den,
                   discarded_json, rounding_direction, registration_seq
            FROM remainder_ledger
            WHERE window_pk=? AND metric=? AND metric_version=?
            """,
            (row["window_pk"], row["metric"], row["metric_version"]),
        ).fetchone()
        ledger_payload = dict(ledger_row) if ledger_row is not None else None
        if ledger_payload is not None and ledger_payload.get("discarded_json"):
            ledger_payload["discarded"] = json.loads(ledger_payload.pop("discarded_json"))
        chains.append(
            {
                "scope_id": row["scope_id"],
                "window_index": row["window_index"],
                "metric": row["metric"],
                "scale": row["scale"],
                "request_ranges": aggregate.summary()["request_ranges"],
                "ledger": ledger_payload,
                "chain": chain_payload,
            }
        )

    sensitivity = _analyze_pairs(
        scenario=scenario,
        metrics_config=metrics_config,
        aggregates=aggregates,
        conn=conn,
        wv=wv,
        threshold_version=threshold_version,
        persist=persist_sensitivity,
    )

    return {
        "schema": "precision-verification/v1",
        "window_version": wv,
        "checked_metric_values": checked,
        "all_within_error_budget": not budget_violations,
        "budget_violations": budget_violations,
        "comparisons": comparisons,
        "error_source_chains": chains,
        "sensitivity": sensitivity,
    }


def _raw_for(
    metrics_config: dict[str, Any],
    metric: str,
    aggregate: Any,
    rate: Decimal,
) -> tuple[Any, Decimal]:
    from .metrics import raw_metric

    definition = metrics_config["metrics"][metric]
    raw = raw_metric(metric, definition, aggregate, rate).value
    truth = decimal_truth_metric(metric, definition, aggregate, rate)
    return raw, truth


def _analyze_pairs(
    *,
    scenario: Any,
    metrics_config: dict[str, Any],
    aggregates: dict[str, dict[int, Any]],
    conn: Any,
    wv: str,
    threshold_version: str,
    persist: bool,
) -> dict[str, Any]:
    tolerance = scenario.precision_tolerance
    pair_reports: list[dict[str, Any]] = []
    sensitive_count = 0
    flip_count = 0

    max_index = max(
        (index for buckets in aggregates.values() for index in buckets),
        default=-1,
    )

    for window_index in range(max_index + 1):
        for metric, definition in metrics_config["metrics"].items():
            mv = metric_version(definition)

            instance_pairs: list[tuple[str, str, str, str]] = []
            for service in scenario.services:
                ids = [instance.instance_id for instance in service.instances]
                for i in range(len(ids)):
                    for j in range(i + 1, len(ids)):
                        instance_pairs.append(
                            (ids[i], ids[j], "instance_pair", service.service_name)
                        )

            service_names = [service.service_name for service in scenario.services]
            scope_pairs = list(instance_pairs)
            for i in range(len(service_names)):
                for j in range(i + 1, len(service_names)):
                    scope_pairs.append(
                        (service_names[i], service_names[j], "service_pair", None)
                    )

            for scope_a, scope_b, relation_type, service_name in scope_pairs:
                key_a = f"{'service' if relation_type == 'service_pair' else 'instance'}:{scope_a}"
                key_b = f"{'service' if relation_type == 'service_pair' else 'instance'}:{scope_b}"
                if key_a not in aggregates or key_b not in aggregates:
                    continue
                if window_index not in aggregates[key_a] or window_index not in aggregates[key_b]:
                    continue
                agg_a = aggregates[key_a][window_index]
                agg_b = aggregates[key_b][window_index]
                rate_a = _scope_rate(
                    scenario,
                    "service" if relation_type == "service_pair" else "instance",
                    scope_a,
                )
                rate_b = _scope_rate(
                    scenario,
                    "service" if relation_type == "service_pair" else "instance",
                    scope_b,
                )
                raw_a, truth_a = _raw_for(metrics_config, metric, agg_a, rate_a)
                raw_b, truth_b = _raw_for(metrics_config, metric, agg_b, rate_b)
                report = compare_pair(
                    metric=metric,
                    definition=definition,
                    scope_a=scope_a,
                    scope_b=scope_b,
                    raw_a=raw_a,
                    raw_b=raw_b,
                    truth_a=truth_a,
                    truth_b=truth_b,
                    tolerance=tolerance,
                    relation_type=relation_type,
                )
                report["window_index"] = window_index
                report["window_version"] = wv
                pair_reports.append(report)
                if report["sensitive"]:
                    sensitive_count += 1
                if report["higher_scale_flip"]:
                    flip_count += 1

                if persist:
                    window_row = conn.execute(
                        "SELECT window_pk FROM windows WHERE scope_id=? AND window_index=? AND window_version=?",
                        (scope_a, window_index, wv),
                    ).fetchone()
                    if window_row is None:
                        continue
                    from .jsonutil import dumps

                    with write_transaction(conn):
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO sensitivity_pairs(
                                window_index, window_version, metric, metric_version,
                                threshold_version, scope_a, scope_b, relation_type,
                                sensitive, higher_scale_flip, payload_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                window_index,
                                wv,
                                metric,
                                mv,
                                threshold_version,
                                scope_a,
                                scope_b,
                                relation_type,
                                1 if report["sensitive"] else 0,
                                1 if report["higher_scale_flip"] else 0,
                                dumps(report),
                            ),
                        )

    sensitive_reports = [report for report in pair_reports if report["sensitive"]]
    return {
        "pair_count": len(pair_reports),
        "sensitive_count": sensitive_count,
        "higher_scale_flip_count": flip_count,
        "sensitive_pairs": sensitive_reports,
        "all_pairs": pair_reports,
    }
