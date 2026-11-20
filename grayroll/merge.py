from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any

import numpy as np

from .model import CandidateBatch, MergeConflict, MergeResult, UnifiedBatch
from .util import canonical_bytes, natural_key, sha256_bytes


def _rank(candidate: CandidateBatch) -> tuple[int, int, int]:
    return (
        candidate.logical_clock_ms,
        candidate.generation,
        candidate.source_priority,
    )


def _same_tiers(left: CandidateBatch, right: CandidateBatch) -> bool:
    return bool(
        np.array_equal(
            np.asarray(left.tiers, dtype=np.float64),
            np.asarray(right.tiers, dtype=np.float64),
        )
    )


def _evidence(candidate: CandidateBatch) -> dict[str, Any]:
    return {
        "depends_on": list(candidate.depends_on),
        "earliest_rollout_ms": candidate.earliest_rollout_ms,
        "generation": candidate.generation,
        "logical_clock_ms": candidate.logical_clock_ms,
        "observation_markers": list(candidate.observation_markers),
        "raw_representation": copy.deepcopy(candidate.raw),
        "rollback_window": {
            "end_ms": candidate.rollback_end_ms,
            "start_ms": candidate.rollback_start_ms,
            "step_ms": candidate.rollback_step_ms,
        },
        "source_priority": candidate.source_priority,
        "source_version": candidate.source_version,
        "tiers": list(candidate.tiers),
    }


def _decision_reason(winner: CandidateBatch, losers: list[CandidateBatch]) -> str:
    if not losers:
        return "single_source"
    for loser in losers:
        if winner.logical_clock_ms != loser.logical_clock_ms:
            return "higher_logical_clock"
        if winner.generation != loser.generation:
            return "higher_batch_generation"
        if winner.source_priority != loser.source_priority:
            return "higher_source_priority"
    return "deterministic_equivalent_tie_break"


def merge_candidates(candidates: tuple[CandidateBatch, ...]) -> MergeResult:
    grouped: dict[tuple[str, str], list[CandidateBatch]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.service, candidate.batch_id)].append(candidate)

    batches: list[UnifiedBatch] = []
    conflicts: list[MergeConflict] = []
    for (service, batch_id), group in sorted(
        grouped.items(), key=lambda item: (item[0][0], natural_key(item[0][1]))
    ):
        ordered = sorted(
            group,
            key=lambda item: (
                _rank(item),
                item.source_version,
                sha256_bytes(canonical_bytes(item.raw)),
            ),
            reverse=True,
        )
        best_rank = _rank(ordered[0])
        top = [item for item in ordered if _rank(item) == best_rank]
        if len(top) > 1 and any(not _same_tiers(top[0], item) for item in top[1:]):
            conflicts.append(
                MergeConflict(
                    service=service,
                    batch_id=batch_id,
                    evidence={
                        "batch_id": batch_id,
                        "candidates": [_evidence(item) for item in top],
                        "reason": "equal_rank_inconsistent_tier_sequences",
                        "service": service,
                    },
                )
            )
            continue

        winner = ordered[0]
        losers = ordered[1:]
        decision = {
            "candidates": [_evidence(item) for item in ordered],
            "decision": _decision_reason(winner, losers),
            "winner": {
                "generation": winner.generation,
                "logical_clock_ms": winner.logical_clock_ms,
                "source_priority": winner.source_priority,
                "source_version": winner.source_version,
            },
        }
        batches.append(
            UnifiedBatch(
                service=winner.service,
                batch_id=winner.batch_id,
                source_version=winner.source_version,
                source_priority=winner.source_priority,
                logical_clock_ms=winner.logical_clock_ms,
                generation=winner.generation,
                tiers=winner.tiers,
                observation_markers=winner.observation_markers,
                earliest_rollout_ms=winner.earliest_rollout_ms,
                criteria=copy.deepcopy(winner.criteria),
                rollback_start_ms=winner.rollback_start_ms,
                rollback_end_ms=winner.rollback_end_ms,
                rollback_step_ms=winner.rollback_step_ms,
                depends_on=winner.depends_on,
                raw=copy.deepcopy(winner.raw),
                decision=decision,
                migration=copy.deepcopy(winner.migration),
            )
        )
    return MergeResult(batches=batches, conflicts=conflicts)
