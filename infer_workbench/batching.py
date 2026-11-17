"""Stable sample ordering and deterministic batch partitioning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .assets import Assets
from .config import BatchConfig
from .errors import WorkbenchError


@dataclass(frozen=True)
class BatchPlan:
    index: int
    batch_id: str
    sample_indices: tuple[int, ...]
    sample_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_index": self.index,
            "batch_id": self.batch_id,
            "sample_indices": list(self.sample_indices),
            "sample_ids": list(self.sample_ids),
        }


def order_sample_indices(assets: Assets, strategy: str) -> list[int]:
    rebuilt = BatchConfig.from_dict(
        {
            "batch_size": 1,
            "sort_strategy": strategy,
            "precision_mode": "float32",
            "quant_scale": 1,
            "drift_threshold": 0.0,
        }
    )
    if rebuilt.sort_strategy == "input_order":
        return list(range(assets.sample_count))
    if rebuilt.sort_strategy == "sample_id":
        return sorted(
            range(assets.sample_count), key=lambda index: assets.samples[index].sample_id
        )
    if rebuilt.sort_strategy == "input_norm":
        norms = [
            math.fsum(float(value) * float(value) for value in sample.values)
            for sample in assets.samples
        ]
        return sorted(range(assets.sample_count), key=lambda index: (norms[index], index))
    raise WorkbenchError("E_CONFIG", "未知排序策略", {"sort_strategy": strategy})


def plan_batches(assets: Assets, config: BatchConfig) -> list[BatchPlan]:
    order = order_sample_indices(assets, config.sort_strategy)
    plans: list[BatchPlan] = []
    for offset in range(0, len(order), config.batch_size):
        indices = tuple(order[offset : offset + config.batch_size])
        batch_number = len(plans) + 1
        plans.append(
            BatchPlan(
                index=batch_number,
                batch_id=f"B{batch_number:03d}",
                sample_indices=indices,
                sample_ids=tuple(assets.samples[index].sample_id for index in indices),
            )
        )
    return plans


def plan_signature(plans: list[BatchPlan]) -> list[list[str]]:
    return [list(plan.sample_ids) for plan in plans]
