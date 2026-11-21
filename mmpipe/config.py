"""Typed, hashable pipeline configuration.

The configuration is the *only* knob that may change numeric results. It is
serialized canonically (sorted JSON, no whitespace) and included in every
snapshot checksum, so a changed config forces a full snapshot replacement.
Resource budget settings deliberately live in a separate object because they
affect scheduling but never the mathematical output.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Tuple

from .errors import ResourceLimitError

STAGES: Tuple[str, ...] = ("image", "text", "fusion", "score")
VALID_MASKS = (1, 2, 3)
VALID_HINTS = ("warmup", "steady", "flush")


@dataclass(frozen=True)
class PipelineConfig:
    """Deterministic parameters for the four-stage inference pipeline."""

    block_size: int = 4
    image_dim: int = 8
    text_dim: int = 8
    fusion_dim: int = 16
    master_seed: int = 20261122
    gate_eps: float = 0.001
    score_bias: float = 0.1
    vocab_size: int = 16

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def digest_input(self) -> bytes:
        return self.canonical_json().encode("utf-8")


@dataclass(frozen=True)
class BudgetConfig:
    """Resource budget admission limits and arbitration policy.

    ``max_*`` fields are hard caps. ``overflow_policy`` selects what happens
    when the requested shape cannot fit: ``reject`` raises E_RESOURCE_LIMIT,
    ``degrade`` shrinks batch/worker counts and records a degradation tag.
    Priority order for arbitration is explicit and fixed.
    """

    max_workers: int = 2
    max_batch_pairs: int = 4
    max_resident_embeddings: int = 64
    max_stage_pairs: int = 256
    max_parallel_tasks: int = 8
    bytes_per_embedding: int = 4
    overflow_policy: str = "degrade"
    arbitration_priority: Tuple[str, ...] = ("progress", "memory", "parallelism")

    def validate(self) -> None:
        if self.overflow_policy not in ("reject", "degrade"):
            raise ResourceLimitError(
                f"overflow_policy must be 'reject' or 'degrade', got {self.overflow_policy!r}"
            )
        bad = [p for p in self.arbitration_priority if p not in ("progress", "memory", "parallelism")]
        if bad or len(set(self.arbitration_priority)) != 3:
            raise ResourceLimitError(
                f"arbitration_priority must be a permutation of "
                f"('progress','memory','parallelism'), got {self.arbitration_priority!r}"
            )
        for name in (
            "max_workers",
            "max_batch_pairs",
            "max_resident_embeddings",
            "max_stage_pairs",
            "max_parallel_tasks",
        ):
            if getattr(self, name) <= 0:
                raise ResourceLimitError(f"{name} must be positive")


@dataclass(frozen=True)
class SchedulingDecision:
    """The reproducible output of the budget arbiter for one stage."""

    stage: str
    pair_count: int
    requested_workers: int
    requested_batch: int
    chosen_workers: int
    chosen_batch: int
    resident_embeddings: int
    parallel_tasks: int
    degraded: bool
    degradation_sources: Tuple[str, ...] = field(default_factory=tuple)
    rationale: Tuple[str, ...] = field(default_factory=tuple)

    def to_report(self) -> dict:
        return {
            "stage": self.stage,
            "pair_count": self.pair_count,
            "requested_workers": self.requested_workers,
            "requested_batch": self.requested_batch,
            "chosen_workers": self.chosen_workers,
            "chosen_batch": self.chosen_batch,
            "resident_embeddings": self.resident_embeddings,
            "parallel_tasks": self.parallel_tasks,
            "degraded": self.degraded,
            "degradation_sources": list(self.degradation_sources),
            "rationale": list(self.rationale),
        }

