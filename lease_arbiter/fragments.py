from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .errors import Code
from .models import (
    AnomalyInjection,
    Card,
    Job,
    LeaseConfig,
    align_up,
    effective_alignment,
)


@dataclass(frozen=True)
class Interval:
    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length

    def as_dict(self, dimension: str) -> dict[str, Any]:
        return {
            "dimension": dimension,
            "start": self.start,
            "length": self.length,
            "end": self.end,
        }


def free_runs(
    card: Card,
    config: LeaseConfig,
    dimension: str,
    occupied: Iterable[tuple[int, int]],
) -> list[Interval]:
    """Return aligned free runs using NumPy slot masks and run detection."""
    alignment = effective_alignment(card, config)
    total = (
        card.total_compute_shares
        if dimension == "compute"
        else card.total_memory_shares
    )
    slots = total // alignment
    mask = np.ones(slots, dtype=np.int8)
    for start, length in occupied:
        if start % alignment or length % alignment:
            raise ValueError("occupied interval is not aligned")
        left = start // alignment
        right = (start + length) // alignment
        if left < 0 or right > slots or right <= left:
            raise ValueError("occupied interval is outside card capacity")
        mask[left:right] = 0
    padded = np.concatenate((np.zeros(1, dtype=np.int8), mask, np.zeros(1, dtype=np.int8)))
    changes = np.diff(padded)
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    return [
        Interval(int(start) * alignment, int(end - start) * alignment)
        for start, end in zip(starts, ends, strict=True)
    ]


def _runs_payload(runs: list[Interval], dimension: str) -> list[dict[str, Any]]:
    return [run.as_dict(dimension) for run in runs]


def _stitch_explanation(runs: list[Interval], needed: int) -> dict[str, Any]:
    order = [{"start": run.start, "end": run.end, "length": run.length} for run in runs]
    longest = max((run.length for run in runs), default=0)
    return {
        "fragment_order": order,
        "needed_length": needed,
        "longest_contiguous_length": longest,
        "merge_rule": "adjacent aligned intervals may merge; gaps are never re-indexed or borrowed",
        "alignment_assumption": "starts and lengths use lcm(card.granularity, config.alignment_granularity)",
    }


def _digest(card_id: str, compute: list[Interval], memory: list[Interval]) -> str:
    payload = {
        "card_id": card_id,
        "compute": [[run.start, run.length] for run in compute],
        "memory": [[run.start, run.length] for run in memory],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def analyze_card(
    card: Card,
    config: LeaseConfig,
    job: Job,
    compute_runs: list[Interval],
    memory_runs: list[Interval],
    *,
    round_no: int,
    injections: list[AnomalyInjection],
) -> dict[str, Any]:
    alignment = effective_alignment(card, config)
    required_compute = align_up(job.compute_shares, alignment)
    required_memory = align_up(job.memory_shares, alignment)
    total_compute = sum(run.length for run in compute_runs)
    total_memory = sum(run.length for run in memory_runs)
    max_compute = max((run.length for run in compute_runs), default=0)
    max_memory = max((run.length for run in memory_runs), default=0)

    satisfiable = (
        max_compute >= required_compute
        and max_memory >= required_memory
        and total_compute >= required_compute
        and total_memory >= required_memory
    )
    if satisfiable:
        classification = "satisfiable"
    elif total_compute < required_compute or total_memory < required_memory:
        classification = "no_capacity"
    else:
        classification = "fragmentation"
    compute_score = (
        max(0.0, 1.0 - (max_compute / required_compute))
        if classification == "fragmentation" and required_compute > 0
        else 0.0
    )
    memory_score = (
        max(0.0, 1.0 - (max_memory / required_memory))
        if classification == "fragmentation" and required_memory > 0
        else 0.0
    )
    fragmentation_score = max(compute_score, memory_score)
    threshold_exceeded = (
        classification == "fragmentation"
        and fragmentation_score >= config.fragmentation_threshold
    )

    actual_digest = _digest(card.card_id, compute_runs, memory_runs)
    effective_digest = actual_digest
    anomaly = None
    for injection in injections:
        if (
            injection.enabled
            and injection.stage == "recombine"
            and injection.type == "fragment_digest_mismatch"
            and injection.round_no == round_no
            and injection.card_id == card.card_id
            and (injection.job_id is None or injection.job_id == job.job_id)
        ):
            effective_digest = ("0" if actual_digest[0] != "0" else "1") + actual_digest[1:]
            anomaly = {
                "code": Code.ANOMALY_FRAGMENT_DIGEST,
                "round": round_no,
                "card_id": card.card_id,
                "anomaly_id": injection.anomaly_id,
                "job_id": job.job_id,
                "actual_digest": actual_digest,
                "computed_digest": effective_digest,
                "message": "fragment recombination digest mismatch injected",
            }
            break

    failure_dimensions: list[str] = []
    if total_compute < required_compute or max_compute < required_compute:
        failure_dimensions.append("compute")
    if total_memory < required_memory or max_memory < required_memory:
        failure_dimensions.append("memory")

    return {
        "card_id": card.card_id,
        "alignment": alignment,
        "classification": classification,
        "fragmentation_score": round(fragmentation_score, 6),
        "fragmentation_threshold": config.fragmentation_threshold,
        "threshold_exceeded": threshold_exceeded,
        "required": {"compute": required_compute, "memory": required_memory},
        "free_totals": {"compute": total_compute, "memory": total_memory},
        "longest_contiguous": {"compute": max_compute, "memory": max_memory},
        "fragment_digest": effective_digest,
        "fragment_digest_valid": effective_digest == actual_digest,
        "failure_dimensions": failure_dimensions,
        "compute_fragments": _runs_payload(compute_runs, "compute"),
        "memory_fragments": _runs_payload(memory_runs, "memory"),
        "compute_stitch": _stitch_explanation(compute_runs, required_compute),
        "memory_stitch": _stitch_explanation(memory_runs, required_memory),
        "anomaly": anomaly,
    }


def analyze_waiting_job(
    job: Job,
    cards: list[Card],
    config: LeaseConfig,
    free_by_card: dict[str, dict[str, list[Interval]]],
    *,
    round_no: int,
    injections: list[AnomalyInjection],
) -> dict[str, Any]:
    card_results = [
        analyze_card(
            card,
            config,
            job,
            free_by_card[card.card_id]["compute"],
            free_by_card[card.card_id]["memory"],
            round_no=round_no,
            injections=injections,
        )
        for card in cards
        if card.allocatable
    ]
    anomalies = [item["anomaly"] for item in card_results if item["anomaly"]]
    if job.allow_cross_card:
        total_compute = sum(
            item["free_totals"]["compute"] for item in card_results
        )
        total_memory = sum(item["free_totals"]["memory"] for item in card_results)
        alignment_values = {item["alignment"] for item in card_results}
        required_compute = align_up(job.compute_shares, min(alignment_values, default=1))
        required_memory = align_up(job.memory_shares, min(alignment_values, default=1))
        if total_compute < required_compute or total_memory < required_memory:
            overall = "no_capacity"
        else:
            overall = "fragmentation"
        alignment_note = (
            "cross-card mode may pair multiple card-local leases; fragments are never "
            "concatenated across card boundaries, and each card keeps its own absolute "
            "aligned offsets"
        )
    else:
        classifications = {item["classification"] for item in card_results}
        if "satisfiable" in classifications:
            overall = "satisfiable"
        elif "fragmentation" in classifications:
            overall = "fragmentation"
        else:
            overall = "no_capacity"
        alignment_note = (
            "single-card mode requires one aligned compute run and one aligned memory "
            "run on the same card; separate free runs can only merge when adjacent"
        )
    key_failures = [
        {
            "card_id": item["card_id"],
            "classification": item["classification"],
            "compute_fragments": item["compute_fragments"],
            "memory_fragments": item["memory_fragments"],
            "longest_contiguous": item["longest_contiguous"],
            "free_totals": item["free_totals"],
            "fragmentation_score": item["fragmentation_score"],
            "threshold_exceeded": item["threshold_exceeded"],
        }
        for item in card_results
        if item["classification"] != "satisfiable"
    ]
    return {
        "job_id": job.job_id,
        "allow_cross_card": job.allow_cross_card,
        "round": round_no,
        "classification": overall,
        "fragmentation_threshold": config.fragmentation_threshold,
        "required": {
            "compute": job.compute_shares,
            "memory": job.memory_shares,
        },
        "alignment_assumption": alignment_note,
        "stitch_rule": (
            "sort fragments by card_id/dimension/start; coalesce only adjacent aligned "
            "runs in that order; preserve each free gap as a hard boundary"
        ),
        "cards": card_results,
        "key_failures": key_failures,
        "anomalies": anomalies,
    }
