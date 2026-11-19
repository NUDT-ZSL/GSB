from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from .consistency import verify_run
from .diagnostics import build_error_spectrum, diagnose_job
from .errors import BusinessIssue, DomainError, ErrorCode
from .executor import execute_plan
from .models import (
    Budget,
    ExecutionConfig,
    Job,
    SchedulerConfig,
    ToleranceConfig,
)
from .scheduler import schedule_batch
from .snapshots import SnapshotStore
from .validation import validate_config, validate_job_records


class QConvService:
    def __init__(
        self,
        snapshots_dir: str | Path = "snapshots",
        cache_dir: str | Path = "cache",
        recent_error_limit: int = 50,
    ) -> None:
        self.snapshots_dir = Path(snapshots_dir)
        self.cache_dir = Path(cache_dir)
        self.store = SnapshotStore(self.snapshots_dir)
        self.recent_errors: deque[dict[str, Any]] = deque(maxlen=recent_error_limit)

    def _remember(self, issue: BusinessIssue | dict[str, Any]) -> None:
        if isinstance(issue, BusinessIssue):
            payload = issue.as_dict()
        else:
            payload = dict(issue)
        self.recent_errors.append(payload)

    def run_records(
        self,
        records: list[dict[str, Any]],
        budget: Budget,
        scheduler_config: SchedulerConfig | None = None,
        tolerance: ToleranceConfig | None = None,
        execution_config: ExecutionConfig | None = None,
        *,
        commit_snapshot: bool = True,
    ) -> dict[str, Any]:
        scheduler_config = scheduler_config or SchedulerConfig()
        tolerance = tolerance or ToleranceConfig()
        jobs, issues = validate_job_records(records)
        issues.extend(validate_config(budget, scheduler_config, tolerance))
        if issues:
            for issue in issues:
                self._remember(issue)
            return {
                "format": "qconv-run-artifact-v1",
                "status": "validation_failed",
                "code": str(issues[0].code),
                "run_status": "validation_failed",
                "jobs": records,
                "budget": budget.to_dict(),
                "scheduler_config": scheduler_config.to_dict(),
                "tolerance": tolerance.to_dict(),
                "validation_issues": [issue.as_dict() for issue in issues],
                "business_errors": [issue.as_dict() for issue in issues],
                "recent_errors": list(self.recent_errors),
                "plan": None,
                "ledger": None,
                "execution": None,
                "error_spectrum": None,
                "self_check": None,
                "snapshot": None,
                "snapshot_error": None,
            }
        if execution_config is None:
            execution_config = ExecutionConfig(max_processes=budget.worker_slots)

        scheduled = schedule_batch(jobs, budget, scheduler_config)
        plan = scheduled["plan"]
        ledger = scheduled["ledger"]
        jobs_by_id: dict[str, Job] = scheduled["jobs_by_id"]
        outputs, execution = execute_plan(jobs_by_id, plan, execution_config)

        diagnostics: list[dict[str, Any]] = []
        diagnostic_issues: list[BusinessIssue] = []
        for job_id in execution["completed_job_ids"]:
            try:
                diagnostics.append(
                    diagnose_job(jobs_by_id[job_id], outputs[job_id], tolerance)
                )
            except DomainError as exc:
                diagnostic_issues.append(exc.as_issue())
        spectrum = build_error_spectrum(diagnostics)

        business_errors: list[BusinessIssue] = []
        for decision in plan["decisions"]:
            if decision["status"] == "rejected":
                business_errors.append(
                    BusinessIssue(
                        code=decision["code"],
                        message=decision["message"],
                        job_id=decision["job_id"],
                        detail={
                            "budget_before": decision.get("budget_before"),
                            "mac_required": decision.get("mac_required"),
                            "combination_remaining": decision.get(
                                "combination_remaining"
                            ),
                            "memory_remaining": decision.get("memory_remaining"),
                            "minimum_memory_bytes": decision.get(
                                "minimum_memory_bytes"
                            ),
                        },
                    )
                )
        for error in execution["worker_errors"]:
            business_errors.append(
                BusinessIssue(
                    code=error["code"],
                    message=error["message"],
                    job_id=error["job_id"],
                    chunk_id=error["chunk_id"],
                    stage=error["stage"],
                    detail=error.get("detail", {}),
                )
            )
        for diagnostic in diagnostics:
            if not diagnostic["tolerance_ok"]:
                first = diagnostic["violations"][0] if diagnostic["violations"] else None
                business_errors.append(
                    BusinessIssue(
                        code=ErrorCode.E_TOLERANCE_EXCEEDED,
                        message=(
                            f"{diagnostic['violation_count']} output element(s) exceed "
                            "the configured tolerance"
                        ),
                        job_id=diagnostic["job_id"],
                        stage="dequantized_output",
                        detail={
                            "violation_count": diagnostic["violation_count"],
                            "first_violation": first,
                            "dominant_stage": diagnostic["dominant_stage"],
                        },
                    )
                )
        business_errors.extend(diagnostic_issues)

        self_check = verify_run(
            jobs_by_id, plan, ledger, execution, spectrum
        )
        if not self_check["ok"]:
            business_errors.append(
                BusinessIssue(
                    code=ErrorCode.E_INCONSISTENT,
                    message="cross-module consistency self-check failed",
                    detail={"issues": self_check["issues"]},
                )
            )

        worker_failure = bool(execution["failed_job_ids"])
        all_failed = (
            bool(execution["admitted_job_ids"])
            and not execution["completed_job_ids"]
            and bool(execution["failed_job_ids"])
        )
        tolerance_failure = bool(spectrum["tolerance_failed_job_ids"])
        if not self_check["ok"]:
            status = "inconsistent"
            primary_code = ErrorCode.E_INCONSISTENT.value
        elif all_failed:
            status = "failed"
            primary_code = execution["worker_errors"][0]["code"]
        elif worker_failure:
            status = "partial_failure"
            primary_code = execution["worker_errors"][0]["code"]
        elif tolerance_failure:
            status = "tolerance_exceeded"
            primary_code = ErrorCode.E_TOLERANCE_EXCEEDED.value
        elif not execution["admitted_job_ids"]:
            status = "no_admission"
            primary_code = business_errors[0].code if business_errors else "OK"
        elif business_errors:
            status = "ok_with_rejections"
            primary_code = business_errors[0].code
        else:
            status = "ok"
            primary_code = "OK"

        for issue in business_errors:
            self._remember(issue)
        result_payload = {
            "format": "qconv-result-v1",
            "status": status,
            "primary_code": str(primary_code),
            "execution": execution,
            "error_spectrum": spectrum,
            "business_errors": [issue.as_dict() for issue in business_errors],
            "self_check": self_check,
        }
        artifact: dict[str, Any] = {
            "format": "qconv-run-artifact-v1",
            "status": status,
            "code": str(primary_code),
            "run_status": status,
            "jobs": [job.to_dict() for job in jobs],
            "budget": budget.to_dict(),
            "scheduler_config": scheduler_config.to_dict(),
            "tolerance": tolerance.to_dict(),
            "validation_issues": [],
            "plan": plan,
            "ledger": ledger,
            "execution": execution,
            "error_spectrum": spectrum,
            "self_check": self_check,
            "business_errors": [issue.as_dict() for issue in business_errors],
            "recent_errors": list(self.recent_errors),
            "snapshot": None,
            "snapshot_error": None,
        }
        if not self_check["ok"]:
            artifact["snapshot_error"] = {
                "code": ErrorCode.E_INCONSISTENT.value,
                "message": "snapshot not committed because self-check failed",
            }
            return artifact
        if commit_snapshot:
            try:
                snapshot = self.store.commit(
                    {
                        "plan": plan,
                        "budget_ledger": ledger,
                        "result_summary": result_payload,
                    }
                )
                artifact["snapshot"] = {
                    "path": str(self.store.snapshot_path),
                    "generation": snapshot["generation"],
                    "previous_generation": snapshot["previous_generation"],
                    "snapshot_checksum": snapshot["snapshot_checksum"],
                    "component_versions": {
                        name: wrapper["version"]
                        for name, wrapper in snapshot["components"].items()
                    },
                    "component_checksums": {
                        name: wrapper["checksum"]
                        for name, wrapper in snapshot["components"].items()
                    },
                }
            except DomainError as exc:
                self._remember(exc.as_issue())
                artifact["run_status"] = status
                artifact["status"] = "snapshot_error"
                artifact["code"] = str(exc.code)
                artifact["snapshot_error"] = exc.as_dict()
                artifact["recent_errors"] = list(self.recent_errors)
        return artifact

    def load_snapshot(self) -> dict[str, Any] | None:
        try:
            return self.store.load()
        except DomainError as exc:
            self._remember(exc.as_issue())
            raise

    def clean(self) -> dict[str, Any]:
        result = self.store.clean()
        if self.cache_dir.exists():
            removed_cache: list[str] = []
            for path in sorted(self.cache_dir.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                    removed_cache.append(str(path))
                elif path.is_dir():
                    path.rmdir()
            result["removed"].extend(removed_cache)
        result["recent_errors_cleared"] = len(self.recent_errors)
        self.recent_errors.clear()
        return result

