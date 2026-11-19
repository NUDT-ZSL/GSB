from __future__ import annotations

from typing import Any

from .diagnostics import CONTRIBUTING_STAGES, STAGE_ORDER
from .models import Job
from .quant_conv import coverage_counts


def _issue(check: str, message: str, **detail: Any) -> dict[str, Any]:
    return {"check": check, "message": message, "detail": detail}


def verify_run(
    jobs_by_id: dict[str, Job],
    plan: dict[str, Any],
    ledger: dict[str, Any],
    execution: dict[str, Any],
    spectrum: dict[str, Any],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    admitted = plan.get("admitted", [])
    admitted_ids = [item["job_id"] for item in admitted]
    plan_declared_ids = list(plan.get("admitted_job_ids", []))
    if len(admitted_ids) != len(set(admitted_ids)):
        issues.append(_issue("plan_unique", "duplicate admitted job_id in plan"))
    if admitted_ids != plan_declared_ids:
        issues.append(
            _issue(
                "plan_ids",
                "admitted plans and admitted_job_ids differ",
                plans=admitted_ids,
                declared=plan_declared_ids,
            )
        )
    decisions = plan.get("decisions", [])
    decision_ids = [item["job_id"] for item in decisions]
    if set(decision_ids) != set(jobs_by_id):
        issues.append(
            _issue(
                "decision_coverage",
                "decisions do not cover every input job exactly once",
                missing=sorted(set(jobs_by_id) - set(decision_ids)),
                extra=sorted(set(decision_ids) - set(jobs_by_id)),
            )
        )
    if len(decision_ids) != len(set(decision_ids)):
        issues.append(_issue("decision_unique", "duplicate decision job_id"))
    plan_admitted_set = set(admitted_ids)
    for decision in decisions:
        expected = decision["status"] == "admitted"
        if expected != (decision["job_id"] in plan_admitted_set):
            issues.append(
                _issue(
                    "decision_status",
                    "decision status and admitted plan disagree",
                    job_id=decision["job_id"],
                )
            )

    for item in admitted:
        job = jobs_by_id.get(item["job_id"])
        if job is None:
            issues.append(_issue("plan_job", "plan references unknown job", job=item["job_id"]))
            continue
        coverage = coverage_counts(job, item["chunks"])
        if not coverage["ok"]:
            issues.append(
                _issue(
                    "chunk_coverage",
                    "chunk partition does not cover every output exactly once",
                    job_id=job.job_id,
                    coverage=coverage,
                )
            )
        if item.get("coverage") != coverage:
            issues.append(
                _issue(
                    "coverage_record",
                    "recorded coverage does not match recomputed coverage",
                    job_id=job.job_id,
                )
            )
        if int(item["worker_count"]) < 1:
            issues.append(
                _issue("worker_plan", "admitted plan has no worker", job_id=job.job_id)
            )
        if int(item["chunk_count"]) != len(item["chunks"]):
            issues.append(
                _issue("chunk_count", "chunk_count disagrees with chunk list", job_id=job.job_id)
            )

    committed_ids = list(ledger.get("committed_job_ids", []))
    if committed_ids != admitted_ids:
        issues.append(
            _issue(
                "ledger_ids",
                "ledger committed ids differ from admitted ids",
                ledger=committed_ids,
                admitted=admitted_ids,
            )
        )
    entries = ledger.get("entries", [])
    if [entry["job_id"] for entry in entries] != decision_ids:
        issues.append(
            _issue("ledger_entries", "ledger entries do not mirror decisions")
        )
    usage = ledger.get("usage", {})
    limits = ledger.get("limits", {})
    sums = {
        "mac_committed": sum(entry["mac_debit"] for entry in entries),
        "memory_committed_bytes": sum(
            entry["memory_debit_bytes"] for entry in entries
        ),
        "workers_committed": sum(entry["worker_debit"] for entry in entries),
    }
    for key, value in sums.items():
        if usage.get(key) != value:
            issues.append(
                _issue(
                    "ledger_sum",
                    f"ledger usage {key} is not reconciled",
                    expected=value,
                    actual=usage.get(key),
                )
            )
    if usage.get("mac_committed", 0) > limits.get("mac_budget", 0):
        issues.append(_issue("ledger_mac_limit", "committed MACs exceed limit"))
    if usage.get("memory_committed_bytes", 0) > limits.get(
        "memory_budget_bytes", 0
    ):
        issues.append(_issue("ledger_memory_limit", "committed memory exceeds limit"))
    if usage.get("workers_committed", 0) > limits.get("worker_slots", 0):
        issues.append(_issue("ledger_worker_limit", "committed workers exceed slots"))
    for entry in entries:
        if entry["status"] == "rejected" and (
            entry["mac_debit"] != 0
            or entry["memory_debit_bytes"] != 0
            or entry["worker_debit"] != 0
        ):
            issues.append(
                _issue(
                    "rejected_residue",
                    "rejected task has a non-zero budget debit",
                    job_id=entry["job_id"],
                )
            )
        if entry["status"] == "admitted" and entry["code"] != "OK":
            issues.append(
                _issue(
                    "admitted_code",
                    "admitted entry has a non-OK code",
                    job_id=entry["job_id"],
                    code=entry["code"],
                )
            )

    execution_ids = list(execution.get("admitted_job_ids", []))
    execution_jobs = execution.get("jobs", [])
    summary_ids = [item["job_id"] for item in execution_jobs]
    if execution_ids != admitted_ids:
        issues.append(
            _issue(
                "execution_ids",
                "execution admitted ids differ from scheduling admitted ids",
                execution=execution_ids,
                plan=admitted_ids,
            )
        )
    if len(summary_ids) != len(set(summary_ids)) or set(summary_ids) != set(
        admitted_ids
    ):
        issues.append(
            _issue(
                "execution_union",
                "execution summaries do not equal the admitted-job union",
                summaries=summary_ids,
                admitted=admitted_ids,
            )
        )
    completed = list(execution.get("completed_job_ids", []))
    failed = list(execution.get("failed_job_ids", []))
    summary_ok = [item["job_id"] for item in execution_jobs if item["status"] == "ok"]
    summary_failed = [
        item["job_id"] for item in execution_jobs if item["status"] == "failed"
    ]
    if completed != summary_ok or failed != summary_failed:
        issues.append(
            _issue(
                "execution_status",
                "completed/failed id lists disagree with per-job summaries",
            )
        )
    if set(completed) | set(failed) != set(admitted_ids):
        issues.append(
            _issue(
                "execution_partition",
                "completed and failed tasks do not partition admitted tasks",
            )
        )
    for item in execution_jobs:
        if item["status"] == "failed" and not item.get("worker_errors"):
            issues.append(
                _issue(
                    "failure_attribution",
                    "failed job has no worker error attribution",
                    job_id=item["job_id"],
                )
            )
        if item["status"] == "ok" and item.get("output_checksum") is None:
            issues.append(
                _issue(
                    "success_checksum",
                    "successful job has no output checksum",
                    job_id=item["job_id"],
                )
            )
        for error in item.get("worker_errors", []):
            if error.get("job_id") != item["job_id"]:
                issues.append(
                    _issue(
                        "error_attribution",
                        "worker error is attributed to the wrong job",
                        job_id=item["job_id"],
                        error=error,
                    )
                )
            if error.get("chunk_id") is None or not error.get("stage"):
                issues.append(
                    _issue(
                        "error_shape",
                        "worker error lacks chunk_id or stage",
                        job_id=item["job_id"],
                    )
                )

    diagnostics = spectrum.get("jobs", [])
    diagnostics_by_id = {item["job_id"]: item for item in diagnostics}
    if len(diagnostics_by_id) != len(diagnostics):
        issues.append(_issue("spectrum_unique", "duplicate diagnostic job"))
    if set(diagnostics_by_id) != set(completed):
        issues.append(
            _issue(
                "spectrum_coverage",
                "error spectrum does not cover exactly the successful jobs",
                spectrum=sorted(diagnostics_by_id),
                completed=sorted(completed),
            )
        )
    for item in diagnostics:
        stages = item.get("stages", [])
        stage_names = [stage.get("stage") for stage in stages]
        if tuple(stage_names) != STAGE_ORDER:
            issues.append(
                _issue(
                    "spectrum_stages",
                    "error spectrum stages are incomplete or out of order",
                    job_id=item["job_id"],
                    stages=stage_names,
                )
            )
        if item.get("dominant_stage") not in CONTRIBUTING_STAGES:
            issues.append(
                _issue(
                    "dominant_stage",
                    "dominant stage is not a contributing stage",
                    job_id=item["job_id"],
                )
            )
        for stage in stages:
            for key in (
                "max_abs_error",
                "max_relative_error",
                "abs_error_quantiles",
                "relative_error_quantiles",
                "amplification_vs_previous",
                "amplification_vs_quantization",
            ):
                if key not in stage:
                    issues.append(
                        _issue(
                            "stage_metric",
                            f"stage metric {key} is missing",
                            job_id=item["job_id"],
                            stage=stage.get("stage"),
                        )
                    )
        dominant = next(
            (
                stage
                for stage in stages
                if stage.get("stage") == item.get("dominant_stage")
            ),
            None,
        )
        if (
            dominant is not None
            and dominant.get("max_abs_error", 0) > 0
            and not dominant.get("top_abs_errors")
        ):
            issues.append(
                _issue(
                    "dominant_trace",
                    "dominant stage has error but no traceable locations",
                    job_id=item["job_id"],
                )
            )
        if item.get("tolerance_ok") is False and not item.get("violations"):
            issues.append(
                _issue(
                    "violation_detail",
                    "tolerance failure has no element-level details",
                    job_id=item["job_id"],
                )
            )
        for violation in item.get("violations", []):
            if violation.get("job_id") != item["job_id"]:
                issues.append(
                    _issue(
                        "violation_job",
                        "violation is attributed to the wrong job",
                        job_id=item["job_id"],
                    )
                )
            if not isinstance(violation.get("index"), list) or len(violation["index"]) != 4:
                issues.append(
                    _issue(
                        "violation_index",
                        "violation does not include a four-dimensional position",
                        job_id=item["job_id"],
                    )
                )
            if violation.get("stage") != "dequantized_output":
                issues.append(
                    _issue(
                        "violation_stage",
                        "violation is not attached to dequantized_output",
                        job_id=item["job_id"],
                    )
                )
    expected_failed_ids = sorted(item["job_id"] for item in diagnostics if not item["tolerance_ok"])
    if sorted(spectrum.get("tolerance_failed_job_ids", [])) != expected_failed_ids:
        issues.append(
            _issue(
                "tolerance_union",
                "spectrum tolerance-failed ids are not reconciled",
            )
        )

    checks = [
        "plan ids unique and complete",
        "chunk coverage exact-once recomputed",
        "ledger debits reconciled and zero-residue on rejection",
        "execution summary union equals admitted union",
        "error spectrum maps to successful jobs with traceable details",
    ]
    return {"ok": not issues, "checks": checks, "issues": issues}

