"""Cross-level consistency checks for frames, intervals, quantization and traces."""

from __future__ import annotations

import math
from typing import Any

from .errors import DomainError, E_INCONSISTENT
from .pipeline import PipelineResult
from .precision import verify_fixed_recomputation


def _finding(category: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "code": E_INCONSISTENT,
        "category": category,
        "message": message,
        "details": details,
    }


def run_selfcheck(result: PipelineResult) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    category_status: dict[str, dict[str, Any]] = {}

    frame_failures: list[dict[str, Any]] = []
    assignment_failures: list[dict[str, Any]] = []
    interval_failures: list[dict[str, Any]] = []
    fixed_failures: list[dict[str, Any]] = []
    trace_failures: list[dict[str, Any]] = []

    for utterance_id, run in sorted(result.utterances.items()):
        truth_indices = [int(frame.frame_index) for frame in run.sample.truth]
        expected = list(range(len(truth_indices)))
        if truth_indices != expected:
            frame_failures.append(
                {
                    "utterance_id": utterance_id,
                    "layer": "truth",
                    "expected": expected[:8],
                    "actual": truth_indices[:8],
                }
            )
        for tier, execution in sorted(run.executions.items()):
            actual = [int(value) for value in execution.frame_indices]
            if actual != expected:
                frame_failures.append(
                    {
                        "utterance_id": utterance_id,
                        "layer": tier,
                        "first_mismatch_frame": next(
                            (
                                index
                                for index, value in enumerate(actual)
                                if index >= len(expected) or value != expected[index]
                            ),
                            min(len(actual), len(expected)),
                        ),
                        "expected_count": len(expected),
                        "actual_count": len(actual),
                    }
                )
        if run.executions:
            reference = run.executions["float64"]
            reference_labels = tuple(reference.assignment_labels)
            for tier, execution in sorted(run.executions.items()):
                if execution.assignment_digest != reference.assignment_digest:
                    assignment_failures.append(
                        {
                            "utterance_id": utterance_id,
                            "tier": tier,
                            "expected_digest": reference.assignment_digest,
                            "actual_digest": execution.assignment_digest,
                        }
                    )
                if tuple(execution.assignment_labels) != reference_labels:
                    assignment_failures.append(
                        {
                            "utterance_id": utterance_id,
                            "tier": tier,
                            "reason": "label_sequence_changed",
                        }
                    )
        for tier, spectrum in sorted(run.error_spectra.items()):
            for stage_name, stage in sorted(spectrum["stages"].items()):
                interval = stage["error_interval"]
                low = float(interval["low"])
                high = float(interval["high"])
                if not (math.isfinite(low) and math.isfinite(high) and low <= high):
                    interval_failures.append(
                        {
                            "utterance_id": utterance_id,
                            "tier": tier,
                            "stage": stage_name,
                            "scope": "stage",
                            "low": low,
                            "high": high,
                        }
                    )
                for row in stage["details"]:
                    detail_low = float(row["interval_low"])
                    detail_high = float(row["interval_high"])
                    if not (
                        math.isfinite(detail_low)
                        and math.isfinite(detail_high)
                        and detail_low <= detail_high
                    ):
                        interval_failures.append(
                            {
                                "utterance_id": utterance_id,
                                "tier": tier,
                                "stage": stage_name,
                                "scope": "frame",
                                "frame_index": row["frame_index"],
                                "low": detail_low,
                                "high": detail_high,
                            }
                        )
        for tier, execution in sorted(run.executions.items()):
            verification = verify_fixed_recomputation(execution)
            if not verification["ok"]:
                fixed_failures.append(
                    {
                        "utterance_id": utterance_id,
                        "tier": tier,
                        "verification": verification,
                    }
                )
        trace_tiers = set(run.error_spectra)
        if trace_tiers:
            for item in run.clone_decision.get("ranked", []):
                trace = item.get("trace") or {}
                stage = trace.get("stage")
                tier = trace.get("error_spectrum_tier")
                digest = trace.get("detail_digest")
                if tier not in trace_tiers:
                    trace_failures.append(
                        {
                            "utterance_id": utterance_id,
                            "candidate_id": item.get("candidate_id"),
                            "reason": "trace_tier_missing",
                            "tier": tier,
                        }
                    )
                    continue
                spectrum = run.error_spectra[tier]
                if stage not in spectrum.get("stages", {}):
                    trace_failures.append(
                        {
                            "utterance_id": utterance_id,
                            "candidate_id": item.get("candidate_id"),
                            "reason": "trace_stage_missing",
                            "stage": stage,
                        }
                    )
                    continue
                expected_digest = spectrum["stages"][stage].get("detail_digest")
                if digest != expected_digest:
                    trace_failures.append(
                        {
                            "utterance_id": utterance_id,
                            "candidate_id": item.get("candidate_id"),
                            "reason": "detail_digest_mismatch",
                            "expected": expected_digest,
                            "actual": digest,
                        }
                    )
                expected_frames = list(run.executions[tier].frame_indices)
                if list(trace.get("frame_indices", [])) != expected_frames:
                    trace_failures.append(
                        {
                            "utterance_id": utterance_id,
                            "candidate_id": item.get("candidate_id"),
                            "reason": "trace_frame_set_mismatch",
                        }
                    )

    for tier, tier_report in sorted(result.aggregate_error_spectrum["tiers"].items()):
        for stage_name, stage in sorted(tier_report["stages"].items()):
            interval = stage["error_interval"]
            if not (float(interval["low"]) <= float(interval["high"])):
                interval_failures.append(
                    {
                        "utterance_id": None,
                        "tier": tier,
                        "stage": stage_name,
                        "scope": "aggregate",
                        "low": interval["low"],
                        "high": interval["high"],
                    }
                )
        if tier == "fixed":
            verification = tier_report.get("fixed_recompute", {})
            if not verification.get("ok", False):
                fixed_failures.append(
                    {
                        "utterance_id": None,
                        "tier": tier,
                        "verification": verification,
                    }
                )

    checks = (
        ("frame_index_set", frame_failures),
        ("unit_assignment_set", assignment_failures),
        ("error_interval_order", interval_failures),
        ("fixed_recompute", fixed_failures),
        ("clone_decision_trace", trace_failures),
    )
    for category, failures in checks:
        category_status[category] = {
            "ok": not failures,
            "failure_count": len(failures),
        }
        for failure in failures:
            findings.append(_finding(category, f"{category} check failed", **failure))
    findings = sorted(
        findings,
        key=lambda item: (
            item["category"],
            str(item["details"].get("utterance_id")),
            str(item["details"].get("tier")),
            str(item["details"].get("stage")),
        ),
    )
    ok = not findings
    return {
        "ok": ok,
        "business_code": None if ok else E_INCONSISTENT,
        "categories": category_status,
        "findings": findings,
        "checked_utterances": sorted(result.utterances),
    }


def assert_selfcheck(result: PipelineResult) -> dict[str, Any]:
    report = run_selfcheck(result)
    if not report["ok"]:
        first = report["findings"][0]
        raise DomainError(
            E_INCONSISTENT,
            first["message"],
            stage="selfcheck",
            utterance_id=first["details"].get("utterance_id"),
            field_name="category",
            details={"category": first["category"], **first["details"]},
        )
    return report
