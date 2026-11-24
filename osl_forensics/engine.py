"""Concurrent file processing with deterministic admission and budgets."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .checkpoint import CheckpointManager
from .codes import Code
from .ledger import Ledger
from .models import Diagnostic, ParseResult
from .parser import parse_osl
from .policy import Policy
from .storage import atomic_write_json


@dataclass(frozen=True)
class BudgetConfig:
    concurrency: int = 2
    timeout_seconds: float = 5.0
    max_queue: int = 128
    max_expanded_bytes: int | None = None


@dataclass
class ProcessJob:
    file_id: str
    data: bytes | None = None
    path: str | Path | None = None
    revision_id: str | None = None
    arrival_index: int | None = None


@dataclass
class BatchOutcome:
    report: dict[str, Any]
    results: list[ParseResult]
    rejections: list[dict[str, Any]]


def process_files(
    jobs: Iterable[ProcessJob | dict[str, Any] | str | Path],
    *,
    policy: Policy | None = None,
    budget: BudgetConfig | None = None,
    ledger: Ledger | None = None,
    checkpoint_manager: CheckpointManager | None = None,
    report_path: str | Path | None = None,
    run_id_prefix: str = "run",
) -> BatchOutcome:
    used_policy = policy or Policy()
    used_budget = budget or BudgetConfig()
    normalized_jobs = [_coerce_job(job, index) for index, job in enumerate(jobs)]

    # Admission is sorted by stable identity, not wall-clock arrival, so queue
    # rejection records are identical under every submission permutation.
    admission_order = sorted(
        normalized_jobs,
        key=lambda job: (
            job.file_id,
            job.revision_id or "",
            str(job.path or ""),
            job.arrival_index if job.arrival_index is not None else 0,
        ),
    )
    capacity = max(0, int(used_budget.max_queue))
    accepted = admission_order[:capacity]
    overflow = admission_order[capacity:]
    rejections = [
        {
            "file_id": job.file_id,
            "revision_id": job.revision_id,
            "code": Code.E_QUEUE_FULL,
            "status": "rejected",
            "queue_capacity": capacity,
            "reason": "stable admission order exceeds configured pending queue budget",
        }
        for job in sorted(overflow, key=lambda job: job.file_id)
    ]

    if used_budget.concurrency <= 1 or len(accepted) <= 1:
        results = [_execute_job(job, used_policy, used_budget) for job in accepted]
    else:
        workers = min(int(used_budget.concurrency), len(accepted))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_index = {
                executor.submit(_execute_job, job, used_policy, used_budget): index
                for index, job in enumerate(accepted)
            }
            ordered: list[ParseResult | None] = [None] * len(accepted)
            for future, index in future_to_index.items():
                ordered[index] = future.result()
            results = [result for result in ordered if result is not None]

    ledger_summaries: list[dict[str, Any]] = []
    completed_ids: list[str] = []
    for job, result in zip(accepted, results):
        run_id = f"{run_id_prefix}:{job.file_id}:{job.revision_id or 'base'}"
        if ledger is not None:
            summary = ledger.apply_result(result, run_id=run_id, revision_id=job.revision_id)
            ledger_summaries.append(summary)
            crash_file = os.environ.get("OSL_CRASH_AFTER_LEDGER_BEFORE_CHECKPOINT")
            if crash_file and job.file_id == crash_file:
                # Test-only hard termination point after SQLite commit and
                # before the next complete checkpoint is published.
                os._exit(137)
        completed_ids.append(job.file_id)
        if checkpoint_manager is not None and ledger is not None:
            checkpoint_manager.save(ledger, completed_ids)

    counts: dict[str, int] = {}
    for result in results:
        counts[result.primary_code] = counts.get(result.primary_code, 0) + 1
    for rejection in rejections:
        counts[rejection["code"]] = counts.get(rejection["code"], 0) + 1

    report = {
        "kind": "osl-batch-report",
        "report_version": 1,
        "budget": asdict(used_budget),
        "total_jobs": len(normalized_jobs),
        "accepted_jobs": len(accepted),
        "rejected_jobs": len(rejections),
        "counts": dict(sorted(counts.items())),
        "rejections": rejections,
        "ledger": {
            "enabled": ledger is not None,
            "summaries": ledger_summaries,
            "replay": ledger.verify_replay()["ok"] if ledger is not None else None,
        },
        "results": [result.to_dict() for result in results],
    }
    if report_path is not None:
        atomic_write_json(report_path, report)
    return BatchOutcome(report=report, results=results, rejections=rejections)


def _coerce_job(job: ProcessJob | dict[str, Any] | str | Path, index: int) -> ProcessJob:
    if isinstance(job, ProcessJob):
        if job.arrival_index is None:
            job.arrival_index = index
        return job
    if isinstance(job, (str, Path)):
        path = Path(job)
        return ProcessJob(file_id=path.stem, path=path, arrival_index=index)
    return ProcessJob(
        file_id=str(job["file_id"]),
        data=job.get("data"),
        path=job.get("path"),
        revision_id=job.get("revision_id"),
        arrival_index=job.get("arrival_index", index),
    )


def _execute_job(
    job: ProcessJob, policy: Policy, budget: BudgetConfig
) -> ParseResult:
    start = time.perf_counter()
    execution_policy = policy
    if budget.max_expanded_bytes is not None:
        execution_policy = policy.with_changes(
            max_expanded_bytes=budget.max_expanded_bytes
        )
    try:
        try:
            if job.data is not None:
                data = job.data
            elif job.path is not None:
                data = Path(job.path).read_bytes()
            else:
                raise FileNotFoundError("job has neither data nor path")
        except OSError as exc:
            result = ParseResult(
                file_id=job.file_id,
                ok=False,
                policy_fingerprint=policy.fingerprint(),
                header_layout=policy.header_layout.name,
                header_size=policy.header_layout.size,
                diagnostics=[
                    Diagnostic(
                        Code.E_HEADER,
                        f"sample could not be read: {exc}",
                        None,
                        {"path": str(job.path)},
                    )
                ],
            )
            result.elapsed_seconds = time.perf_counter() - start
            return result

        result = parse_osl(
            data,
            file_id=job.file_id,
            policy=execution_policy,
            timeout_seconds=budget.timeout_seconds,
        )
    except Exception as exc:  # keep one malformed/unexpected job from killing a batch
        result = ParseResult(
            file_id=job.file_id,
            ok=False,
            policy_fingerprint=policy.fingerprint(),
            header_layout=policy.header_layout.name,
            header_size=policy.header_layout.size,
            diagnostics=[
                Diagnostic(
                    Code.E_INTERNAL,
                    f"unexpected parser failure: {type(exc).__name__}: {exc}",
                    None,
                )
            ],
        )
        result.elapsed_seconds = time.perf_counter() - start
        return result

    elapsed = time.perf_counter() - start
    result.elapsed_seconds = elapsed
    if elapsed > budget.timeout_seconds and not any(
        diagnostic.code == Code.E_TIMEOUT for diagnostic in result.diagnostics
    ):
        result.diagnostics.append(
            Diagnostic(
                Code.E_TIMEOUT,
                f"measured total wall-clock {elapsed:.6f}s exceeds budget",
                0,
                elapsed_seconds=elapsed,
                timeout_seconds=budget.timeout_seconds,
            )
        )
        result.ok = False
    return result
