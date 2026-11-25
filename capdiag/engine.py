from __future__ import annotations

import os
import signal
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .aggregation import aggregate_trace
from .errors import CapDiagError, ErrorCode
from .evaluation import evaluate_metric
from .metrics import calculate_fixed_metric, compare_values, decimal_truth_metric, raw_metric
from .storage import (
    commit_window_bundle,
    has_metric_value,
    has_window,
    previous_carry,
    stage_window_bundle,
    write_transaction,
)
from .thresholds import ThresholdCandidate, select_thresholds_at_clock
from .versions import metric_version, window_version


def _scope_expected_rate(scenario: Any, scope_type: str, scope_id: str) -> Decimal:
    if scope_type == "service":
        return scenario.service(scope_id).expected_request_rate
    for service in scenario.services:
        for instance in service.instances:
            if instance.instance_id == scope_id:
                return instance.expected_request_rate
    raise KeyError(scope_id)


def _service_name_for_scope(scenario: Any, scope_type: str, scope_id: str) -> str | None:
    if scope_type == "service":
        return scope_id
    return scenario.service_for_instance(scope_id)


def _fraction_from_stored(payload: Any) -> Fraction:
    return Fraction(int(payload["numerator"]), int(payload["denominator"]))


def run_service(
    *,
    conn: Any,
    scenario: Any,
    records: list[Any],
    metrics_config: dict[str, Any],
    threshold_candidates: list[ThresholdCandidate],
    threshold_version: str,
    service_name: str,
    crash_at: str | None = None,
    crash_window: int | None = None,
) -> dict[str, Any]:
    origin = int(scenario.origin_logical_ms)
    first_definition = next(iter(metrics_config["metrics"].values()))
    length_ms = int(first_definition["window_length_ms"])
    step_ms = int(first_definition["window_step_ms"])
    wv = window_version(metrics_config, origin)

    aggregates_by_key = aggregate_trace(
        records,
        origin_ms=origin,
        length_ms=length_ms,
        step_ms=step_ms,
        expected_window_count=metrics_config.get("window_count"),
        service_filter=service_name,
    )

    ordered_keys = sorted(
        aggregates_by_key,
        key=lambda key: (0 if key.startswith("service:") else 1, key),
    )
    report: dict[str, Any] = {
        "service_name": service_name,
        "window_version": wv,
        "scopes": [],
        "committed_windows": [],
        "skipped_windows": [],
        "budget_violations": [],
        "evaluations_inserted": 0,
    }

    for scope_key in ordered_keys:
        scope_type, scope_id = scope_key.split(":", 1)
        aggregates = aggregates_by_key[scope_key]
        scope_report = {"scope_type": scope_type, "scope_id": scope_id, "windows": []}

        for index in sorted(aggregates):
            aggregate = aggregates[index]
            window = aggregate.window
            window_pk = has_window(
                conn,
                scope_id=scope_id,
                window_index=index,
                window_version=wv,
            )

            metric_bundles: dict[str, Any] = {}
            ledgers: dict[str, Any] = {}
            for metric_name, definition in metrics_config["metrics"].items():
                mv = metric_version(definition)
                if window_pk is not None and has_metric_value(
                    conn, window_pk=window_pk, metric=metric_name, metric_version=mv
                ):
                    continue

                stored_carry = previous_carry(
                    conn,
                    scope_id=scope_id,
                    window_index=index,
                    window_version=wv,
                    metric=metric_name,
                    metric_version=mv,
                )
                carry_in = (
                    _fraction_from_stored(
                        {"numerator": stored_carry[0], "denominator": stored_carry[1]}
                    )
                    if stored_carry is not None
                    else Fraction(0, 1)
                )

                expected_rate = _scope_expected_rate(scenario, scope_type, scope_id)
                result = calculate_fixed_metric(
                    metric_name,
                    definition,
                    aggregate,
                    expected_rate,
                    carry_in,
                )
                truth = decimal_truth_metric(
                    metric_name, definition, aggregate, expected_rate
                )
                comparison = compare_values(
                    fixed_value=result.fixed_value,
                    scale=int(definition["scale"]),
                    truth=truth,
                    error_bound=result.error_bound,
                )
                if not comparison["within_error_budget"]:
                    report["budget_violations"].append(
                        {
                            "scope_id": scope_id,
                            "window_index": index,
                            "metric": metric_name,
                            "metric_version": mv,
                            "register_ids": result.chain.register_ids,
                            **comparison,
                        }
                    )

                direction = str(definition.get("rounding_direction", "round_half_up"))
                ledger = {
                    "carry_in_num": carry_in.numerator,
                    "carry_in_den": carry_in.denominator,
                    "carry_out_num": result.carry_out.numerator,
                    "carry_out_den": result.carry_out.denominator,
                    "discarded": {
                        "numerator": result.discarded.numerator,
                        "denominator": result.discarded.denominator,
                    },
                    "rounding_direction": result.operations[-1].get(
                        "rounding_direction", direction
                    ),
                    "registration_seq": result.operations[-1]["registration_seq"],
                }
                metric_bundles[metric_name] = {
                    "metric_version": mv,
                    "scale": definition["scale"],
                    "fixed_value": result.fixed_value,
                    "raw_value": result.raw_value,
                    "truth_value": comparison["truth_decimal"],
                    "absolute_deviation": comparison["absolute_deviation"],
                    "relative_deviation": comparison["relative_deviation"],
                    "error_bound": comparison["error_bound"],
                    "within_budget": comparison["within_error_budget"],
                    "chain": {
                        "metric": metric_name,
                        "scale": definition["scale"],
                        "register_ids": result.chain.register_ids,
                        "operations": result.operations,
                    },
                    "ledger": ledger,
                }
                ledgers[metric_name] = ledger

                if (
                    crash_at == "conversion"
                    and crash_window == index
                    and scope_type == "service"
                ):
                    os.kill(os.getpid(), signal.SIGKILL)

            summary = aggregate.summary()
            assignment_hash_source = sorted(aggregate.assignment_keys)
            from .jsonutil import stable_hash

            assignment_hash = stable_hash(assignment_hash_source)
            bundle = {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "window_index": index,
                "window_version": wv,
                "start_ms": window.start_ms,
                "end_ms": window.end_ms,
                "step_ms": window.step_ms,
                "watermark_ms": window.end_ms,
                "max_event_clock_ms": aggregate.max_event_clock_ms,
                "request_count": aggregate.request_count,
                "success_count": aggregate.success_count,
                "dropped_count": aggregate.dropped_count,
                "summary": summary,
                "assignment_hash": assignment_hash,
                "metrics": metric_bundles,
                "ledgers": ledgers,
                "evaluations": [],
            }

            # Evaluate newly computed metric bundles in the same final transaction.
            view = select_thresholds_at_clock(
                threshold_candidates, window.end_ms
            )
            service_for_scope = _service_name_for_scope(
                scenario, scope_type, scope_id
            )
            for metric_name, metric_bundle in metric_bundles.items():
                definition = metrics_config["metrics"][metric_name]
                raw_value = _fraction_from_stored(metric_bundle["raw_value"]) if isinstance(metric_bundle["raw_value"], dict) else metric_bundle["raw_value"]
                evaluation = evaluate_metric(
                    metric=metric_name,
                    definition=definition,
                    raw_value=raw_value,
                    truth_value=Decimal(metric_bundle["truth_value"]),
                    fixed_value=metric_bundle["fixed_value"],
                    error_bound=Decimal(metric_bundle["error_bound"]),
                    threshold_view=view,
                    scope_id=scope_id,
                    service_name=service_for_scope,
                    metric_version=metric_bundle["metric_version"],
                    threshold_version=threshold_version,
                )
                if evaluation is not None:
                    bundle["evaluations"].append(evaluation)

            if window_pk is None or metric_bundles:
                prep_id = stage_window_bundle(conn, bundle)
                if (
                    crash_at == "stage"
                    and crash_window == index
                    and scope_type == "service"
                ):
                    os.kill(os.getpid(), signal.SIGKILL)
                committed_pk = commit_window_bundle(conn, prep_id)
                report["committed_windows"].append(
                    {"scope_id": scope_id, "window_index": index, "window_pk": committed_pk}
                )
                scope_report["windows"].append(
                    {"index": index, "status": "committed", "metrics": list(metric_bundles)}
                )
            else:
                report["skipped_windows"].append(
                    {"scope_id": scope_id, "window_index": index, "window_pk": window_pk}
                )
                scope_report["windows"].append({"index": index, "status": "skipped_committed"})

            inserted = ensure_evaluations_for_window(
                conn=conn,
                scenario=scenario,
                metrics_config=metrics_config,
                aggregate=aggregate,
                window_pk=window_pk
                or has_window(
                    conn,
                    scope_id=scope_id,
                    window_index=index,
                    window_version=wv,
                ),
                threshold_candidates=threshold_candidates,
                threshold_version=threshold_version,
            )
            report["evaluations_inserted"] += inserted

        report["scopes"].append(scope_report)

    return report


def ensure_evaluations_for_window(
    *,
    conn: Any,
    scenario: Any,
    metrics_config: dict[str, Any],
    aggregate: Any,
    window_pk: int | None,
    threshold_candidates: list[ThresholdCandidate],
    threshold_version: str,
) -> int:
    if window_pk is None:
        return 0
    scope = aggregate.window.scope_id
    scope_type = aggregate.window.scope_type
    expected_rate = _scope_expected_rate(scenario, scope_type, scope)
    view = select_thresholds_at_clock(threshold_candidates, aggregate.window.end_ms)
    inserted = 0

    for metric_name, definition in metrics_config["metrics"].items():
        mv = metric_version(definition)
        existing = conn.execute(
            "SELECT 1 FROM evaluations WHERE window_pk=? AND metric=? AND metric_version=? AND threshold_version=?",
            (window_pk, metric_name, mv, threshold_version),
        ).fetchone()
        if existing is not None:
            continue
        value_row = conn.execute(
            "SELECT fixed_value, error_bound FROM metric_values WHERE window_pk=? AND metric=? AND metric_version=?",
            (window_pk, metric_name, mv),
        ).fetchone()
        if value_row is None:
            continue
        truth = decimal_truth_metric(metric_name, definition, aggregate, expected_rate)
        raw = raw_metric(metric_name, definition, aggregate, expected_rate).value
        evaluation = evaluate_metric(
            metric=metric_name,
            definition=definition,
            raw_value=raw,
            truth_value=truth,
            fixed_value=int(value_row["fixed_value"]),
            error_bound=Decimal(value_row["error_bound"]),
            threshold_view=view,
            scope_id=scope,
            service_name=_service_name_for_scope(scenario, scope_type, scope),
            metric_version=mv,
            threshold_version=threshold_version,
        )
        if evaluation is None:
            continue
        with write_transaction(conn):
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO evaluations(
                    window_pk, metric, metric_version, threshold_version,
                    scope_id, verdict, safe_verdict, precision_sensitive,
                    higher_scale_flip, reused, source_threshold_version, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (
                    window_pk,
                    metric_name,
                    mv,
                    threshold_version,
                    scope,
                    evaluation["verdict"],
                    evaluation["safe_verdict"],
                    1 if evaluation["precision_sensitive"] else 0,
                    1 if evaluation["higher_scale_flip"] else 0,
                    __import__("capdiag.jsonutil", fromlist=["dumps"]).dumps(evaluation),
                ),
            )
            inserted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    return inserted
