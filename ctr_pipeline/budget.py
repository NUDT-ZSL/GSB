from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .errors import (
    E_COMPUTE_BUDGET,
    E_CONFIG_INVALID,
    E_MEMORY_LIMIT,
    PipelineError,
)
from .models import BudgetConfig, BudgetDecision, ExpandedSample, TrainingChunk, TrainingConfig
from .scheduling import (
    batches_from_chunks,
    candidate_chunk_sizes,
    candidate_worker_counts,
    make_training_chunks,
)


PRIORITY_RULES: tuple[str, ...] = (
    "P1 preserve label identity, admitted-sample order, and per-sample scoring semantics",
    "P2 never exceed the resident-memory budget; shrink chunks or reject whole trailing chunks first",
    "P3 maximize the admitted sample prefix before reducing workers or increasing reduction overhead",
    "P4 among equal admitted sets, prefer the largest chunk that fits, then the most workers",
    "P5 do not shrink the hashed feature dimension by default because it silently removes cross-term capacity",
)


@dataclass(frozen=True)
class _Candidate:
    chunk_size: int
    workers: int
    admitted_chunk_count: int
    admitted_sample_count: int
    admitted_nnz: int
    estimated_macs: int
    estimated_memory_bytes: int
    next_chunk_blockers: tuple[str, ...]


def _config_error(field: str, message: str, **extra: object) -> PipelineError:
    return PipelineError(
        E_CONFIG_INVALID,
        "budget_config",
        message,
        {"field": field, **extra},
    )


def validate_budget_config(config: BudgetConfig) -> None:
    positive_fields = {
        "total_mac_budget": config.total_mac_budget,
        "memory_budget_bytes": config.memory_budget_bytes,
        "worker_memory_bytes": config.worker_memory_bytes,
        "sample_base_bytes": config.sample_base_bytes,
        "nnz_bytes": config.nnz_bytes,
        "score_bytes": config.score_bytes,
        "chunk_overhead_bytes": config.chunk_overhead_bytes,
    }
    for field, value in positive_fields.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _config_error(field, f"{field} must be a positive integer", actual=value)
    if (
        isinstance(config.worker_overhead_macs_per_round, bool)
        or not isinstance(config.worker_overhead_macs_per_round, int)
        or config.worker_overhead_macs_per_round < 0
    ):
        raise _config_error(
            "worker_overhead_macs_per_round",
            "worker_overhead_macs_per_round must be a non-negative integer",
            actual=config.worker_overhead_macs_per_round,
        )
    if not isinstance(config.allow_degraded, bool):
        raise _config_error(
            "allow_degraded",
            "allow_degraded must be a boolean",
            actual=config.allow_degraded,
        )


def _estimate_macs(
    *,
    sample_count: int,
    nnz: int,
    chunk_count: int,
    feature_dimension: int,
    rounds: int,
    workers: int,
    budget_config: BudgetConfig,
) -> int:
    sample_ops = rounds * (2 * nnz + 4 * sample_count)
    reduction_ops = rounds * max(0, chunk_count - 1) * (feature_dimension + 2)
    update_ops = rounds * (feature_dimension + 1)
    worker_ops = rounds * workers * budget_config.worker_overhead_macs_per_round
    return sample_ops + reduction_ops + update_ops + worker_ops


def _estimate_memory(
    *,
    sample_count: int,
    nnz: int,
    chunk_count: int,
    chunk_size: int,
    feature_dimension: int,
    workers: int,
    budget_config: BudgetConfig,
) -> int:
    sample_memory = (
        sample_count
        * (budget_config.sample_base_bytes + budget_config.score_bytes)
        + nnz * budget_config.nnz_bytes
    )
    parameter_memory = (feature_dimension + 1) * 16
    worker_memory = workers * budget_config.worker_memory_bytes
    chunk_memory = chunk_count * budget_config.chunk_overhead_bytes + chunk_size * 64
    return sample_memory + parameter_memory + worker_memory + chunk_memory


def _evaluate_candidate(
    samples: Sequence[ExpandedSample],
    *,
    chunk_size: int,
    workers: int,
    feature_dimension: int,
    training_config: TrainingConfig,
    budget_config: BudgetConfig,
) -> _Candidate:
    chunks = make_training_chunks(
        samples,
        chunk_size=chunk_size,
        batch_sample_size=training_config.batch_sample_size,
    )
    sample_count = 0
    nnz = 0
    admitted_chunks = 0
    blockers: tuple[str, ...] = ()
    for chunk in chunks:
        next_sample_count = sample_count + chunk.sample_count
        next_nnz = nnz + sum(sample.nnz for sample in chunk.samples)
        next_chunk_count = admitted_chunks + 1
        next_macs = _estimate_macs(
            sample_count=next_sample_count,
            nnz=next_nnz,
            chunk_count=next_chunk_count,
            feature_dimension=feature_dimension,
            rounds=training_config.max_rounds,
            workers=workers,
            budget_config=budget_config,
        )
        next_memory = _estimate_memory(
            sample_count=next_sample_count,
            nnz=next_nnz,
            chunk_count=next_chunk_count,
            chunk_size=chunk_size,
            feature_dimension=feature_dimension,
            workers=workers,
            budget_config=budget_config,
        )
        current_blockers: list[str] = []
        if next_macs > budget_config.total_mac_budget:
            current_blockers.append("compute")
        if next_memory > budget_config.memory_budget_bytes:
            current_blockers.append("memory")
        if current_blockers:
            blockers = tuple(current_blockers)
            break
        sample_count = next_sample_count
        nnz = next_nnz
        admitted_chunks = next_chunk_count

    macs = _estimate_macs(
        sample_count=sample_count,
        nnz=nnz,
        chunk_count=admitted_chunks,
        feature_dimension=feature_dimension,
        rounds=training_config.max_rounds,
        workers=workers,
        budget_config=budget_config,
    )
    memory = _estimate_memory(
        sample_count=sample_count,
        nnz=nnz,
        chunk_count=admitted_chunks,
        chunk_size=chunk_size,
        feature_dimension=feature_dimension,
        workers=workers,
        budget_config=budget_config,
    )
    return _Candidate(
        chunk_size=chunk_size,
        workers=workers,
        admitted_chunk_count=admitted_chunks,
        admitted_sample_count=sample_count,
        admitted_nnz=nnz,
        estimated_macs=macs,
        estimated_memory_bytes=memory,
        next_chunk_blockers=blockers,
    )


def _candidate_key(candidate: _Candidate) -> tuple[int, int, int, int, int]:
    return (
        candidate.admitted_sample_count,
        candidate.chunk_size,
        candidate.workers,
        -candidate.estimated_memory_bytes,
        -candidate.estimated_macs,
    )


def _batch_route_rows(
    admitted_chunks: Sequence[TrainingChunk],
    *,
    feature_dimension: int,
    training_config: TrainingConfig,
    budget_config: BudgetConfig,
    restricted: bool,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for batch_index, batch in enumerate(batches_from_chunks(admitted_chunks)):
        sample_count = sum(chunk.sample_count for chunk in batch)
        nnz = sum(
            sample.nnz for chunk in batch for sample in chunk.samples
        )
        macs = _estimate_macs(
            sample_count=sample_count,
            nnz=nnz,
            chunk_count=len(batch),
            feature_dimension=feature_dimension,
            rounds=training_config.max_rounds,
            workers=1,
            budget_config=budget_config,
        )
        memory = _estimate_memory(
            sample_count=sample_count,
            nnz=nnz,
            chunk_count=len(batch),
            chunk_size=max(chunk.sample_count for chunk in batch),
            feature_dimension=feature_dimension,
            workers=1,
            budget_config=budget_config,
        )
        rows.append(
            {
                "batch_index": batch_index,
                "chunk_ids": [chunk.chunk_id for chunk in batch],
                "chunk_count": len(batch),
                "estimated_batch_macs": macs,
                "estimated_batch_memory_bytes": memory,
                "path": "restricted" if restricted else "full",
                "sample_count": sample_count,
                "sample_nnz": nnz,
            }
        )
    return rows


def plan_budget(
    samples: Sequence[ExpandedSample],
    *,
    feature_dimension: int,
    training_config: TrainingConfig,
    budget_config: BudgetConfig,
) -> BudgetDecision:
    validate_budget_config(budget_config)
    ordered = sorted(samples, key=lambda sample: sample.sample_id)
    if not ordered:
        raise _config_error("sample_count", "at least one sample is required")

    candidates: list[_Candidate] = []
    for chunk_size in candidate_chunk_sizes(training_config.chunk_size):
        for workers in candidate_worker_counts(training_config.workers):
            candidates.append(
                _evaluate_candidate(
                    ordered,
                    chunk_size=chunk_size,
                    workers=workers,
                    feature_dimension=feature_dimension,
                    training_config=training_config,
                    budget_config=budget_config,
                )
            )
    selected = max(candidates, key=_candidate_key)
    requested = next(
        candidate
        for candidate in candidates
        if candidate.chunk_size == training_config.chunk_size
        and candidate.workers == training_config.workers
    )

    admitted_ids = tuple(
        sample.sample_id for sample in ordered[: selected.admitted_sample_count]
    )
    rejected_ids = tuple(
        sample.sample_id for sample in ordered[selected.admitted_sample_count :]
    )
    admitted_samples = ordered[: selected.admitted_sample_count]
    effective_chunks = make_training_chunks(
        admitted_samples,
        chunk_size=selected.chunk_size,
        batch_sample_size=training_config.batch_sample_size,
    )

    requested_fully_admitted = requested.admitted_sample_count == len(ordered)
    selected_fully_admitted = selected.admitted_sample_count == len(ordered)
    changed = (
        selected.chunk_size != training_config.chunk_size
        or selected.workers != training_config.workers
        or not selected_fully_admitted
    )

    degrade_categories: list[str] = []
    if changed:
        requested_blockers = set(requested.next_chunk_blockers)
        selected_blockers = set(selected.next_chunk_blockers)
        all_blockers = requested_blockers | selected_blockers
        if "memory" in all_blockers:
            degrade_categories.append(E_MEMORY_LIMIT)
        if "compute" in all_blockers:
            degrade_categories.append(E_COMPUTE_BUDGET)
        if not degrade_categories:
            if selected.workers != training_config.workers:
                degrade_categories.append(E_COMPUTE_BUDGET)
            else:
                degrade_categories.append(E_MEMORY_LIMIT)

    error: dict[str, object] | None = None
    if degrade_categories:
        primary = degrade_categories[0]
        first_rejected = rejected_ids[0] if rejected_ids else None
        error = {
            "code": primary,
            "stage": "budget_admission",
            "message": (
                "admission was restricted by the selected resource budget"
                if rejected_ids
                else "requested chunk/worker settings were reduced to satisfy the budget"
            ),
            "details": {
                "blocking_constraints": sorted(
                    set(requested.next_chunk_blockers) | set(selected.next_chunk_blockers)
                ),
                "degraded_categories": degrade_categories,
                "effective_chunk_size": selected.chunk_size,
                "effective_workers": selected.workers,
                "first_rejected_sample_id": first_rejected,
                "rejected_sample_count": len(rejected_ids),
                "requested_chunk_size": training_config.chunk_size,
                "requested_workers": training_config.workers,
            },
        }

    if changed and not budget_config.allow_degraded:
        assert error is not None
        raise PipelineError(
            str(error["code"]),
            str(error["stage"]),
            str(error["message"]),
            dict(error["details"]),  # type: ignore[arg-type]
        )

    reductions: dict[str, object] = {
        "chunk_size": {
            "changed": selected.chunk_size != training_config.chunk_size,
            "effective": selected.chunk_size,
            "reason": (
                "larger chunks exceeded memory or reduced the admitted prefix"
                if selected.chunk_size != training_config.chunk_size
                else "requested chunk size retained"
            ),
            "requested": training_config.chunk_size,
        },
        "workers": {
            "changed": selected.workers != training_config.workers,
            "effective": selected.workers,
            "reason": (
                "worker count was reduced to lower shared MAC overhead or worker scratch memory"
                if selected.workers != training_config.workers
                else "requested worker count retained"
            ),
            "requested": training_config.workers,
        },
        "feature_dimension": {
            "applied": False,
            "effective": feature_dimension,
            "reason": (
                "feature projection is intentionally not applied because it would "
                "silently remove hashed cross-term capacity"
            ),
            "requested": feature_dimension,
        },
    }
    concessions: list[dict[str, object]] = []
    for name in ("chunk_size", "workers"):
        detail = reductions[name]
        if detail["changed"]:  # type: ignore[index]
            concessions.append(
                {
                    "dimension": name,
                    "effective": detail["effective"],  # type: ignore[index]
                    "reason": detail["reason"],  # type: ignore[index]
                    "requested": detail["requested"],  # type: ignore[index]
                }
            )
    if rejected_ids:
        concessions.append(
            {
                "dimension": "admitted_samples",
                "effective": len(admitted_ids),
                "reason": "whole trailing chunks were rejected to stay within resource budgets",
                "requested": len(ordered),
            }
        )

    report = {
        "admission_objective": (
            "maximize admitted samples; then maximize chunk size; then maximize workers"
        ),
        "batch_routes": _batch_route_rows(
            effective_chunks,
            feature_dimension=feature_dimension,
            training_config=training_config,
            budget_config=budget_config,
            restricted=changed,
        ),
        "candidate_count": len(candidates),
        "concessions": concessions,
        "feature_projection": reductions["feature_dimension"],
        "priority_rules": list(PRIORITY_RULES),
        "requested": {
            "admitted_sample_count": requested.admitted_sample_count,
            "chunk_size": training_config.chunk_size,
            "estimated_macs": requested.estimated_macs,
            "estimated_memory_bytes": requested.estimated_memory_bytes,
            "fully_admitted": requested_fully_admitted,
            "next_chunk_blockers": list(requested.next_chunk_blockers),
            "workers": training_config.workers,
        },
        "selected": {
            "admitted_chunk_count": selected.admitted_chunk_count,
            "admitted_sample_count": selected.admitted_sample_count,
            "chunk_size": selected.chunk_size,
            "estimated_macs": selected.estimated_macs,
            "estimated_memory_bytes": selected.estimated_memory_bytes,
            "fully_admitted": selected_fully_admitted,
            "next_chunk_blockers": list(selected.next_chunk_blockers),
            "workers": selected.workers,
        },
    }
    return BudgetDecision(
        requested_chunk_size=training_config.chunk_size,
        effective_chunk_size=selected.chunk_size,
        requested_workers=training_config.workers,
        effective_workers=selected.workers,
        admitted_sample_ids=admitted_ids,
        rejected_sample_ids=rejected_ids,
        admitted_chunk_count=selected.admitted_chunk_count,
        total_chunk_count=math.ceil(len(ordered) / selected.chunk_size),
        estimated_macs=selected.estimated_macs,
        estimated_memory_bytes=selected.estimated_memory_bytes,
        degraded=bool(degrade_categories),
        degrade_categories=tuple(degrade_categories),
        error=error,
        report=report,
    )
