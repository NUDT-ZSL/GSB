from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from typing import Any

from . import __version__
from .ledger import LedgerBuilder, scaled_tolerance
from .scene import BatchPlan, PrecisionConfig, Scene, parse_scene
from .stats import decide_gradient
from .store import Store
from .util import decimal_from_fraction, fraction_dict, sha256_json


HIGHER_SCALE_FACTOR = 100


def build_report(store: Store) -> dict[str, Any]:
    context = store.report_context()
    meta = context["meta"]
    scene = parse_scene(json.loads(meta["scene_json"]))
    return analyze_context(scene, context)


def analyze_context(scene: Scene, context: dict[str, Any]) -> dict[str, Any]:
    meta = context["meta"]
    units = [dict(item) for item in context["units"]]
    windows = [dict(item) for item in context["windows"]]
    ledger_rows = [dict(item) for item in context["ledger"]]
    errors = [dict(item) for item in context["errors"]]

    units_by_key = {str(item["unit_key"]): item for item in units}
    windows_by_unit: dict[str, list[dict[str, Any]]] = {}
    parsed_payloads: dict[str, dict[str, Any]] = {}
    parsed_summaries: dict[str, dict[str, Any]] = {}
    for row in windows:
        unit_key = str(row["unit_key"])
        windows_by_unit.setdefault(unit_key, []).append(row)
        parsed_payloads[str(row["window_key"])] = json.loads(row["payload_json"])
        parsed_summaries[str(row["window_key"])] = json.loads(row["summary_json"])
    for unit_windows in windows_by_unit.values():
        unit_windows.sort(key=lambda item: (int(item["window_index"]), int(item["version"])))

    ledger_by_scope: dict[str, list[dict[str, Any]]] = {}
    for row in ledger_rows:
        ledger_by_scope.setdefault(str(row["scope_key"]), []).append(row)
    for scoped in ledger_by_scope.values():
        scoped.sort(key=lambda item: (int(item["local_seq"]), int(item["seq"])))
    semantic_ledger = sorted(
        ledger_rows,
        key=lambda item: (str(item["scope_key"]), int(item["local_seq"])),
    )
    actual_to_registration = {
        int(row["seq"]): index + 1
        for index, row in enumerate(semantic_ledger)
    }

    high_precision = replace(
        scene.precision,
        latency_scale=scene.precision.latency_scale * HIGHER_SCALE_FACTOR,
        percent_scale=scene.precision.percent_scale * HIGHER_SCALE_FACTOR,
    )

    batches_by_id = {batch.batch_id: batch for batch in scene.batches}
    unit_analysis: dict[str, dict[str, Any]] = {}
    for unit in units:
        unit_key = str(unit["unit_key"])
        unit_windows = windows_by_unit.get(unit_key, [])
        payloads = [parsed_payloads[str(row["window_key"])] for row in unit_windows]
        success_ledger_seq = None
        p95_ledger_seq = None
        if unit["status"] == "COMPLETED" and unit.get("decision_json"):
            decision_payload = json.loads(unit["decision_json"])
            metric = decision_payload["metric"]
            decision = decision_payload["decision"]
            success_ledger_seq = decision_payload.get("success_ledger_seq")
            p95_ledger_seq = decision_payload.get("p95_ledger_seq")
        else:
            metric = None
            decision = None
        high_metric, high_decision = _high_scale_unit(scene, unit, payloads, high_precision)
        unit_analysis[unit_key] = {
            "unit": unit,
            "windows": unit_windows,
            "payloads": payloads,
            "metric": metric,
            "decision": decision,
            "high_metric": high_metric,
            "high_decision": high_decision,
            "success_ledger_seq": success_ledger_seq,
            "p95_ledger_seq": p95_ledger_seq,
            "status": _unit_decision_status(unit, decision),
            "high_status": _unit_decision_status(unit, high_decision),
        }

    gradient_rows: list[dict[str, Any]] = []
    conclusions: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    for batch in sorted(scene.batches, key=lambda item: item.batch_id):
        batch_units = sorted(
            [unit for unit in units if str(unit["batch_id"]) == batch.batch_id],
            key=lambda item: (int(item["gradient_index"]), str(item["unit_key"])),
        )
        batch_analysis = _analyze_batch(
            scene,
            batch,
            batch_units,
            unit_analysis,
            ledger_by_scope,
            actual_to_registration,
        )
        conclusions.append(batch_analysis["conclusion"])
        gradient_rows.extend(batch_analysis["gradients"])
        marks.extend(batch_analysis["marks"])

    marks.extend(
        _tenant_marks(
            scene,
            units,
            unit_analysis,
            conclusions,
            ledger_by_scope,
            actual_to_registration,
        )
    )
    marks = sorted(marks, key=lambda item: item["mark_key"])

    window_rows = []
    for row in sorted(
        windows,
        key=lambda item: (
            str(item["batch_id"]),
            str(item["tenant"]),
            int(item["gradient_index"]),
            int(item["window_index"]),
            int(item["version"]),
        ),
    ):
        summary = parsed_summaries[str(row["window_key"])]
        window_ledger_rows = ledger_by_scope.get(str(row["window_key"]), [])
        window_registration_seqs = [
            actual_to_registration[int(item["seq"])]
            for item in window_ledger_rows
            if int(item["seq"]) in actual_to_registration
        ]
        window_rows.append(
            {
                "window_key": row["window_key"],
                "version": int(row["version"]),
                "unit_key": row["unit_key"],
                "batch_id": row["batch_id"],
                "service": row["service"],
                "tenant": row["tenant"],
                "gradient_index": int(row["gradient_index"]),
                "window_index": int(row["window_index"]),
                "start_ms": int(row["start_ms"]),
                "end_ms": int(row["end_ms"]),
                "request_count": int(row["request_count"]),
                "success_count": int(row["success_count"]),
                "failure_count": int(row["failure_count"]),
                "success_rate_scaled": int(row["success_rate_scaled"]),
                "success_rate_scale": int(summary["success_rate_scale"]),
                "p95_latency_scaled": row["p95_latency_scaled"],
                "p95_latency_scale": int(summary["p95_latency_scale"]),
                "success_rate_error_bound": summary["success_rate_error_bound"],
                "p95_latency_error_bound": summary["p95_latency_error_bound"],
                "within_error_budget": bool(summary["within_error_budget"]),
                "errors_by_code": summary["errors_by_code"],
                "ledger_seq_start": min(window_registration_seqs) if window_registration_seqs else None,
                "ledger_seq_end": max(window_registration_seqs) if window_registration_seqs else None,
                "ledger_refs": [
                    {
                        "local_seq": int(item["local_seq"]),
                        "seq": actual_to_registration[int(item["seq"])],
                    }
                    for item in window_ledger_rows
                ],
                "payload_hash": row["payload_hash"],
            }
        )

    ledger_output = [
        _ledger_row_output(row, actual_to_registration[int(row["seq"])])
        for row in semantic_ledger
    ]
    error_output = [
        {
            "sequence": index + 1,
            "window_key": row["window_key"],
            "unit_key": row["unit_key"],
            "request_id": row["request_id"],
            "code": row["code"],
            "message": row["message"],
            "fields": json.loads(row["fields_json"]),
        }
        for index, row in enumerate(
            sorted(
                errors,
                key=lambda item: (
                    str(item["window_key"]),
                    str(item["request_id"]),
                    str(item["code"]),
                    int(item["id"]),
                ),
            )
        )
    ]
    abandoned = [
        {
            "unit_key": row["unit_key"],
            "batch_id": row["batch_id"],
            "tenant": row["tenant"],
            "gradient_index": int(row["gradient_index"]),
            "concurrency": int(row["concurrency"]),
            "status": row["status"],
            "reason": row["reason"],
        }
        for row in units
        if row["status"] in {"ABANDONED", "SKIPPED_EARLY_TERMINATION"}
    ]
    abandoned.sort(key=lambda item: (item["batch_id"], item["gradient_index"], item["unit_key"]))

    status_counts: dict[str, int] = {}
    for row in units:
        status_counts[str(row["status"])] = status_counts.get(str(row["status"]), 0) + 1
    all_terminal = all(str(row["status"]) in {"COMPLETED", "ABANDONED", "SKIPPED_EARLY_TERMINATION"} for row in units)
    report = {
        "schema_version": int(meta.get("schema_version", "0")),
        "engine_version": __version__,
        "scene": {
            "scene_id": scene.scene_id,
            "sha256": scene.sha256,
            "latency_scale": scene.precision.latency_scale,
            "percent_scale": scene.precision.percent_scale,
            "compare_tolerance": {
                "success_rate": _decimal_text(scene.precision.compare_tolerance.success_rate),
                "p95_latency_ms": _decimal_text(scene.precision.compare_tolerance.p95_latency_ms),
            },
            "higher_scale_factor": HIGHER_SCALE_FACTOR,
        },
        "budget": {
            "global_logical_time_ms": int(meta["global_budget_ms"]),
            "budget_used_ms": int(meta["budget_used_ms"]),
            "logical_clock_ms": int(meta["logical_clock_ms"]),
            "logical_clock_high_watermark_ms": int(meta["logical_clock_high_watermark_ms"]),
        },
        "analysis_complete": all_terminal,
        "unit_status_counts": dict(sorted(status_counts.items())),
        "capacity_conclusions": conclusions,
        "gradients": sorted(
            gradient_rows,
            key=lambda item: (item["batch_id"], item["gradient_index"], item["unit_key"]),
        ),
        "windows": window_rows,
        "precision_sensitive": marks,
        "abandoned_units": abandoned,
        "request_errors": sorted(error_output, key=lambda item: item["sequence"]),
        "ledger_entries": ledger_output,
    }
    report["report_sha256"] = sha256_json(report)
    return report


def persist_analysis(store: Store, report: dict[str, Any]) -> None:
    if not report.get("analysis_complete"):
        return
    marks_by_batch: dict[str, list[dict[str, Any]]] = {}
    for mark in report["precision_sensitive"]:
        marks_by_batch.setdefault(mark["batch_id"], []).append(mark)
    for conclusion in report["capacity_conclusions"]:
        store.persist_batch_analysis(
            conclusion["batch_id"],
            conclusion,
            marks_by_batch.get(conclusion["batch_id"], []),
        )


def _analyze_batch(
    scene: Scene,
    batch: BatchPlan,
    units: list[dict[str, Any]],
    unit_analysis: dict[str, dict[str, Any]],
    ledger_by_scope: dict[str, list[dict[str, Any]]],
    actual_to_registration: dict[int, int],
) -> dict[str, Any]:
    tenant = scene.tenants[batch.tenant]
    gradients: list[dict[str, Any]] = []
    for unit in units:
        analysis = unit_analysis[str(unit["unit_key"])]
        metric = analysis["metric"]
        decision = analysis["decision"]
        high_metric = analysis["high_metric"]
        high_decision = analysis["high_decision"]
        status = analysis["status"]
        window_entries = []
        for window in analysis["windows"]:
            window_ledger_rows = ledger_by_scope.get(str(window["window_key"]), [])
            registration_seqs = [
                actual_to_registration[int(item["seq"])]
                for item in window_ledger_rows
                if int(item["seq"]) in actual_to_registration
            ]
            window_entries.append(
                {
                    "window_key": window["window_key"],
                    "window_index": int(window["window_index"]),
                    "start_ms": int(window["start_ms"]),
                    "end_ms": int(window["end_ms"]),
                    "ledger_seq_start": min(registration_seqs) if registration_seqs else None,
                    "ledger_seq_end": max(registration_seqs) if registration_seqs else None,
                    "request_count": int(window["request_count"]),
                    "success_count": int(window["success_count"]),
                    "success_rate_scaled": int(window["success_rate_scaled"]),
                    "p95_latency_scaled": window["p95_latency_scaled"],
                }
            )
        gradients.append(
            {
                "unit_key": unit["unit_key"],
                "batch_id": batch.batch_id,
                "service": batch.service,
                "tenant": batch.tenant,
                "gradient_index": int(unit["gradient_index"]),
                "concurrency": int(unit["concurrency"]),
                "unit_status": unit["status"],
                "status": status,
                "reason": unit.get("reason"),
                "metric": metric,
                "decision": decision,
                "high_scale_metric": high_metric,
                "high_scale_decision": high_decision,
                "high_scale_status": analysis["high_status"],
                "success_ledger_seq": (
                    actual_to_registration.get(int(analysis["success_ledger_seq"]))
                    if analysis["success_ledger_seq"] is not None
                    else None
                ),
                "p95_ledger_seq": (
                    actual_to_registration.get(int(analysis["p95_ledger_seq"]))
                    if analysis["p95_ledger_seq"] is not None
                    else None
                ),
                "success_ledger_local_seq": (
                    metric.get("success_ledger_local_seq") if metric is not None else None
                ),
                "p95_ledger_local_seq": (
                    metric.get("p95_ledger_local_seq") if metric is not None else None
                ),
                "window_count": len(window_entries),
                "windows": window_entries,
            }
        )
    conclusion, high_conclusion = _capacity_conclusion(batch, gradients)
    marks = _gradient_marks(
        scene,
        batch,
        gradients,
        conclusion,
        high_conclusion,
        ledger_by_scope,
        actual_to_registration,
    )
    conclusion["high_scale"] = high_conclusion
    return {"conclusion": conclusion, "gradients": gradients, "marks": marks}


def _capacity_conclusion(batch: BatchPlan, gradients: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    def evaluate(entries: list[dict[str, Any]], status_key: str) -> dict[str, Any]:
        last_capacity: int | None = None
        inflection: int | None = None
        incomplete = False
        for entry in entries:
            status = entry[status_key]
            if status in {"PASS", "DEGRADED_PASS"}:
                last_capacity = int(entry["concurrency"])
            elif status == "ABORT":
                if inflection is None:
                    inflection = int(entry["concurrency"])
            elif status in {"SKIPPED_EARLY_TERMINATION", "ABANDONED", "PENDING", "RUNNING"}:
                if inflection is None:
                    incomplete = True
        if inflection is not None:
            status = "INFLECTION_FOUND"
            capacity = last_capacity if last_capacity is not None else 0
        elif incomplete:
            status = "INCOMPLETE"
            capacity = last_capacity
        elif all(entry[status_key] in {"PASS", "DEGRADED_PASS"} for entry in entries):
            status = "NO_INFLECTION"
            capacity = last_capacity
        else:
            status = "INCOMPLETE"
            capacity = last_capacity
        return {
            "batch_id": batch.batch_id,
            "service": batch.service,
            "tenant": batch.tenant,
            "status": status,
            "capacity_concurrency": capacity,
            "inflection_concurrency": inflection,
            "allow_early_termination": batch.allow_early_termination,
            "tested_gradients": [
                {
                    "gradient_index": int(entry["gradient_index"]),
                    "concurrency": int(entry["concurrency"]),
                    "status": entry[status_key],
                }
                for entry in entries
            ],
        }

    original = evaluate(gradients, "status")
    high = evaluate(gradients, "high_scale_status")
    return original, high


def _gradient_marks(
    scene: Scene,
    batch: BatchPlan,
    gradients: list[dict[str, Any]],
    original_conclusion: dict[str, Any],
    high_conclusion: dict[str, Any],
    ledger_by_scope: dict[str, list[dict[str, Any]]],
    actual_to_registration: dict[int, int],
) -> list[dict[str, Any]]:
    marks: list[dict[str, Any]] = []
    completed = [entry for entry in gradients if entry["metric"] is not None and entry["decision"] is not None]
    for left, right in zip(completed, completed[1:]):
        for metric_name in ("success_rate", "p95_latency"):
            mark = _make_mark(
                scene,
                kind="gradient",
                batch=batch,
                left=left,
                right=right,
                metric_name=metric_name,
                original_conclusion=original_conclusion,
                high_conclusion=high_conclusion,
                ledger_by_scope=ledger_by_scope,
                actual_to_registration=actual_to_registration,
                left_analysis=None,
                right_analysis=None,
            )
            if mark is not None:
                marks.append(mark)
    return marks


def _make_mark(
    scene: Scene,
    kind: str,
    batch: BatchPlan,
    left: dict[str, Any],
    right: dict[str, Any],
    metric_name: str,
    original_conclusion: dict[str, Any],
    high_conclusion: dict[str, Any],
    ledger_by_scope: dict[str, list[dict[str, Any]]],
    actual_to_registration: dict[int, int],
    left_analysis: dict[str, Any] | None,
    right_analysis: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if metric_name == "success_rate":
        scale = int(left["metric"]["success_rate_scale"])
        high_scale = int(left["high_scale_metric"]["success_rate_scale"]) if left.get("high_scale_metric") else scale
        tolerance_scaled = scaled_tolerance(scene.precision.compare_tolerance.success_rate, scale)
        tolerance_value = scene.precision.compare_tolerance.success_rate
        left_value = int(left["metric"]["success_rate_scaled"])
        right_value = int(right["metric"]["success_rate_scaled"])
        high_left_value = (
            int(left["high_scale_metric"]["success_rate_scaled"]) if left.get("high_scale_metric") else None
        )
        high_right_value = (
            int(right["high_scale_metric"]["success_rate_scaled"]) if right.get("high_scale_metric") else None
        )
        left_outcome = _metric_outcome(left["decision"], "success")
        right_outcome = _metric_outcome(right["decision"], "success")
        high_left_outcome = _metric_outcome(left.get("high_scale_decision"), "success")
        high_right_outcome = _metric_outcome(right.get("high_scale_decision"), "success")
        left_bound = _fraction_from_dict(left["metric"]["success_rate_error_bound"])
        right_bound = _fraction_from_dict(right["metric"]["success_rate_error_bound"])
        unit_ledger_field = "success_ledger_seq"
        unit_ledger_local_field = "success_ledger_local_seq"
        window_kind = "window_success_rate"
    else:
        if left["metric"].get("p95_latency_scaled") is None or right["metric"].get("p95_latency_scaled") is None:
            return None
        if left.get("high_scale_metric", {}).get("p95_latency_scaled") is None:
            return None
        scale = int(left["metric"]["p95_latency_scale"])
        high_scale = int(left["high_scale_metric"]["p95_latency_scale"])
        tolerance_scaled = scaled_tolerance(scene.precision.compare_tolerance.p95_latency_ms, scale)
        tolerance_value = scene.precision.compare_tolerance.p95_latency_ms
        left_value = int(left["metric"]["p95_latency_scaled"])
        right_value = int(right["metric"]["p95_latency_scaled"])
        high_left_value = int(left["high_scale_metric"]["p95_latency_scaled"])
        high_right_value = int(right["high_scale_metric"]["p95_latency_scaled"])
        left_outcome = _metric_outcome(left["decision"], "p95")
        right_outcome = _metric_outcome(right["decision"], "p95")
        high_left_outcome = _metric_outcome(left.get("high_scale_decision"), "p95")
        high_right_outcome = _metric_outcome(right.get("high_scale_decision"), "p95")
        left_bound = _fraction_from_dict(left["metric"]["p95_latency_error_bound"])
        right_bound = _fraction_from_dict(right["metric"]["p95_latency_error_bound"])
        unit_ledger_field = "p95_ledger_seq"
        unit_ledger_local_field = "p95_ledger_local_seq"
        window_kind = "window_p95_latency"

    difference = abs(left_value - right_value)
    if tolerance_scaled == 0:
        if difference != 0:
            return None
    elif difference >= tolerance_scaled:
        return None
    metric_depends = (
        left_outcome != right_outcome
        or high_left_outcome != left_outcome
        or high_right_outcome != right_outcome
    )
    if not metric_depends:
        return None
    original_capacity_key = (
        original_conclusion.get("status"),
        original_conclusion.get("capacity_concurrency"),
        original_conclusion.get("inflection_concurrency"),
    )
    high_capacity_key = (
        high_conclusion.get("status"),
        high_conclusion.get("capacity_concurrency"),
        high_conclusion.get("inflection_concurrency"),
    )
    capacity_flip = original_capacity_key != high_capacity_key
    pair_status_flip = (
        left.get("status") != left.get("high_scale_status")
        or right.get("status") != right.get("high_scale_status")
    )
    higher_scale_flips = (
        (high_left_outcome, high_right_outcome) != (left_outcome, right_outcome)
        or pair_status_flip
        or capacity_flip
    )
    if not (higher_scale_flips or left_outcome != right_outcome):
        return None
    left_unit_key = str(left["unit_key"])
    right_unit_key = str(right["unit_key"])
    high_left_real = _scaled_real_text(high_left_value, high_scale) if high_left_value is not None else None
    high_right_real = _scaled_real_text(high_right_value, high_scale) if high_right_value is not None else None
    mark_key = f"{kind}|{left_unit_key}|{right_unit_key}|{metric_name}"
    left_registration_seq = left.get(unit_ledger_field)
    right_registration_seq = right.get(unit_ledger_field)
    mark = {
        "mark_key": mark_key,
        "kind": kind,
        "batch_id": batch.batch_id,
        "metric": metric_name,
        "left_unit_key": left_unit_key,
        "right_unit_key": right_unit_key,
        "left_batch_id": left["batch_id"],
        "right_batch_id": right["batch_id"],
        "left_tenant": left["tenant"],
        "right_tenant": right["tenant"],
        "left_concurrency": int(left["concurrency"]),
        "right_concurrency": int(right["concurrency"]),
        "left_value_scaled": left_value,
        "right_value_scaled": right_value,
        "left_value": _scaled_real_text(left_value, scale),
        "right_value": _scaled_real_text(right_value, scale),
        "difference_scaled": difference,
        "difference": _scaled_real_text(difference, scale),
        "tolerance_scaled": tolerance_scaled,
        "tolerance": _decimal_text(tolerance_value),
        "left_status": left["status"],
        "right_status": right["status"],
        "left_metric_outcome": left_outcome,
        "right_metric_outcome": right_outcome,
        "left_error_bound_scaled": fraction_dict(left_bound),
        "right_error_bound_scaled": fraction_dict(right_bound),
        "difference_error_bound_scaled": fraction_dict(left_bound + right_bound),
        "left_metric_ledger_seq": left_registration_seq,
        "right_metric_ledger_seq": right_registration_seq,
        "left_metric_ledger_ref": _ledger_ref(
            f"gradient:{left_unit_key}",
            left.get("metric", {}).get(unit_ledger_local_field),
            left_registration_seq,
        ),
        "right_metric_ledger_ref": _ledger_ref(
            f"gradient:{right_unit_key}",
            right.get("metric", {}).get(unit_ledger_local_field),
            right_registration_seq,
        ),
        "left_windows": _window_ledger_output(
            left.get("windows", []),
            ledger_by_scope,
            window_kind,
            actual_to_registration,
        ),
        "right_windows": _window_ledger_output(
            right.get("windows", []),
            ledger_by_scope,
            window_kind,
            actual_to_registration,
        ),
        "higher_scale": {
            "latency_scale": int(scene.precision.latency_scale * HIGHER_SCALE_FACTOR),
            "percent_scale": int(scene.precision.percent_scale * HIGHER_SCALE_FACTOR),
            "left_value_scaled": high_left_value,
            "right_value_scaled": high_right_value,
            "left_value": high_left_real,
            "right_value": high_right_real,
            "left_metric_outcome": high_left_outcome,
            "right_metric_outcome": high_right_outcome,
            "left_status": left.get("high_scale_status"),
            "right_status": right.get("high_scale_status"),
            "capacity_status": high_conclusion.get("status"),
            "capacity_concurrency": high_conclusion.get("capacity_concurrency"),
            "inflection_concurrency": high_conclusion.get("inflection_concurrency"),
        },
        "original_capacity": {
            "status": original_conclusion.get("status"),
            "capacity_concurrency": original_conclusion.get("capacity_concurrency"),
            "inflection_concurrency": original_conclusion.get("inflection_concurrency"),
        },
        "higher_scale_flips": higher_scale_flips,
        "capacity_conclusion_flips": capacity_flip,
        "depends_on_difference": True,
    }
    if left_analysis is not None and right_analysis is not None:
        mark["left_window_count"] = len(left_analysis.get("windows", []))
        mark["right_window_count"] = len(right_analysis.get("windows", []))
    return mark


def _tenant_marks(
    scene: Scene,
    units: list[dict[str, Any]],
    unit_analysis: dict[str, dict[str, Any]],
    conclusions: list[dict[str, Any]],
    ledger_by_scope: dict[str, list[dict[str, Any]]],
    actual_to_registration: dict[int, int],
) -> list[dict[str, Any]]:
    conclusions_by_batch = {str(item["batch_id"]): item for item in conclusions}
    gradients_present = {
        (str(unit["service"]), int(unit["gradient_index"]), str(unit["tenant"])): str(unit["unit_key"])
        for unit in units
        if str(unit["status"]) == "COMPLETED"
    }
    grouped: dict[tuple[str, int], list[str]] = {}
    for service, gradient_index, tenant in sorted(gradients_present):
        grouped.setdefault((service, gradient_index), []).append(tenant)
    marks: list[dict[str, Any]] = []
    for (service, gradient_index), tenants in sorted(grouped.items()):
        if len(tenants) < 2:
            continue
        for left_tenant, right_tenant in zip(tenants, tenants[1:]):
            left_unit_key = gradients_present[(service, gradient_index, left_tenant)]
            right_unit_key = gradients_present[(service, gradient_index, right_tenant)]
            left_analysis = unit_analysis[left_unit_key]
            right_analysis = unit_analysis[right_unit_key]
            left = _gradient_view(left_analysis, service, actual_to_registration)
            right = _gradient_view(right_analysis, service, actual_to_registration)
            left_conclusion = conclusions_by_batch.get(str(left["batch_id"]))
            right_conclusion = conclusions_by_batch.get(str(right["batch_id"]))
            if left_conclusion is None or right_conclusion is None:
                continue
            for metric_name in ("success_rate", "p95_latency"):
                mark = _make_mark(
                    scene,
                    kind="tenant",
                    batch=next(batch for batch in scene.batches if batch.batch_id == left["batch_id"]),
                    left=left,
                    right=right,
                    metric_name=metric_name,
                    original_conclusion=left_conclusion,
                    high_conclusion=left_conclusion["high_scale"],
                    ledger_by_scope=ledger_by_scope,
                    actual_to_registration=actual_to_registration,
                    left_analysis=left_analysis,
                    right_analysis=right_analysis,
                )
                if mark is not None:
                    right_capacity_key = (
                        right_conclusion["status"],
                        right_conclusion["capacity_concurrency"],
                        right_conclusion["inflection_concurrency"],
                    )
                    right_high_key = (
                        right_conclusion["high_scale"]["status"],
                        right_conclusion["high_scale"]["capacity_concurrency"],
                        right_conclusion["high_scale"]["inflection_concurrency"],
                    )
                    mark["right_original_capacity"] = {
                        "status": right_capacity_key[0],
                        "capacity_concurrency": right_capacity_key[1],
                        "inflection_concurrency": right_capacity_key[2],
                    }
                    mark["right_higher_scale_capacity"] = right_conclusion["high_scale"]
                    mark["capacity_conclusion_flips"] = bool(
                        mark["capacity_conclusion_flips"] or right_capacity_key != right_high_key
                    )
                    mark["higher_scale_flips"] = bool(
                        mark["higher_scale_flips"] or right_capacity_key != right_high_key
                    )
                    marks.append(mark)
    return marks


def _high_scale_unit(
    scene: Scene,
    unit: dict[str, Any],
    payloads: list[dict[str, Any]],
    high_precision: PrecisionConfig,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if unit["status"] != "COMPLETED" or not payloads:
        return None, None
    observations = [
        observation
        for payload in payloads
        for observation in payload.get("observations", [])
    ]
    ledger = LedgerBuilder(f"high-scale:{unit['unit_key']}")
    request_count = len(observations)
    success_count = sum(1 for item in observations if item.get("success"))
    latency_samples = [
        (str(item["request_id"]), Decimal(str(item["latency_ms"])))
        for item in observations
        if item.get("latency_ms") is not None
    ]
    ledger.add_exact_count("high_request_count", request_count, str(unit["unit_key"]))
    ledger.add_exact_count("high_success_count", success_count, str(unit["unit_key"]))
    success_entry = ledger.add_ratio(
        "high_success_rate",
        success_count,
        request_count,
        high_precision.percent_scale,
        str(unit["unit_key"]),
    )
    p95_entry = ledger.add_p95(
        "high_p95_latency",
        latency_samples,
        high_precision.latency_scale,
        str(unit["unit_key"]),
    )
    metric = {
        "unit_key": str(unit["unit_key"]),
        "request_count": request_count,
        "success_count": success_count,
        "success_rate_scaled": success_entry.result_scaled,
        "success_rate_scale": high_precision.percent_scale,
        "p95_latency_scaled": p95_entry.result_scaled if p95_entry is not None else None,
        "p95_latency_scale": high_precision.latency_scale,
        "success_rate_error_bound": fraction_dict(success_entry.cumulative_error),
        "p95_latency_error_bound": fraction_dict(p95_entry.cumulative_error if p95_entry is not None else Fraction(0)),
    }
    tenant = scene.tenants[str(unit["tenant"])]
    decision = decide_gradient(metric, tenant.success_rate_min, tenant.p95_latency_max_ms, high_precision)
    return metric, decision


def _unit_decision_status(unit: dict[str, Any], decision: dict[str, Any] | None) -> str:
    if decision is not None:
        return str(decision["status"])
    status = str(unit["status"])
    if status in {"ABANDONED", "SKIPPED_EARLY_TERMINATION"}:
        return status
    return "INCOMPLETE"


def _ledger_row_output(row: dict[str, Any], registration_seq: int) -> dict[str, Any]:
    return {
        "seq": registration_seq,
        "scope_key": row["scope_key"],
        "local_seq": int(row["local_seq"]),
        "kind": row["kind"],
        "operation": row["operation"],
        "scale": int(row["scale"]),
        "exact": fraction_dict(Fraction(int(row["exact_num"]), int(row["exact_den"]))),
        "result_scaled": int(row["result_scaled"]),
        "discarded": fraction_dict(Fraction(int(row["discarded_num"]), int(row["discarded_den"]))),
        "rounding_direction": row["rounding_direction"],
        "conversion_error": fraction_dict(
            Fraction(int(row["conversion_error_num"]), int(row["conversion_error_den"]))
        ),
        "cumulative_error": fraction_dict(
            Fraction(int(row["cumulative_error_num"]), int(row["cumulative_error_den"]))
        ),
        "source": row["source"],
        "details": json.loads(row["details_json"]),
    }


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _metric_outcome(decision: dict[str, Any] | None, metric: str) -> str:
    if decision is None:
        return "INCOMPLETE"
    if metric == "success":
        if decision.get("success_pass"):
            return "PASS"
        if decision.get("success_degraded"):
            return "DEGRADED"
        return "FAIL"
    if decision.get("p95_pass"):
        return "PASS"
    if decision.get("p95_degraded"):
        return "DEGRADED"
    return "FAIL"


def _fraction_from_dict(value: dict[str, Any] | None) -> Fraction:
    if not value:
        return Fraction(0)
    return Fraction(int(value["num"]), int(value["den"]))


def _scaled_real_text(value: int | None, scale: int) -> str | None:
    if value is None:
        return None
    return decimal_from_fraction(Fraction(int(value), scale))


def _window_ledger_output(
    windows: list[dict[str, Any]],
    ledger_by_scope: dict[str, list[dict[str, Any]]],
    kind: str,
    actual_to_registration: dict[int, int],
) -> list[dict[str, Any]]:
    output = []
    for window in windows:
        window_key = str(window["window_key"])
        rows = ledger_by_scope.get(window_key, [])
        metric_rows = [
            {
                "local_seq": int(row["local_seq"]),
                "seq": actual_to_registration[int(row["seq"])],
                "result_scaled": int(row["result_scaled"]),
                "rounding_direction": row["rounding_direction"],
                "discarded": fraction_dict(
                    Fraction(int(row["discarded_num"]), int(row["discarded_den"]))
                ),
                "cumulative_error_scaled": fraction_dict(
                    Fraction(
                        int(row["cumulative_error_num"]),
                        int(row["cumulative_error_den"]),
                    )
                ),
            }
            for row in rows
            if row["kind"] == kind
        ]
        registration_seqs = [
            actual_to_registration[int(row["seq"])]
            for row in rows
            if int(row["seq"]) in actual_to_registration
        ]
        output.append(
            {
                "window_key": window_key,
                "window_index": int(window["window_index"]),
                "start_ms": int(window["start_ms"]),
                "end_ms": int(window["end_ms"]),
                "ledger_seq_start": min(registration_seqs) if registration_seqs else None,
                "ledger_seq_end": max(registration_seqs) if registration_seqs else None,
                "metric_ledger_entries": metric_rows,
            }
        )
    return output


def _ledger_ref(
    scope_key: str,
    local_seq: Any,
    registration_seq: Any,
) -> dict[str, Any] | None:
    if local_seq is None and registration_seq is None:
        return None
    return {
        "scope_key": scope_key,
        "local_seq": int(local_seq) if local_seq is not None else None,
        "seq": int(registration_seq) if registration_seq is not None else None,
    }


def _gradient_view(
    analysis: dict[str, Any],
    service: str,
    actual_to_registration: dict[int, int],
) -> dict[str, Any]:
    unit = analysis["unit"]
    return {
        "unit_key": unit["unit_key"],
        "batch_id": unit["batch_id"],
        "service": service,
        "tenant": unit["tenant"],
        "gradient_index": int(unit["gradient_index"]),
        "concurrency": int(unit["concurrency"]),
        "status": analysis["status"],
        "high_scale_status": analysis["high_status"],
        "metric": analysis["metric"],
        "decision": analysis["decision"],
        "high_scale_metric": analysis["high_metric"],
        "high_scale_decision": analysis["high_decision"],
        "success_ledger_seq": (
            actual_to_registration.get(int(analysis["success_ledger_seq"]))
            if analysis["success_ledger_seq"] is not None
            else None
        ),
        "p95_ledger_seq": (
            actual_to_registration.get(int(analysis["p95_ledger_seq"]))
            if analysis["p95_ledger_seq"] is not None
            else None
        ),
        "windows": [
            {
                "window_key": window["window_key"],
                "window_index": int(window["window_index"]),
                "start_ms": int(window["start_ms"]),
                "end_ms": int(window["end_ms"]),
                "ledger_seq_start": window["ledger_seq_start"],
                "ledger_seq_end": window["ledger_seq_end"],
            }
            for window in analysis["windows"]
        ],
    }

