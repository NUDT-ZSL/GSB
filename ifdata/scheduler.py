"""Deterministic batch admission, worker selection and parallel execution."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Iterable

from .errors import (
    BusinessError,
    E_COMPARE_BUDGET,
    E_CONCURRENCY_LIMIT,
    E_INVALID_INPUT,
    E_MEMORY_LIMIT,
    E_WORKER_FAILURE,
)
from .types import AdmissionPlan, BatchEstimate, BudgetLimits


DEFAULT_ERROR_PRIORITY = (
    E_CONCURRENCY_LIMIT,
    E_COMPARE_BUDGET,
    E_MEMORY_LIMIT,
)


def ordered_primary_code(
    violations: Iterable[str],
    priority: tuple[str, ...] | list[str] = DEFAULT_ERROR_PRIORITY,
) -> str | None:
    present = set(violations)
    for code in priority:
        if code in present:
            return code
    return next(iter(present), None)


def evaluate_batch(
    batch: BatchEstimate,
    *,
    comparisons_used: int,
    peak_memory_bytes: int,
    limits: BudgetLimits,
) -> list[str]:
    violations: list[str] = []
    if batch.requested_workers > limits.max_workers:
        violations.append(E_CONCURRENCY_LIMIT)
    if comparisons_used + batch.comparisons > limits.compare_budget:
        violations.append(E_COMPARE_BUDGET)
    if max(peak_memory_bytes, batch.memory_bytes) > limits.memory_limit_bytes:
        violations.append(E_MEMORY_LIMIT)
    return violations


def plan_admission(
    batches: Iterable[BatchEstimate],
    limits: BudgetLimits,
) -> AdmissionPlan:
    """Build an admission set while retaining explicit rejection attribution."""

    plan = AdmissionPlan()
    for batch in batches:
        violations = evaluate_batch(
            batch,
            comparisons_used=plan.comparisons_used,
            peak_memory_bytes=plan.peak_memory_bytes,
            limits=limits,
        )
        if violations:
            code = ordered_primary_code(violations, limits.error_priority)
            plan.rejected.append(
                {
                    "batch_id": batch.batch_id,
                    "sample_count": batch.sample_count,
                    "code": code,
                    "all_violations": violations,
                    "budgets": {
                        "max_workers": limits.max_workers,
                        "compare_budget": limits.compare_budget,
                        "memory_limit_bytes": limits.memory_limit_bytes,
                        "estimated_comparisons": batch.comparisons,
                        "comparisons_already_used": plan.comparisons_used,
                        "estimated_memory_bytes": batch.memory_bytes,
                        "peak_memory_bytes": plan.peak_memory_bytes,
                    },
                }
            )
            continue
        plan.admitted.append(batch)
        plan.comparisons_used += batch.comparisons
        plan.peak_memory_bytes = max(
            plan.peak_memory_bytes, batch.memory_bytes
        )
    return plan


def admit_batch_or_raise(
    batch: BatchEstimate,
    *,
    comparisons_used: int,
    peak_memory_bytes: int,
    limits: BudgetLimits,
    stage: str,
) -> None:
    violations = evaluate_batch(
        batch,
        comparisons_used=comparisons_used,
        peak_memory_bytes=peak_memory_bytes,
        limits=limits,
    )
    if not violations:
        return
    code = ordered_primary_code(violations, limits.error_priority)
    messages = {
        E_CONCURRENCY_LIMIT: "worker count exceeds the configured hard limit",
        E_COMPARE_BUDGET: "candidate comparisons exceed the configured budget",
        E_MEMORY_LIMIT: "resident sample memory exceeds the configured limit",
    }
    raise BusinessError(
        code or E_WORKER_FAILURE,
        messages.get(code or "", "batch admission failed"),
        stage=stage,
        batch_id=batch.batch_id,
        details={
            "all_violations": violations,
            "batch": {
                "sample_count": batch.sample_count,
                "comparisons": batch.comparisons,
                "memory_bytes": batch.memory_bytes,
                "requested_workers": batch.requested_workers,
            },
            "budgets": {
                "max_workers": limits.max_workers,
                "compare_budget": limits.compare_budget,
                "memory_limit_bytes": limits.memory_limit_bytes,
                "comparisons_used": comparisons_used,
                "peak_memory_bytes": peak_memory_bytes,
            },
        },
    )


def select_worker_count(
    *,
    requested_workers: int,
    limits: BudgetLimits,
    base_memory_bytes: int,
    per_worker_memory_bytes: int,
    stage: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Choose the largest safe worker count and record rejected options."""

    if requested_workers < 1:
        raise BusinessError(
            E_INVALID_INPUT,
            "requested_workers must be at least 1",
            stage=stage,
        )
    if requested_workers > limits.max_workers:
        raise BusinessError(
            E_CONCURRENCY_LIMIT,
            "requested worker count exceeds the configured hard limit",
            stage=stage,
            details={
                "requested_workers": requested_workers,
                "max_workers": limits.max_workers,
            },
        )
    decisions: list[dict[str, Any]] = []
    selected: int | None = None
    for workers in range(requested_workers, 0, -1):
        estimated = base_memory_bytes + workers * per_worker_memory_bytes
        if estimated <= limits.memory_limit_bytes:
            selected = workers
            break
        decisions.append(
            {
                "decision": "worker_count",
                "candidate_workers": workers,
                "status": "rejected",
                "code": E_MEMORY_LIMIT,
                "reason": (
                    "higher worker count would exceed the resident sample "
                    "memory limit"
                ),
                "budgets": {
                    "estimated_memory_bytes": estimated,
                    "memory_limit_bytes": limits.memory_limit_bytes,
                    "max_workers": limits.max_workers,
                },
            }
        )
    if selected is None:
        estimated = base_memory_bytes + per_worker_memory_bytes
        raise BusinessError(
            E_MEMORY_LIMIT,
            "even one worker cannot fit within the resident memory limit",
            stage=stage,
            details={
                "estimated_memory_bytes": estimated,
                "memory_limit_bytes": limits.memory_limit_bytes,
            },
        )
    if selected != requested_workers:
        decisions.insert(
            0,
            {
                "decision": "worker_count",
                "candidate_workers": selected,
                "status": "accepted",
                "reason": "selected highest worker count that fits memory",
                "budgets": {
                    "estimated_memory_bytes": base_memory_bytes
                    + selected * per_worker_memory_bytes,
                    "memory_limit_bytes": limits.memory_limit_bytes,
                },
            },
        )
    return selected, decisions


def _run_one_batch(
    stage: str,
    batch_id: str,
    worker_fn: Callable[[list[Any]], list[Any]],
    items: list[Any],
) -> list[Any]:
    try:
        return worker_fn(items)
    except BusinessError as exc:
        raise exc.with_context(stage=stage, batch_id=batch_id)
    except Exception as exc:  # noqa: BLE001 - intentionally attributed
        raise BusinessError(
            E_WORKER_FAILURE,
            f"worker failed: {type(exc).__name__}: {exc}",
            stage=stage,
            batch_id=batch_id,
            details={"exception_type": type(exc).__name__},
        ) from exc


def run_worker_batches(
    items: list[Any],
    worker_fn: Callable[[list[Any]], list[Any]],
    *,
    stage: str,
    batch_size: int,
    workers: int,
    failpoints: dict[str, str] | None = None,
) -> list[Any]:
    """Run batches concurrently, preserving input batch order in the output."""

    if not items:
        return []
    batches = [
        (f"{stage}-batch-{index:04d}", items[start : start + batch_size])
        for index, start in enumerate(range(0, len(items), batch_size), start=1)
    ]
    failpoints = failpoints or {}
    futures: list[tuple[str, Future[list[Any]]]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=stage) as pool:
        for batch_id, batch_items in batches:
            suffix = batch_id.rsplit("-", 1)[-1]
            numeric = str(int(suffix))
            keys = {f"{stage}:{suffix}", f"{stage}:{numeric}"}
            matched = next(
                (key for key in keys if key in failpoints),
                None,
            )
            if matched is not None:
                raise BusinessError(
                    E_WORKER_FAILURE,
                    failpoints[matched],
                    stage=stage,
                    batch_id=batch_id,
                    details={"injected": True},
                )
            future = pool.submit(
                _run_one_batch,
                stage,
                batch_id,
                worker_fn,
                batch_items,
            )
            futures.append((batch_id, future))
        output: list[Any] = []
        for batch_id, future in futures:
            try:
                output.extend(future.result())
            except BusinessError as exc:
                raise exc.with_context(stage=stage, batch_id=batch_id)
    return output

