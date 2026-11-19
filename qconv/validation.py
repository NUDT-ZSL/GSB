from __future__ import annotations

from typing import Any, Iterable

from .errors import BusinessIssue, DomainError, ErrorCode
from .models import Budget, Job, SchedulerConfig, ToleranceConfig


def validate_job_records(
    records: Iterable[dict[str, Any]],
) -> tuple[list[Job], list[BusinessIssue]]:
    jobs: list[Job] = []
    issues: list[BusinessIssue] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        job_id = raw.get("job_id") if isinstance(raw, dict) else None
        if isinstance(job_id, str) and job_id in seen:
            issues.append(
                BusinessIssue(
                    code=ErrorCode.E_DUPLICATE_JOB_ID,
                    message=f"duplicate job_id {job_id!r}",
                    job_id=job_id,
                    field="job_id",
                    detail={"record_index": index},
                )
            )
            continue
        try:
            job = Job.from_dict(raw)
        except DomainError as exc:
            issue = exc.as_issue()
            issue.detail = dict(issue.detail) | {"record_index": index}
            issues.append(issue)
            if isinstance(job_id, str):
                seen.add(job_id)
            continue
        seen.add(job.job_id)
        jobs.append(job)
    return jobs, issues


def validate_config(
    budget: Budget,
    scheduler_config: SchedulerConfig,
    tolerance: ToleranceConfig,
) -> list[BusinessIssue]:
    issues: list[BusinessIssue] = []
    if scheduler_config.max_workers_per_task > budget.worker_slots:
        issues.append(
            BusinessIssue(
                code=ErrorCode.E_INVALID_WORKER_CONFIG,
                message="max_workers_per_task exceeds the global worker_slots budget",
                field="max_workers_per_task",
                detail={
                    "max_workers_per_task": scheduler_config.max_workers_per_task,
                    "worker_slots": budget.worker_slots,
                },
            )
        )
    return issues

