"""Deterministic admission, batching, worker-slot, MAC, and memory planning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .canonical import content_hash
from .errors import (
    E_BUDGET_FLOP_EXCEEDED,
    E_BUDGET_MEMORY_EXCEEDED,
    W_DEGRADED_GROUPING,
    BusinessError,
)
from .neardup import signature_candidates


FLAG_PRIORITY = {"clean": 3, "borderline": 2, "noisy": 1, "suspicious": 0}


@dataclass
class TrialPlan:
    workers: int
    batch_size: int
    grouping_variant: str
    candidate_mode: str
    comparison_limit: int
    admitted_ids: list[str]
    rejected_ids: list[str]
    n_batches: int
    mac: int
    memory: int
    estimated_time: float
    binding_reason: str | None
    metrics: dict[str, float]
    fingerprint: str

    def public(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "workers": self.workers,
            "batch_size": self.batch_size,
            "grouping_variant": self.grouping_variant,
            "candidate_mode": self.candidate_mode,
            "comparison_limit": self.comparison_limit,
            "admitted_pair_ids": self.admitted_ids,
            "rejected_pair_ids": self.rejected_ids,
            "n_batches": self.n_batches,
            "estimate": {
                "mac": self.mac,
                "flops": self.mac,
                "memory_bytes": self.memory,
                "estimated_time": round(self.estimated_time, 6),
            },
            "binding_reason": self.binding_reason,
        }


def _grouping_quality(
    variant: str,
    admitted_set: set[str],
    *,
    full_candidate_pairs: list[tuple[str, str]],
    signature_pair_list: list[tuple[str, str]],
) -> tuple[str, int, int]:
    if variant == "all":
        filtered = [
            (left, right)
            for left, right in full_candidate_pairs
            if left in admitted_set and right in admitted_set
        ]
        return "all", len(filtered), len(filtered)
    if variant == "none":
        return "none", 0, 0
    filtered = [
        (left, right)
        for left, right in signature_pair_list
        if left in admitted_set and right in admitted_set
    ]
    if variant == "signature":
        limit = len(filtered)
    elif variant == "signature_half":
        limit = math.ceil(len(filtered) / 2)
    elif variant == "signature_quarter":
        limit = math.ceil(len(filtered) / 4)
    else:
        raise ValueError(f"unknown grouping variant: {variant}")
    return "signature", limit, len(filtered)


def _estimate_cost(
    pair_count: int,
    comparison_count: int,
    batch_size: int,
    workers: int,
    cost_model: dict[str, Any],
) -> tuple[int, int, int, float]:
    effective_batch = min(batch_size, pair_count) if pair_count else 1
    n_batches = math.ceil(pair_count / effective_batch) if pair_count else 0
    mac = (
        pair_count * int(cost_model["mac_per_pair"])
        + comparison_count * int(cost_model["mac_per_comparison"])
        + n_batches * int(cost_model["mac_reduction_per_batch"])
        + workers * int(cost_model["mac_worker_overhead"])
    )
    memory = (
        int(cost_model["memory_fixed_bytes"])
        + workers * int(cost_model["memory_per_worker_bytes"])
        + workers * effective_batch * int(cost_model["memory_per_pair_bytes"])
        + comparison_count * int(cost_model["memory_per_comparison_bytes"])
    )
    work = (
        pair_count * int(cost_model["mac_per_pair"])
        + comparison_count * int(cost_model["mac_per_comparison"])
    )
    estimated_time = (
        work / workers
        + n_batches * int(cost_model["mac_reduction_per_batch"])
        + workers * int(cost_model["mac_worker_overhead"])
    )
    return mac, memory, n_batches, estimated_time


def _priority_value(metrics: dict[str, float], priority: str) -> float:
    mapping = {
        "progress": metrics["progress"],
        "memory": metrics["memory_ok"],
        "grouping_quality": metrics["grouping_quality"],
        "parallelism": metrics["parallelism"],
        "wall_time": metrics["wall_time"],
        "batch_efficiency": metrics["batch_efficiency"],
    }
    return float(mapping[priority])


def build_schedule(
    pairs: list[dict[str, Any]],
    scheduler_config: dict[str, Any],
    neardup_config: dict[str, Any],
    priority_order: list[str],
) -> dict[str, Any]:
    ordered_pairs = sorted(pairs, key=lambda item: str(item["pair_id"]))
    all_ids = [str(pair["pair_id"]) for pair in ordered_pairs]
    all_id_set = set(all_ids)
    _, signature_pair_list = signature_candidates(
        ordered_pairs, int(neardup_config["chunk_tokens"])
    )
    full_candidate_pairs = [
        (all_ids[left], all_ids[right])
        for left in range(len(all_ids))
        for right in range(left + 1, len(all_ids))
    ]
    flag_by_id = {
        str(pair["pair_id"]): str(pair.get("quality_flag", "borderline"))
        for pair in ordered_pairs
    }
    admission_order = sorted(
        all_ids,
        key=lambda pair_id: (-FLAG_PRIORITY.get(flag_by_id[pair_id], 1), pair_id),
    )

    max_workers = int(scheduler_config["max_workers"])
    max_batch = int(scheduler_config["max_batch_pairs"])
    mac_budget = int(scheduler_config["mac_budget"])
    memory_budget = int(scheduler_config["memory_budget_bytes"])
    cost_model = scheduler_config["cost_model"]
    variants = ("all", "signature", "signature_half", "signature_quarter", "none")
    trials: list[TrialPlan] = []

    for variant in variants:
        for workers in range(1, max_workers + 1):
            for batch_size in range(1, max_batch + 1):
                remaining = list(admission_order)
                binding_reason: str | None = None
                while True:
                    admitted_set = set(remaining)
                    candidate_mode, comparison_limit, _ = _grouping_quality(
                        variant,
                        admitted_set,
                        full_candidate_pairs=full_candidate_pairs,
                        signature_pair_list=signature_pair_list,
                    )
                    mac, memory, n_batches, estimated_time = _estimate_cost(
                        len(remaining),
                        comparison_limit,
                        batch_size,
                        workers,
                        cost_model,
                    )
                    if mac <= mac_budget and memory <= memory_budget:
                        break
                    if not remaining:
                        binding_reason = "memory" if memory > memory_budget else "mac"
                        break
                    binding_reason = "mac" if mac > mac_budget else "memory"
                    remaining.pop()

                if not remaining:
                    continue
                admitted_set = set(remaining)
                candidate_mode, comparison_limit, _ = _grouping_quality(
                    variant,
                    admitted_set,
                    full_candidate_pairs=full_candidate_pairs,
                    signature_pair_list=signature_pair_list,
                )
                mac, memory, n_batches, estimated_time = _estimate_cost(
                    len(remaining),
                    comparison_limit,
                    batch_size,
                    workers,
                    cost_model,
                )
                if mac > mac_budget or memory > memory_budget:
                    continue
                admitted_ids = sorted(remaining)
                rejected_ids = sorted(all_id_set - admitted_set)
                metrics = {
                    "progress": float(len(admitted_ids)),
                    "memory_ok": 1.0 if memory <= memory_budget else 0.0,
                    "grouping_quality": float(comparison_limit),
                    "parallelism": float(workers),
                    "wall_time": -float(estimated_time),
                    "batch_efficiency": float(batch_size),
                }
                descriptor = {
                    "workers": workers,
                    "batch_size": batch_size,
                    "grouping_variant": variant,
                    "candidate_mode": candidate_mode,
                    "comparison_limit": comparison_limit,
                    "admitted_pair_ids": admitted_ids,
                    "rejected_pair_ids": rejected_ids,
                    "mac": mac,
                    "memory": memory,
                    "n_batches": n_batches,
                }
                fingerprint = content_hash(descriptor)
                trials.append(
                    TrialPlan(
                        workers=workers,
                        batch_size=batch_size,
                        grouping_variant=variant,
                        candidate_mode=candidate_mode,
                        comparison_limit=comparison_limit,
                        admitted_ids=admitted_ids,
                        rejected_ids=rejected_ids,
                        n_batches=n_batches,
                        mac=mac,
                        memory=memory,
                        estimated_time=estimated_time,
                        binding_reason=binding_reason,
                        metrics=metrics,
                        fingerprint=fingerprint,
                    )
                )

    if not trials:
        raise BusinessError(
            E_BUDGET_MEMORY_EXCEEDED,
            "no feasible schedule under the declared budgets",
            details={
                "mac_budget": mac_budget,
                "memory_budget_bytes": memory_budget,
                "max_workers": max_workers,
            },
        )

    def plan_key(plan: TrialPlan) -> tuple[float, ...]:
        values = [_priority_value(plan.metrics, priority) for priority in priority_order]
        return tuple(values) + (-float(int(plan.fingerprint.split(":")[-1][:12], 16)),)

    selected = max(trials, key=plan_key)
    selected_admitted = set(selected.admitted_ids)
    batches: list[dict[str, Any]] = []
    batch_by_pair: dict[str, int] = {}
    slot_by_pair: dict[str, int] = {}
    for batch_id, offset in enumerate(
        range(0, len(selected.admitted_ids), selected.batch_size)
    ):
        batch_pairs = selected.admitted_ids[offset : offset + selected.batch_size]
        worker_slot = batch_id % selected.workers
        batches.append(
            {
                "batch_id": batch_id,
                "worker_slot": worker_slot,
                "pair_ids": batch_pairs,
            }
        )
        for pair_id in batch_pairs:
            batch_by_pair[pair_id] = batch_id
            slot_by_pair[pair_id] = worker_slot

    exact_trials = [plan for plan in trials if plan.grouping_variant == "all"]
    exact_best = max(exact_trials, key=plan_key) if exact_trials else None
    max_parallel_trial = max(
        (
            plan
            for plan in trials
            if plan.admitted_ids == selected.admitted_ids
            and plan.grouping_variant == selected.grouping_variant
        ),
        key=lambda plan: (plan.workers, plan.batch_size),
    )
    adjudications: list[dict[str, Any]] = []
    if selected.workers < max_workers:
        adjudications.append(
            {
                "constraint": "memory_or_mac",
                "action": "reduce_workers",
                "from": max_workers,
                "to": selected.workers,
                "basis": {
                    "selected_memory_bytes": selected.memory,
                    "memory_budget_bytes": memory_budget,
                    "selected_mac": selected.mac,
                    "mac_budget": mac_budget,
                },
            }
        )
    if selected.batch_size < max_batch:
        same_admission_max_batch_mac, same_admission_max_batch_memory, _, _ = _estimate_cost(
            len(selected.admitted_ids),
            selected.comparison_limit,
            max_batch,
            selected.workers,
            cost_model,
        )
        adjudications.append(
            {
                "constraint": "memory",
                "action": "reduce_batch_size",
                "from": max_batch,
                "to": selected.batch_size,
                "basis": {
                    "max_batch_memory_bytes": same_admission_max_batch_memory,
                    "max_batch_mac": same_admission_max_batch_mac,
                    "memory_budget_bytes": memory_budget,
                    "mac_budget": mac_budget,
                },
            }
        )
    if selected.grouping_variant != "all":
        adjudications.append(
            {
                "constraint": "mac",
                "action": "degrade_grouping",
                "code": W_DEGRADED_GROUPING,
                "from": "all_pairs_exact",
                "to": selected.grouping_variant,
                "basis": {
                    "preserves_progress": selected.metrics["progress"],
                    "selected_comparisons": selected.comparison_limit,
                    "exact_mode_admitted_count": (
                        len(exact_best.admitted_ids) if exact_best else 0
                    ),
                },
            }
        )
    if selected.rejected_ids:
        reject_code = (
            E_BUDGET_MEMORY_EXCEEDED
            if selected.binding_reason == "memory"
            else E_BUDGET_FLOP_EXCEEDED
        )
        adjudications.append(
            {
                "constraint": selected.binding_reason or "mac",
                "action": "reject_pairs",
                "code": reject_code,
                "count": len(selected.rejected_ids),
                "basis": {
                    "minimal_plan_mac": selected.mac,
                    "mac_budget": mac_budget,
                    "minimal_plan_memory_bytes": selected.memory,
                    "memory_budget_bytes": memory_budget,
                },
            }
        )
    if selected.workers != max_parallel_trial.workers:
        adjudications.append(
            {
                "constraint": "memory",
                "action": "prefer_memory_safety",
                "selected_workers": selected.workers,
                "same_admission_max_workers": max_parallel_trial.workers,
            }
        )

    pair_paths: dict[str, dict[str, Any]] = {}
    for pair_id in all_ids:
        if pair_id in selected_admitted:
            degraded = selected.grouping_variant != "all"
            pair_paths[pair_id] = {
                "status": "accepted",
                "code": W_DEGRADED_GROUPING if degraded else "OK",
                "stage": "scheduler",
                "batch_id": batch_by_pair[pair_id],
                "worker_slot": slot_by_pair[pair_id],
                "grouping_mode": selected.grouping_variant,
                "degraded_categories": (
                    ["grouping_quality"] if degraded else []
                ),
            }
        else:
            code = (
                E_BUDGET_MEMORY_EXCEEDED
                if selected.binding_reason == "memory"
                else E_BUDGET_FLOP_EXCEEDED
            )
            pair_paths[pair_id] = {
                "status": "rejected",
                "code": code,
                "stage": "scheduler",
                "reason": selected.binding_reason or "mac",
            }

    plan_fingerprint = content_hash(
        {
            "selected_plan": selected.public(),
            "batches": batches,
            "priority_order": priority_order,
            "pair_paths": pair_paths,
        }
    )
    return {
        "schema_version": "scheduler-report/1.0",
        "priority_order": list(priority_order),
        "constraints": {
            "max_workers": max_workers,
            "max_batch_pairs": max_batch,
            "mac_budget": mac_budget,
            "memory_budget_bytes": memory_budget,
        },
        "selected_plan": selected.public(),
        "plan_fingerprint": plan_fingerprint,
        "candidate_plan_count": len(trials),
        "adjudications": adjudications,
        "rejected_codes": sorted(
            {
                E_BUDGET_MEMORY_EXCEEDED
                if selected.binding_reason == "memory"
                else E_BUDGET_FLOP_EXCEEDED
            }
            if selected.rejected_ids
            else set()
        ),
        "degraded_grouping": selected.grouping_variant != "all",
        "batches": batches,
        "pair_paths": {pair_id: pair_paths[pair_id] for pair_id in sorted(pair_paths)},
    }
