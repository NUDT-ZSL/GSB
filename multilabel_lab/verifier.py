"""Two-run reproducibility and cross-stage consistency verification."""

from __future__ import annotations

from typing import Any, Iterable

from .errors import E_METRIC_UNDEFINED, E_VERIFY_FAIL, LabError
from .hashing import sha256_json
from .injection import Injection
from .metrics import recompute_metrics_from_details
from .pipeline import STAGES, classify_experiment


def _first_object_difference(
    expected: Any,
    actual: Any,
    *,
    path: str = "$",
) -> dict[str, Any] | None:
    if type(expected) is not type(actual):
        return {
            "location": path,
            "expected_type": type(expected).__name__,
            "actual_type": type(actual).__name__,
        }
    if isinstance(expected, dict):
        expected_keys = sorted(expected)
        actual_keys = sorted(actual)
        if expected_keys != actual_keys:
            return {
                "location": path,
                "expected_keys": expected_keys,
                "actual_keys": actual_keys,
            }
        for key in expected_keys:
            difference = _first_object_difference(
                expected[key], actual[key], path=f"{path}.{key}"
            )
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return {
                "location": path,
                "expected_length": len(expected),
                "actual_length": len(actual),
            }
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            difference = _first_object_difference(
                left, right, path=f"{path}[{index}]"
            )
            if difference is not None:
                return difference
        return None
    if expected != actual:
        return {"location": path, "expected": expected, "actual": actual}
    return None


def _check_full_hash(
    first: dict[str, Any],
    second: dict[str, Any],
    first_hash: str,
    second_hash: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    passed = first_hash == second_hash
    check = {
        "name": "full_output_hash",
        "passed": passed,
        "algorithm": "sha256",
        "first_hash": first_hash,
        "second_hash": second_hash,
    }
    difference = None
    if not passed:
        difference = _first_object_difference(first, second)
        if difference is None:
            difference = {"location": "$", "reason": "hash_mismatch"}
        difference["kind"] = "rerun_mismatch"
    return check, difference


def _check_stage_sets(report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    samples = report.get("samples", [])
    expected_ids = [sample["sample_id"] for sample in samples]
    audit = report.get("stage_audit", {})
    for stage in STAGES:
        actual_ids = audit.get(stage)
        if actual_ids != expected_ids:
            return (
                {"name": "stage_sample_sets", "passed": False, "stage": stage},
                {
                    "kind": "stage_sample_set_mismatch",
                    "location": f"$.stage_audit.{stage}",
                    "expected": expected_ids,
                    "actual": actual_ids,
                },
            )
    manifests = report.get("stage_manifests", {})
    for stage in STAGES:
        if manifests.get(stage, {}).get("sample_count") != len(expected_ids):
            return (
                {"name": "stage_sample_sets", "passed": False, "stage": stage},
                {
                    "kind": "stage_manifest_count_mismatch",
                    "location": f"$.stage_manifests.{stage}.sample_count",
                    "expected": len(expected_ids),
                    "actual": manifests.get(stage, {}).get("sample_count"),
                },
            )
    return {"name": "stage_sample_sets", "passed": True}, None


def _check_score_decision_reproducibility(
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    labels = report.get("config", {}).get("labels", [])
    for sample_index, sample in enumerate(report.get("samples", [])):
        stages = sample.get("stages", {})
        score_stage = stages.get("score", {})
        decision_stage = stages.get("decision", {})
        score_labels = score_stage.get("labels")
        scores = score_stage.get("values")
        thresholds = decision_stage.get("thresholds")
        decisions = decision_stage.get("decisions")
        if score_labels != labels:
            return (
                {"name": "score_decision_reproducibility", "passed": False},
                {
                    "kind": "label_order_mismatch",
                    "location": f"$.samples[{sample_index}].stages.score.labels",
                    "sample_id": sample.get("sample_id"),
                    "label": labels[0] if labels else None,
                    "expected": labels,
                    "actual": score_labels,
                },
            )
        if (
            not isinstance(scores, list)
            or not isinstance(thresholds, list)
            or not isinstance(decisions, list)
            or not (len(scores) == len(thresholds) == len(decisions) == len(labels))
        ):
            return (
                {"name": "score_decision_reproducibility", "passed": False},
                {
                    "kind": "vector_alignment_mismatch",
                    "location": f"$.samples[{sample_index}].stages.decision",
                    "sample_id": sample.get("sample_id"),
                    "label": labels[0] if labels else None,
                },
            )
        expected_decisions = [
            bool(float(score) >= float(threshold))
            for score, threshold in zip(scores, thresholds, strict=True)
        ]
        for label_index, (label, score, threshold, decision, expected) in enumerate(
            zip(labels, scores, thresholds, decisions, expected_decisions, strict=True)
        ):
            if bool(decision) != expected:
                return (
                    {"name": "score_decision_reproducibility", "passed": False},
                    {
                        "kind": "decision_not_reproducible",
                        "location": (
                            f"$.samples[{sample_index}].stages.decision.decisions[{label_index}]"
                        ),
                        "sample_id": sample.get("sample_id"),
                        "label": label,
                        "score": score,
                        "threshold": threshold,
                        "expected": expected,
                        "actual": decision,
                    },
                )
        expected_labels = [
            label
            for label, decision in zip(labels, decisions, strict=True)
            if bool(decision)
        ]
        if decision_stage.get("predicted_labels") != expected_labels:
            return (
                {"name": "score_decision_reproducibility", "passed": False},
                {
                    "kind": "predicted_label_set_mismatch",
                    "location": (
                        f"$.samples[{sample_index}].stages.decision.predicted_labels"
                    ),
                    "sample_id": sample.get("sample_id"),
                    "label": None,
                    "expected": expected_labels,
                    "actual": decision_stage.get("predicted_labels"),
                },
            )
    return {"name": "score_decision_reproducibility", "passed": True}, None


def _check_traceability(report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    sample_ids = {sample["sample_id"] for sample in report.get("samples", [])}
    source_ids = set(
        report.get("resource_limits", {}).get(
            "source_sample_ids",
            report.get("stage_audit", {}).get("preprocess", sample_ids),
        )
    )
    effective_ids = set(
        report.get("resource_limits", {}).get(
            "effective_sample_ids",
            report.get("stage_audit", {}).get("preprocess", sample_ids),
        )
    )
    if effective_ids != sample_ids:
        return (
            {"name": "injection_degradation_traceability", "passed": False},
            {
                "kind": "resource_effective_sample_mismatch",
                "location": "$.resource_limits.effective_sample_ids",
                "expected": sorted(sample_ids),
                "actual": sorted(effective_ids),
            },
        )
    known_codes = set(report.get("business_codes", []))
    for record in report.get("injection_records", []):
        if record.get("sample_id") not in sample_ids:
            return (
                {"name": "injection_degradation_traceability", "passed": False},
                {
                    "kind": "untraceable_injection",
                    "location": "$.injection_records",
                    "injection_id": record.get("injection_id"),
                    "sample_id": record.get("sample_id"),
                },
            )
    for record in report.get("degradations", []):
        sample_id = record.get("sample_id")
        if sample_id is not None and sample_id not in sample_ids:
            return (
                {"name": "injection_degradation_traceability", "passed": False},
                {
                    "kind": "untraceable_degradation",
                    "location": "$.degradations",
                    "sample_id": sample_id,
                },
            )
        affected = record.get("details", {}).get("affected_sample_ids", [])
        if any(item not in source_ids for item in affected):
            return (
                {"name": "injection_degradation_traceability", "passed": False},
                {
                    "kind": "untraceable_resource_affected_sample",
                    "location": "$.degradations",
                    "sample_ids": affected,
                },
            )
        if record.get("triggered_by") not in known_codes:
            return (
                {"name": "injection_degradation_traceability", "passed": False},
                {
                    "kind": "unknown_trigger_code",
                    "location": "$.degradations",
                    "sample_id": sample_id,
                    "triggered_by": record.get("triggered_by"),
                },
            )
    return {"name": "injection_degradation_traceability", "passed": True}, None


def _check_metrics_recomputation(
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        return (
            {"name": "metric_recomputation", "passed": False},
            {
                "kind": "metric_evidence_missing",
                "location": "$.metrics",
                "expected": "metric object",
                "actual": metrics,
            },
        )
    try:
        recomputed = recompute_metrics_from_details(
            metrics["details"],
            metrics["labels"],
            zero_division=metrics.get("zero_division"),
        )
    except LabError as error:
        return (
            {"name": "metric_recomputation", "passed": False},
            {
                "kind": "metric_recompute_error",
                "location": "$.metrics.details",
                "label": error.label,
                "code": error.code,
            },
        )
    fields = (
        "labels",
        "zero_division",
        "micro",
        "macro_f1",
        "macro_label_count",
        "per_label",
        "hamming_loss",
        "included_cells",
    )
    expected = {field: metrics.get(field) for field in fields}
    if expected != recomputed:
        difference = _first_object_difference(expected, recomputed)
        if difference is None:
            difference = {"location": "$.metrics"}
        difference["kind"] = "metric_recompute_mismatch"
        return {"name": "metric_recomputation", "passed": False}, difference
    return {"name": "metric_recomputation", "passed": True}, None


def verify_report(report: dict[str, Any], second: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify one or two reports; when second is omitted the report is reused."""

    first_hash = sha256_json(report)
    second_report = report if second is None else second
    second_hash = sha256_json(second_report)
    checks: list[dict[str, Any]] = []
    first_deviation: dict[str, Any] | None = None

    check, difference = _check_full_hash(report, second_report, first_hash, second_hash)
    checks.append(check)
    first_deviation = first_deviation or difference

    # A business error before predictions (for example E_RESOURCE_LIMIT under
    # an "error" policy) has no stage/metric evidence to self-check. Its two
    # hashes must still match, and the original business code must not be
    # replaced by a spurious E_VERIFY_FAIL.
    no_prediction_evidence = (
        report.get("status") == "error"
        and "samples" not in report
        and "metrics" not in report
    )
    if no_prediction_evidence:
        checks.append(
            {
                "name": "downstream_self_checks",
                "passed": True,
                "skipped": True,
                "reason": "report contains a pre-prediction business error",
                "business_code": report.get("result_code"),
            }
        )
    else:
        for check_function in (
            _check_stage_sets,
            _check_score_decision_reproducibility,
            _check_traceability,
            _check_metrics_recomputation,
        ):
            check, difference = check_function(report)
            checks.append(check)
            first_deviation = first_deviation or difference

    passed = all(check["passed"] for check in checks) and first_deviation is None
    return {
        "status": "ok" if passed else "error",
        "result_code": "OK" if passed else E_VERIFY_FAIL,
        "algorithm": "sha256",
        "first_hash": first_hash,
        "second_hash": second_hash,
        "matched": first_hash == second_hash,
        "checks": checks,
        "first_deviation": first_deviation,
    }


def verify_experiment(
    snapshot: dict[str, Any],
    injections: Iterable[Injection | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the same experiment twice and return a verified report envelope."""

    first = classify_experiment(snapshot, injections)
    second = classify_experiment(snapshot, injections)
    verification = verify_report(first, second)
    if verification["result_code"] == E_VERIFY_FAIL:
        return {
            "schema_version": "1.0",
            "status": "error",
            "result_code": E_VERIFY_FAIL,
            "business_codes": [E_VERIFY_FAIL],
            "verification": verification,
            "report": first,
        }
    return {
        "schema_version": "1.0",
        "status": first["status"],
        "result_code": first["result_code"],
        "business_codes": first.get("business_codes", []),
        "report": first,
        "verification": verification,
    }


def recompute_report_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Public metric-entry helper used by the CLI metrics command."""

    if report.get("status") == "error" and "metrics" not in report:
        raise LabError(
            E_METRIC_UNDEFINED,
            "report does not contain metric detail evidence",
            stage="metrics",
            details={"report_result_code": report.get("result_code")},
        )
    metrics = report["metrics"]
    recomputed = recompute_metrics_from_details(
        metrics["details"],
        metrics["labels"],
        zero_division=metrics.get("zero_division"),
    )
    fields = (
        "labels",
        "zero_division",
        "micro",
        "macro_f1",
        "macro_label_count",
        "per_label",
        "hamming_loss",
        "included_cells",
    )
    stored = {field: metrics.get(field) for field in fields}
    matches = stored == recomputed
    return {
        "status": "ok" if matches else "error",
        "result_code": "OK" if matches else E_VERIFY_FAIL,
        "matches_stored": matches,
        "stored": stored,
        "recomputed": recomputed,
        "first_deviation": None
        if matches
        else _first_object_difference(stored, recomputed),
    }
