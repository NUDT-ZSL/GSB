"""Deterministic block-size, memory, and comparison-cost planning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .config import RunConfig
from .errors import E_RESOURCE_LIMIT, EngineError
from .util import sha256_value


@dataclass(frozen=True)
class ChunkPlan:
    n_samples: int
    n_features: int
    precision_mode: str
    bytes_per_value: int
    requested_block_size: int
    block_size: int
    workers: int
    min_block_size: int
    reduction_order: str
    dispatch_order: str
    small_block_downgrade: bool
    worker_downgrade: bool
    degradation_categories: tuple[str, ...]
    estimated_resident_bytes: int
    memory_budget_bytes: int
    estimated_comparisons: int
    comparison_budget: int
    decision_basis: tuple[dict[str, Any], ...]
    decision_checksum: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChunkPlan":
        return cls(
            n_samples=int(data["n_samples"]),
            n_features=int(data["n_features"]),
            precision_mode=str(data["precision_mode"]),
            bytes_per_value=int(data["bytes_per_value"]),
            requested_block_size=int(data["requested_block_size"]),
            block_size=int(data["block_size"]),
            workers=int(data["workers"]),
            min_block_size=int(data["min_block_size"]),
            reduction_order=str(data["reduction_order"]),
            dispatch_order=str(data["dispatch_order"]),
            small_block_downgrade=bool(data["small_block_downgrade"]),
            worker_downgrade=bool(data["worker_downgrade"]),
            degradation_categories=tuple(data["degradation_categories"]),
            estimated_resident_bytes=int(data["estimated_resident_bytes"]),
            memory_budget_bytes=int(data["memory_budget_bytes"]),
            estimated_comparisons=int(data["estimated_comparisons"]),
            comparison_budget=int(data["comparison_budget"]),
            decision_basis=tuple(dict(item) for item in data["decision_basis"]),
            decision_checksum=str(data["decision_checksum"]),
        )

    @property
    def block_count(self) -> int:
        return (self.n_samples + self.block_size - 1) // self.block_size

    def boundaries(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (start, min(start + self.block_size, self.n_samples))
            for start in range(0, self.n_samples, self.block_size)
        )

    def to_dict(self) -> dict[str, Any]:
        boundaries = self.boundaries()
        covered = sum(end - start for start, end in boundaries)
        return {
            "n_samples": self.n_samples,
            "n_features": self.n_features,
            "precision_mode": self.precision_mode,
            "bytes_per_value": self.bytes_per_value,
            "requested_block_size": self.requested_block_size,
            "block_size": self.block_size,
            "block_count": len(boundaries),
            "workers": self.workers,
            "min_block_size": self.min_block_size,
            "reduction_order": self.reduction_order,
            "dispatch_order": self.dispatch_order,
            "small_block_downgrade": self.small_block_downgrade,
            "worker_downgrade": self.worker_downgrade,
            "degradation_categories": list(self.degradation_categories),
            "estimated_resident_bytes": self.estimated_resident_bytes,
            "memory_budget_bytes": self.memory_budget_bytes,
            "estimated_comparisons": self.estimated_comparisons,
            "comparison_budget": self.comparison_budget,
            "decision_basis": [dict(item) for item in self.decision_basis],
            "boundaries": [[start, end] for start, end in boundaries],
            "coverage": {
                "covered_rows": covered,
                "complete": covered == self.n_samples,
                "contiguous": all(
                    boundaries[index][0] == (0 if index == 0 else boundaries[index - 1][1])
                    for index in range(len(boundaries))
                ),
                "disjoint": all(
                    boundaries[index][0] < boundaries[index][1] for index in range(len(boundaries))
                ),
            },
            "decision_checksum": self.decision_checksum,
        }


def _comparison_estimate(
    *,
    n_samples: int,
    n_features: int,
    block_count: int,
    iteration_counts_ascending: tuple[int, ...],
) -> int:
    gradient_comparisons_per_iteration = (
        n_samples * (n_features + 2)
        + block_count * n_features
        + (n_features + 1)
    )
    gradient_total = sum(iteration_counts_ascending) * gradient_comparisons_per_iteration
    final_evaluation_total = len(iteration_counts_ascending) * n_samples * (n_features + 2)
    return int(gradient_total + final_evaluation_total)


def _resident_estimate(
    *,
    n_samples: int,
    n_features: int,
    block_size: int,
    workers: int,
    bytes_per_value: int,
) -> int:
    # Raw/preprocessed float64 data is retained once. Each spawned worker owns an
    # encoded matrix and a bounded block workspace.
    global_arrays = n_samples * (n_features + 2) * 8
    worker_arrays = workers * n_samples * (n_features + 2) * bytes_per_value
    worker_workspace = workers * block_size * (n_features + 8) * bytes_per_value
    return int(global_arrays + worker_arrays + worker_workspace)


def _resource_error(message: str, **details: Any) -> EngineError:
    return EngineError(
        E_RESOURCE_LIMIT,
        message,
        stage="chunk_planning",
        details=details,
    )


def plan_chunks(
    config: RunConfig,
    *,
    n_samples: int,
    n_features: int,
    iteration_counts_ascending: tuple[int, ...],
    comparisons_already_used: int = 0,
) -> ChunkPlan:
    if n_samples <= 0 or n_features <= 0:
        raise _resource_error(
            "chunk planning requires positive sample and feature counts",
            n_samples=n_samples,
            n_features=n_features,
        )
    execution = config.execution
    if comparisons_already_used < 0 or comparisons_already_used > execution.comparison_budget:
        raise _resource_error(
            "comparisons_already_used must be within the configured comparison budget",
            comparisons_already_used=comparisons_already_used,
            comparison_budget=execution.comparison_budget,
        )
    remaining_comparison_budget = execution.comparison_budget - comparisons_already_used
    requested_block_size = min(execution.block_size, n_samples)
    min_block_size = min(execution.min_block_size, requested_block_size)
    workers = execution.workers
    block_size = requested_block_size
    degradation_categories: list[str] = []
    basis: list[dict[str, Any]] = [
        {
            "factor": "input_scale",
            "n_samples": n_samples,
            "n_features": n_features,
            "requested_block_size": execution.block_size,
            "requested_workers": workers,
            "comparisons_already_used": comparisons_already_used,
            "remaining_comparison_budget": remaining_comparison_budget,
        },
        {
            "factor": "precision_width",
            "precision_mode": config.precision.mode,
            "bytes_per_value": config.precision.bytes_per_value,
        },
    ]

    initial_blocks = math.ceil(n_samples / block_size)
    initial_resident = _resident_estimate(
        n_samples=n_samples,
        n_features=n_features,
        block_size=block_size,
        workers=workers,
        bytes_per_value=config.precision.bytes_per_value,
    )
    initial_comparisons = _comparison_estimate(
        n_samples=n_samples,
        n_features=n_features,
        block_count=initial_blocks,
        iteration_counts_ascending=iteration_counts_ascending,
    )
    basis.append(
        {
            "factor": "initial_estimate",
            "block_size": block_size,
            "block_count": initial_blocks,
            "workers": workers,
            "estimated_resident_bytes": initial_resident,
            "estimated_comparisons": initial_comparisons,
        }
    )
    if initial_comparisons > remaining_comparison_budget:
        raise _resource_error(
            "estimated comparison count exceeds the configured budget",
            estimated_comparisons=initial_comparisons,
            comparison_budget=execution.comparison_budget,
            comparisons_already_used=comparisons_already_used,
            remaining_comparison_budget=remaining_comparison_budget,
        )

    if initial_resident > execution.memory_budget_bytes and execution.allow_worker_downgrade:
        original_workers = workers
        while (
            workers > 1
            and _resident_estimate(
                n_samples=n_samples,
                n_features=n_features,
                block_size=block_size,
                workers=workers,
                bytes_per_value=config.precision.bytes_per_value,
            )
            > execution.memory_budget_bytes
        ):
            workers = max(1, workers // 2)
        if workers < original_workers:
            degradation_categories.append("workers_reduced")
            basis.append(
                {
                    "factor": "memory_guard_worker_reduction",
                    "from_workers": original_workers,
                    "to_workers": workers,
                    "reason": "estimated resident bytes exceeded memory budget",
                }
            )

    if (
        _resident_estimate(
            n_samples=n_samples,
            n_features=n_features,
            block_size=block_size,
            workers=workers,
            bytes_per_value=config.precision.bytes_per_value,
        )
        > execution.memory_budget_bytes
        and execution.allow_small_block_downgrade
    ):
        original_block_size = block_size
        while block_size > min_block_size:
            candidate = max(min_block_size, block_size // 2)
            candidate_resident = _resident_estimate(
                n_samples=n_samples,
                n_features=n_features,
                block_size=candidate,
                workers=workers,
                bytes_per_value=config.precision.bytes_per_value,
            )
            block_size = candidate
            if candidate_resident <= execution.memory_budget_bytes:
                break
        block_size = min(block_size, n_samples)
        if block_size < original_block_size:
            degradation_categories.append("small_block")
            basis.append(
                {
                    "factor": "memory_guard_small_block",
                    "from_block_size": original_block_size,
                    "to_block_size": block_size,
                    "reason": "parallel resident estimate exceeded memory budget",
                }
            )

    block_count = math.ceil(n_samples / block_size)
    resident = _resident_estimate(
        n_samples=n_samples,
        n_features=n_features,
        block_size=block_size,
        workers=workers,
        bytes_per_value=config.precision.bytes_per_value,
    )
    comparisons = _comparison_estimate(
        n_samples=n_samples,
        n_features=n_features,
        block_count=block_count,
        iteration_counts_ascending=iteration_counts_ascending,
    )
    if resident > execution.memory_budget_bytes:
        raise _resource_error(
            "estimated resident memory exceeds the configured budget after degradation",
            estimated_resident_bytes=resident,
            memory_budget_bytes=execution.memory_budget_bytes,
            degradation_categories=degradation_categories,
        )
    if comparisons > remaining_comparison_budget:
        raise _resource_error(
            "estimated comparison count exceeds the configured budget after degradation",
            estimated_comparisons=comparisons,
            comparison_budget=execution.comparison_budget,
            comparisons_already_used=comparisons_already_used,
            remaining_comparison_budget=remaining_comparison_budget,
            degradation_categories=degradation_categories,
        )
    basis.append(
        {
            "factor": "selected_plan",
            "block_size": block_size,
            "block_count": block_count,
            "workers": workers,
            "estimated_resident_bytes": resident,
            "estimated_comparisons": comparisons,
            "reduction_order": execution.reduction_order,
        }
    )
    boundaries = [
        (start, min(start + block_size, n_samples))
        for start in range(0, n_samples, block_size)
    ]
    if not boundaries:
        raise _resource_error("chunking produced no blocks", n_samples=n_samples)
    if boundaries[0][0] != 0 or boundaries[-1][1] != n_samples:
        raise _resource_error(
            "chunk boundaries do not cover all rows",
            boundaries=boundaries,
            n_samples=n_samples,
        )
    if any(start >= end for start, end in boundaries):
        raise _resource_error("chunk boundaries contain an empty block")
    if any(
        boundaries[index][0] != boundaries[index - 1][1]
        for index in range(1, len(boundaries))
    ):
        raise _resource_error("chunk boundaries overlap or leave a gap", boundaries=boundaries)

    checksum_payload = {
        "n_samples": n_samples,
        "n_features": n_features,
        "precision_mode": config.precision.mode,
        "bytes_per_value": config.precision.bytes_per_value,
        "block_size": block_size,
        "workers": workers,
        "min_block_size": min_block_size,
        "degradation_categories": degradation_categories,
        "estimated_resident_bytes": resident,
        "estimated_comparisons": comparisons,
        "boundaries": boundaries,
        "basis": basis,
    }
    return ChunkPlan(
        n_samples=n_samples,
        n_features=n_features,
        precision_mode=config.precision.mode,
        bytes_per_value=config.precision.bytes_per_value,
        requested_block_size=execution.block_size,
        block_size=block_size,
        workers=workers,
        min_block_size=min_block_size,
        reduction_order=execution.reduction_order,
        dispatch_order="block_index_ascending",
        small_block_downgrade="small_block" in degradation_categories,
        worker_downgrade="workers_reduced" in degradation_categories,
        degradation_categories=tuple(degradation_categories),
        estimated_resident_bytes=resident,
        memory_budget_bytes=execution.memory_budget_bytes,
        estimated_comparisons=comparisons,
        comparison_budget=execution.comparison_budget,
        decision_basis=tuple(basis),
        decision_checksum=sha256_value(checksum_payload),
    )

