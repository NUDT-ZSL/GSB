from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .analysis import analyze_context
from .clock import WindowPlan, build_schedule
from .scene import parse_scene
from .stats import Observation, aggregate_gradient_summary, build_window_summary, decide_gradient
from .store import Store
from .util import canonical_json, sha256_json


def verify_database(store: Store) -> dict[str, Any]:
    context = store.report_context()
    meta = context["meta"]
    scene = parse_scene(json.loads(meta["scene_json"]))
    report = analyze_context(scene, context)
    schedule = build_schedule(scene)
    expected_units = {unit.unit_key: unit for unit in schedule.units}
    expected_windows = {
        window.window_key: (unit, window)
        for unit in schedule.units
        for window in unit.windows
    }
    checks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    def check(name: str, ok: bool, **details: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), "details": details})
        if not ok:
            issues.append({"name": name, **details})

    check(
        "scene_snapshot_hash",
        meta.get("scene_sha256") == scene.sha256,
        stored=meta.get("scene_sha256"),
        recomputed=scene.sha256,
        code="E_SCENE_MISMATCH",
    )
    budget = int(meta.get("global_budget_ms", "0"))
    used = int(meta.get("budget_used_ms", "0"))
    check(
        "global_budget_not_overspent",
        used <= budget,
        budget_ms=budget,
        used_ms=used,
        code="E_SCHEMA_MISMATCH",
    )
    reserved_total = sum(int(row["budget_reserved_ms"]) for row in context["units"])
    check(
        "budget_reservation_matches_units",
        used == reserved_total,
        used_ms=used,
        reserved_total_ms=reserved_total,
        code="E_SCHEMA_MISMATCH",
    )

    windows_by_key = {str(row["window_key"]): row for row in context["windows"]}
    ledger_by_scope: dict[str, list[dict[str, Any]]] = {}
    for row in context["ledger"]:
        ledger_by_scope.setdefault(str(row["scope_key"]), []).append(row)
    for rows in ledger_by_scope.values():
        rows.sort(key=lambda item: int(item["local_seq"]))

    all_request_ids: set[str] = set()
    duplicate_request_ids: set[str] = set()
    for window in sorted(context["windows"], key=lambda item: str(item["window_key"])):
        window_key = str(window["window_key"])
        payload = json.loads(window["payload_json"])
        stored_summary = json.loads(window["summary_json"])
        observations = [_observation_from_payload(item) for item in payload.get("observations", [])]
        window_plan = WindowPlan(
            window_key=window_key,
            window_index=int(window["window_index"]),
            start_ms=int(window["start_ms"]),
            end_ms=int(window["end_ms"]),
            waves=(),
            requests=(),
        )
        summary, rebuilt_payload, expected_entries = build_window_summary(
            window_plan,
            observations,
            scene.precision,
        )
        rebuilt_hash = sha256_json(rebuilt_payload)
        check(
            f"window_hash:{window_key}",
            rebuilt_hash == window["payload_hash"] == stored_summary.get("payload_hash"),
            window_key=window_key,
            stored=window["payload_hash"],
            summary=stored_summary.get("payload_hash"),
            recomputed=rebuilt_hash,
            code="E_WINDOW_SUMMARY_MISMATCH",
        )
        field_pairs = [
            ("request_count", summary["request_count"]),
            ("success_count", summary["success_count"]),
            ("failure_count", summary["failure_count"]),
            ("success_rate_scaled", summary["success_rate_scaled"]),
            ("p95_latency_scaled", summary["p95_latency_scaled"]),
        ]
        fields_ok = all(window[field] == value for field, value in field_pairs)
        check(
            f"window_fields:{window_key}",
            fields_ok,
            window_key=window_key,
            expected={field: value for field, value in field_pairs},
            actual={field: window[field] for field, _ in field_pairs},
            code="E_WINDOW_SUMMARY_MISMATCH",
        )
        stored_entries = ledger_by_scope.get(window_key, [])
        ledger_ok, ledger_details = _compare_ledger(stored_entries, expected_entries)
        check(
            f"window_ledger:{window_key}",
            ledger_ok,
            window_key=window_key,
            **ledger_details,
            code="E_WINDOW_SUMMARY_MISMATCH",
        )
        assignment_ok = all(
            item.release_ms >= int(window["start_ms"]) and item.release_ms < int(window["end_ms"])
            for item in observations
        )
        expected_pair = expected_windows.get(window_key)
        expected_ok = False
        if expected_pair is not None:
            expected_unit, expected_window = expected_pair
            expected_ok = (
                str(window["unit_key"]) == expected_unit.unit_key
                and int(window["window_index"]) == expected_window.window_index
                and int(window["start_ms"]) == expected_window.start_ms
                and int(window["end_ms"]) == expected_window.end_ms
                and {item.request_id for item in observations}
                == {request.request_id for request in expected_window.requests}
            )
        check(
            f"window_assignment:{window_key}",
            assignment_ok and expected_ok,
            window_key=window_key,
            start_ms=int(window["start_ms"]),
            end_ms=int(window["end_ms"]),
            expected_in_schedule=expected_ok,
            code="E_WINDOW_SUMMARY_MISMATCH",
        )
        for observation in observations:
            if observation.request_id in all_request_ids:
                duplicate_request_ids.add(observation.request_id)
            all_request_ids.add(observation.request_id)
        error_rows = [
            row
            for row in context["errors"]
            if str(row["window_key"]) == window_key
        ]
        payload_errors = [item for item in observations if item.error_code]
        error_match = len(error_rows) == len(payload_errors) and sorted(
            (str(row["request_id"]), str(row["code"])) for row in error_rows
        ) == sorted((item.request_id, str(item.error_code)) for item in payload_errors)
        check(
            f"request_errors:{window_key}",
            error_match,
            window_key=window_key,
            table_count=len(error_rows),
            payload_count=len(payload_errors),
            code="E_VERIFY_FAILED",
        )
    check(
        "request_ids_unique",
        not duplicate_request_ids,
        duplicate_request_ids=sorted(duplicate_request_ids),
        code="E_VERIFY_FAILED",
    )

    units_by_key = {str(row["unit_key"]): row for row in context["units"]}
    windows_by_unit: dict[str, list[dict[str, Any]]] = {}
    for window in context["windows"]:
        windows_by_unit.setdefault(str(window["unit_key"]), []).append(window)
    for unit_key, unit in sorted(units_by_key.items()):
        unit_windows = sorted(
            windows_by_unit.get(unit_key, []),
            key=lambda item: (int(item["window_index"]), int(item["version"])),
        )
        if unit["status"] == "COMPLETED":
            summaries = [json.loads(item["summary_json"]) for item in unit_windows]
            payloads = [json.loads(item["payload_json"]) for item in unit_windows]
            metric, expected_entries = aggregate_gradient_summary(
                unit_key,
                summaries,
                payloads,
                scene.precision,
            )
            decision_payload = json.loads(unit["decision_json"])
            stored_metric = decision_payload["metric"]
            metric_ok = all(
                stored_metric.get(field) == metric.get(field)
                for field in (
                    "request_count",
                    "success_count",
                    "failure_count",
                    "success_rate_scaled",
                    "p95_latency_scaled",
                )
            )
            check(
                f"gradient_metric:{unit_key}",
                metric_ok,
                unit_key=unit_key,
                stored=stored_metric,
                recomputed=metric,
                code="E_WINDOW_SUMMARY_MISMATCH",
            )
            tenant = scene.tenants[str(unit["tenant"])]
            decision = decide_gradient(
                metric,
                tenant.success_rate_min,
                tenant.p95_latency_max_ms,
                scene.precision,
            )
            check(
                f"gradient_decision:{unit_key}",
                decision["status"] == decision_payload["decision"].get("status"),
                unit_key=unit_key,
                stored=decision_payload["decision"].get("status"),
                recomputed=decision["status"],
                code="E_WINDOW_SUMMARY_MISMATCH",
            )
            stored_entries = ledger_by_scope.get(f"gradient:{unit_key}", [])
            ledger_ok, ledger_details = _compare_ledger(stored_entries, expected_entries)
            check(
                f"gradient_ledger:{unit_key}",
                ledger_ok,
                unit_key=unit_key,
                **ledger_details,
                code="E_WINDOW_SUMMARY_MISMATCH",
            )
            check(
                f"gradient_window_count:{unit_key}",
                len(unit_windows) == int(unit["window_count"]),
                unit_key=unit_key,
                expected=int(unit["window_count"]),
                actual=len(unit_windows),
                code="E_INCOMPLETE_RUN",
            )
            plan = expected_units.get(unit_key)
            expected_keys = {window.window_key for window in plan.windows} if plan else set()
            actual_keys = {str(window["window_key"]) for window in unit_windows}
            check(
                f"gradient_window_partition:{unit_key}",
                actual_keys == expected_keys,
                unit_key=unit_key,
                missing=sorted(expected_keys - actual_keys),
                extra=sorted(actual_keys - expected_keys),
                code="E_WINDOW_SUMMARY_MISMATCH",
            )
        elif unit["status"] in {"ABANDONED", "SKIPPED_EARLY_TERMINATION"}:
            check(
                f"terminal_without_commit:{unit_key}",
                not unit_windows,
                unit_key=unit_key,
                committed_windows=len(unit_windows),
                status=unit["status"],
                code="E_WINDOW_SUMMARY_MISMATCH",
            )

    persisted_conclusions = {
        str(row["batch_id"]): json.loads(row["data_json"])
        for row in context["conclusions"]
    }
    report_conclusions = {
        item["batch_id"]: item
        for item in report["capacity_conclusions"]
    }
    conclusions_ok = canonical_json(persisted_conclusions) == canonical_json(report_conclusions)
    check(
        "persisted_conclusions",
        conclusions_ok,
        persisted_batches=sorted(persisted_conclusions),
        report_batches=sorted(report_conclusions),
        code="E_INCOMPLETE_RUN",
    )
    persisted_marks = {
        str(row["mark_key"]): json.loads(row["data_json"])
        for row in context["marks"]
    }
    report_marks = {
        item["mark_key"]: item
        for item in report["precision_sensitive"]
    }
    marks_ok = canonical_json(persisted_marks) == canonical_json(report_marks)
    check(
        "persisted_sensitivity_marks",
        marks_ok,
        persisted_marks=sorted(persisted_marks),
        report_marks=sorted(report_marks),
        code="E_INCOMPLETE_RUN",
    )
    check(
        "analysis_complete",
        bool(report["analysis_complete"]),
        unit_status_counts=report["unit_status_counts"],
        code="E_INCOMPLETE_RUN",
    )
    check(
        "no_active_leases_when_complete",
        (not report["analysis_complete"]) or int(context.get("active_leases", 0)) == 0,
        active_leases=int(context.get("active_leases", 0)),
        code="E_INCOMPLETE_RUN",
    )

    return {
        "ok": not issues,
        "engine_version": report["engine_version"],
        "scene_id": report["scene"]["scene_id"],
        "scene_sha256": report["scene"]["sha256"],
        "report_sha256": report["report_sha256"],
        "checks_total": len(checks),
        "checks_failed": len(issues),
        "issues": issues,
        "checks": checks,
        "analysis_complete": report["analysis_complete"],
    }


def _observation_from_payload(item: dict[str, Any]) -> Observation:
    latency = item.get("latency_ms")
    return Observation(
        request_id=str(item["request_id"]),
        request_index=int(item["request_index"]),
        wave_index=int(item["wave_index"]),
        release_ms=int(item["release_ms"]),
        window_index=int(item["window_index"]),
        http_status=item.get("http_status"),
        success=bool(item["success"]),
        latency_ms=Decimal(str(latency)) if latency is not None else None,
        error_code=item.get("error_code"),
        error_message=item.get("error_message"),
        error_fields=item.get("error_fields", {}),
        raw_body=None,
    )


def _compare_ledger(
    stored_rows: list[dict[str, Any]],
    expected_entries: list[Any],
) -> tuple[bool, dict[str, Any]]:
    if len(stored_rows) != len(expected_entries):
        return False, {
            "reason": "entry_count",
            "stored_count": len(stored_rows),
            "expected_count": len(expected_entries),
        }
    for row, expected in zip(stored_rows, expected_entries):
        checks = {
            "local_seq": int(row["local_seq"]) == expected.local_seq,
            "kind": row["kind"] == expected.kind,
            "operation": row["operation"] == expected.operation,
            "scale": int(row["scale"]) == expected.scale,
            "exact": Fraction(int(row["exact_num"]), int(row["exact_den"])) == Fraction(expected.exact_num, expected.exact_den),
            "result_scaled": int(row["result_scaled"]) == expected.result_scaled,
            "discarded": Fraction(int(row["discarded_num"]), int(row["discarded_den"])) == expected.discarded,
            "rounding_direction": row["rounding_direction"] == expected.rounding_direction,
            "conversion_error": Fraction(
                int(row["conversion_error_num"]), int(row["conversion_error_den"])
            )
            == expected.conversion_error,
            "cumulative_error": Fraction(
                int(row["cumulative_error_num"]), int(row["cumulative_error_den"])
            )
            == expected.cumulative_error,
            "source": row["source"] == expected.source,
            "details": canonical_json(json.loads(row["details_json"])) == canonical_json(expected.details),
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            return False, {
                "reason": "entry_mismatch",
                "local_seq": expected.local_seq,
                "failed_fields": failed,
            }
    return True, {"entries": len(stored_rows)}

