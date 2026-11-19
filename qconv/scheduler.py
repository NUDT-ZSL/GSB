from __future__ import annotations

import math
from typing import Any

from .canonical import checksum
from .errors import DomainError, ErrorCode
from .models import Budget, Job, SchedulerConfig
from .quant_conv import coverage_counts, make_chunks, reduction_shape

QUANT_RANK = {"fine": 0, "balanced": 1, "coarse": 2}
EXACT_MODE = "int64_exact"
REDUCED_MODE = "float32_chunk_reduce"


def _output_bytes(mode: str) -> int:
    return 4 if mode == REDUCED_MODE else 8


def _memory_estimate(
    job: Job,
    workers: int,
    tile_k: int,
    tile_oh: int,
    tile_ow: int,
    mode: str,
) -> dict[str, int]:
    input_bytes = math.prod(job.input_shape)
    weight_bytes = math.prod(job.kernel_shape)
    out_bytes = _output_bytes(mode)
    tile_elements = tile_k * tile_oh * tile_ow
    accumulator_tile_bytes = tile_elements * 4
    output_tile_bytes = tile_elements * out_bytes
    per_worker_bytes = (
        input_bytes
        + weight_bytes
        + accumulator_tile_bytes
        + output_tile_bytes
    )
    output_buffer_bytes = job.output_elements * out_bytes
    resident_bytes = output_buffer_bytes + workers * per_worker_bytes
    return {
        "resident_bytes": int(resident_bytes),
        "input_bytes_per_worker": int(input_bytes),
        "weight_bytes_per_worker": int(weight_bytes),
        "accumulator_tile_bytes": int(accumulator_tile_bytes),
        "output_tile_bytes": int(output_tile_bytes),
        "per_worker_bytes": int(per_worker_bytes),
        "output_buffer_bytes": int(output_buffer_bytes),
    }


def _candidate_tiles(job: Job) -> list[tuple[int, int, int]]:
    _, k, oh, ow = job.output_shape
    candidates_by_dim: list[list[int]] = []
    for dimension in (k, oh, ow):
        sizes = {1, dimension}
        power = 2
        while power < dimension:
            sizes.add(power)
            power *= 2
        for divisor_count in range(2, 17):
            sizes.add(math.ceil(dimension / divisor_count))
        candidates_by_dim.append(sorted(size for size in sizes if 1 <= size <= dimension))
    candidates: list[tuple[int, int, int]] = []
    for tile_k in candidates_by_dim[0]:
        for tile_oh in candidates_by_dim[1]:
            for tile_ow in candidates_by_dim[2]:
                candidates.append((tile_k, tile_oh, tile_ow))
    return candidates


def _select_for_mode(
    job: Job,
    workers: int,
    memory_limit: int,
    config: SchedulerConfig,
    mode: str,
) -> dict[str, Any] | None:
    n, k, oh, ow = job.output_shape
    reduction = reduction_shape(job, config)
    best: tuple[tuple[int, int, int, int, int], dict[str, Any]] | None = None
    for tile_k, tile_oh, tile_ow in _candidate_tiles(job):
        chunk_count = (
            n
            * math.ceil(k / tile_k)
            * math.ceil(oh / tile_oh)
            * math.ceil(ow / tile_ow)
        )
        if chunk_count < workers:
            continue
        memory = _memory_estimate(job, workers, tile_k, tile_oh, tile_ow, mode)
        if memory["resident_bytes"] > memory_limit:
            continue
        rounds = math.ceil(chunk_count / workers)
        tile_volume = tile_k * tile_oh * tile_ow
        score = (tile_volume, -chunk_count, tile_ow, tile_oh, tile_k)
        if best is None or score > best[0]:
            best = (score, {"memory": memory})
    if best is None:
        return None
    tile_k = best[0][4]
    tile_oh = best[0][3]
    tile_ow = best[0][2]
    chunk_count = (
        n
        * math.ceil(k / tile_k)
        * math.ceil(oh / tile_oh)
        * math.ceil(ow / tile_ow)
    )
    loop_order = "n_oh_ow_k" if tile_k < k else "n_k_oh_ow"
    chunks = make_chunks(job, 1, tile_k, tile_oh, tile_ow, loop_order)
    coverage = coverage_counts(job, chunks)
    if not coverage["ok"]:
        raise DomainError(
            ErrorCode.E_INCONSISTENT,
            "generated chunks do not cover output exactly once",
            job_id=job.job_id,
            detail=coverage,
        )
    rounds = math.ceil(chunk_count / workers)
    per_worker_mac = math.ceil(job.mac_count / workers)
    merge_dtype = "float32" if mode == REDUCED_MODE else "int64"
    output_dtype = "float32" if mode == REDUCED_MODE else "float64"
    tradeoffs: list[dict[str, Any]] = [
        {
            "code": "BLOCK_SIZE",
            "message": (
                f"tile K/OH/OW={tile_k}/{tile_oh}/{tile_ow} yields "
                f"{chunk_count} chunks; larger tiles reduce reduction overhead "
                "but increase per-worker resident memory"
            ),
        },
        {
            "code": "LOOP_ORDER",
            "message": (
                f"{loop_order} chosen because tile_k={tile_k} and output K={k}"
            ),
        },
        {
            "code": "WORKER_ALLOCATION",
            "message": (
                f"{workers} worker slot(s), {rounds} estimated round(s), "
                f"per-worker MAC share={per_worker_mac}; more workers shorten "
                "rounds but multiply resident worker buffers"
            ),
        },
    ]
    if reduction["segmented"]:
        tradeoffs.append(
            {
                "code": "SEGMENTED_ACCUMULATION",
                "message": (
                    f"overflow guard splits {reduction['terms_per_output']} terms "
                    f"into {reduction['segments']} int32 segments; this adds "
                    "reduction bookkeeping while preventing wraparound"
                ),
            }
        )
    if mode == REDUCED_MODE:
        tradeoffs.append(
            {
                "code": "COARSE_QUANT_REDUCED_REDUCTION",
                "message": (
                    "exact merge did not fit the resident-memory budget; the coarse "
                    "quantization tier uses float32 chunk merging to save 4 bytes "
                    "per output value, accepting one float32 rounding step"
                ),
            }
        )
    basis = [
        "task priority descending",
        "finer quantization tier before coarser at equal priority",
        "progress before memory and parallelism when budgets are equal",
        "reject rather than wait when a hard budget is exhausted",
    ]
    if mode == REDUCED_MODE:
        basis.append("coarse-tier reduced reduction only after exact path fails memory")
    else:
        basis.append("exact int64 merge selected to protect accumulation precision")
    return {
        "job_id": job.job_id,
        "quant_tier": job.quant_tier,
        "priority": job.priority,
        "output_shape": list(job.output_shape),
        "mac_count": job.mac_count,
        "worker_count": workers,
        "tile_n": 1,
        "tile_k": tile_k,
        "tile_oh": tile_oh,
        "tile_ow": tile_ow,
        "chunk_count": chunk_count,
        "estimated_rounds": rounds,
        "per_worker_mac_share": per_worker_mac,
        "loop_order": loop_order,
        "reduction": reduction,
        "accumulator": {
            "accumulator_dtype": "int32",
            "merge_mode": mode,
            "merge_dtype": merge_dtype,
            "output_dtype": output_dtype,
        },
        "memory": best[1]["memory"],
        "coverage": coverage,
        "chunks": chunks,
        "tradeoffs": tradeoffs,
        "decision_basis": basis,
    }


def select_plan(
    job: Job,
    workers: int,
    memory_limit: int,
    config: SchedulerConfig,
) -> dict[str, Any] | None:
    exact = _select_for_mode(job, workers, memory_limit, config, EXACT_MODE)
    if exact is not None:
        return exact
    if (
        config.allow_coarse_reduced_reduction
        and job.quant_tier == "coarse"
    ):
        return _select_for_mode(job, workers, memory_limit, config, REDUCED_MODE)
    return None


def minimum_memory_bytes(job: Job, config: SchedulerConfig) -> int:
    plan = _select_for_mode(job, 1, 2**63 - 1, config, EXACT_MODE)
    if plan is None:
        # This can only happen for pathological dimensions; the complete output
        # buffer is still a valid lower bound.
        return job.output_elements * 8
    # Compute the absolute one-element working set deterministically.
    return _memory_estimate(job, 1, 1, 1, 1, EXACT_MODE)["resident_bytes"]


def _decision_base(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "priority": job.priority,
        "quant_tier": job.quant_tier,
        "mac_required": job.mac_count,
        "output_shape": list(job.output_shape),
    }


def schedule_batch(
    jobs: list[Job],
    budget: Budget,
    config: SchedulerConfig,
) -> dict[str, Any]:
    ordered = sorted(
        jobs,
        key=lambda job: (
            -job.priority,
            QUANT_RANK[job.quant_tier],
            job.job_id,
        ),
    )
    remaining = {
        "mac": budget.mac_budget,
        "memory_bytes": budget.memory_budget_bytes,
        "workers": budget.worker_slots,
    }
    admitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    by_id = {job.job_id: job for job in jobs}

    for job in ordered:
        decision = _decision_base(job)
        decision["budget_before"] = dict(remaining)
        if job.mac_count > remaining["mac"]:
            decision.update(
                {
                    "status": "rejected",
                    "code": ErrorCode.E_MAC_BUDGET.value,
                    "message": "required convolution MACs exceed remaining budget",
                    "combination_required": job.mac_count,
                    "combination_remaining": remaining["mac"],
                    "deductions": {"mac": 0, "memory_bytes": 0, "workers": 0},
                }
            )
            rejected.append(decision)
            continue
        if remaining["workers"] < 1:
            decision.update(
                {
                    "status": "rejected",
                    "code": ErrorCode.E_WORKER_SLOTS.value,
                    "message": "no worker slot remains for the minimum one-worker plan",
                    "deductions": {"mac": 0, "memory_bytes": 0, "workers": 0},
                }
            )
            rejected.append(decision)
            continue
        plan = select_plan(job, 1, remaining["memory_bytes"], config)
        if plan is None:
            decision.update(
                {
                    "status": "rejected",
                    "code": ErrorCode.E_MEMORY_LIMIT.value,
                    "message": "no tile plan fits the remaining resident-memory budget",
                    "minimum_memory_bytes": minimum_memory_bytes(job, config),
                    "memory_remaining": remaining["memory_bytes"],
                    "deductions": {"mac": 0, "memory_bytes": 0, "workers": 0},
                }
            )
            rejected.append(decision)
            continue
        remaining["mac"] -= job.mac_count
        remaining["memory_bytes"] -= plan["memory"]["resident_bytes"]
        remaining["workers"] -= 1
        admitted.append(plan)
        decision.update(
            {
                "status": "admitted",
                "code": "OK",
                "budget_after_provisional": dict(remaining),
            }
        )
        decision["_job"] = job
        decision["_plan"] = plan

    # Add concurrency only after every task has its mandatory one-worker plan.
    # This prevents a high-priority task from starving later tasks of the
    # admission pass while still allowing throughput improvements.
    made_progress = True
    while remaining["workers"] > 0 and made_progress:
        made_progress = False
        for index, plan in enumerate(admitted):
            if remaining["workers"] <= 0:
                break
            if plan["worker_count"] >= config.max_workers_per_task:
                continue
            if plan["worker_count"] >= plan["chunk_count"]:
                continue
            job = by_id[plan["job_id"]]
            old_resident = plan["memory"]["resident_bytes"]
            candidate = select_plan(
                job,
                plan["worker_count"] + 1,
                remaining["memory_bytes"] + old_resident,
                config,
            )
            if candidate is None:
                continue
            delta = candidate["memory"]["resident_bytes"] - old_resident
            if delta > remaining["memory_bytes"]:
                continue
            if candidate["estimated_rounds"] >= plan["estimated_rounds"]:
                continue
            admitted[index] = candidate
            remaining["memory_bytes"] -= delta
            remaining["workers"] -= 1
            made_progress = True

    committed_mac = sum(plan["mac_count"] for plan in admitted)
    committed_memory = sum(plan["memory"]["resident_bytes"] for plan in admitted)
    committed_workers = sum(plan["worker_count"] for plan in admitted)
    accepted_ids = [plan["job_id"] for plan in admitted]

    decisions: list[dict[str, Any]] = []
    for plan in admitted:
        decision = _decision_base(by_id[plan["job_id"]])
        decision.update(
            {
                "status": "admitted",
                "code": "OK",
                "plan": plan,
                "deductions": {
                    "mac": plan["mac_count"],
                    "memory_bytes": plan["memory"]["resident_bytes"],
                    "workers": plan["worker_count"],
                },
                "budget_after": {
                    "mac": budget.mac_budget - committed_mac,
                    "memory_bytes": budget.memory_budget_bytes - committed_memory,
                    "workers": budget.worker_slots - committed_workers,
                },
            }
        )
        decisions.append(decision)
    decisions.extend(rejected)
    decision_order = {job.job_id: index for index, job in enumerate(ordered)}
    decisions.sort(key=lambda item: decision_order[str(item["job_id"])])

    ledger_entries: list[dict[str, Any]] = []
    for decision in decisions:
        job_id = str(decision["job_id"])
        ledger_entries.append(
            {
                "job_id": job_id,
                "status": decision["status"],
                "code": decision["code"],
                "mac_debit": int(decision["deductions"]["mac"]),
                "memory_debit_bytes": int(decision["deductions"]["memory_bytes"]),
                "worker_debit": int(decision["deductions"]["workers"]),
            }
        )
    ledger = {
        "limits": budget.to_dict(),
        "usage": {
            "mac_committed": committed_mac,
            "memory_committed_bytes": committed_memory,
            "workers_committed": committed_workers,
        },
        "remaining": {
            "mac_budget": budget.mac_budget - committed_mac,
            "memory_budget_bytes": budget.memory_budget_bytes - committed_memory,
            "worker_slots": budget.worker_slots - committed_workers,
        },
        "committed_job_ids": accepted_ids,
        "entries": ledger_entries,
        "reconciled": (
            sum(entry["mac_debit"] for entry in ledger_entries) == committed_mac
            and sum(entry["memory_debit_bytes"] for entry in ledger_entries)
            == committed_memory
            and sum(entry["worker_debit"] for entry in ledger_entries)
            == committed_workers
        ),
        "rejected_debits_zero": all(
            entry["mac_debit"] == 0
            and entry["memory_debit_bytes"] == 0
            and entry["worker_debit"] == 0
            for entry in ledger_entries
            if entry["status"] == "rejected"
        ),
    }
    plan_payload = {
        "format": "qconv-schedule-v1",
        "jobs": [job.to_dict() for job in ordered],
        "policy": config.to_dict(),
        "policy_order": list(config.priority_policy),
        "budget": budget.to_dict(),
        "considered_job_ids": [job.job_id for job in ordered],
        "decisions": decisions,
        "admitted": admitted,
        "admitted_job_ids": accepted_ids,
        "rejected_job_ids": [decision["job_id"] for decision in rejected],
        "worker_slots_used": committed_workers,
        "total_chunks": sum(plan["chunk_count"] for plan in admitted),
        "all_coverage_ok": all(plan["coverage"]["ok"] for plan in admitted),
        "plan_digest": checksum(
            {
                "policy": config.to_dict(),
                "decisions": decisions,
                "admitted": admitted,
            }
        ),
    }
    return {"plan": plan_payload, "ledger": ledger, "jobs_by_id": by_id}

