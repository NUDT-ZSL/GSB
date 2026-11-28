"""Capacity-aware target selection and deterministic batch planning."""

from __future__ import annotations

from .model import Dataset, contract_id, contract_index


def _bp(numerator: int, denominator: int) -> int:
    return (numerator * 10_000) // denominator if denominator else 0


def _slice_contracts(dataset: Dataset, start: str, end: str):
    contracts = dataset.contracts_by_id
    return [
        contracts[contract_id(index)]
        for index in range(contract_index(start), contract_index(end) + 1)
    ]


def _choose_target(
    dataset: Dataset,
    source_shard: str,
    required_bytes: int,
    current_usage: dict[str, int],
) -> tuple[str | None, int, list[dict]]:
    candidates: list[dict] = []
    for shard in dataset.shards:
        if shard.shard_id == source_shard:
            continue
        used = current_usage[shard.shard_id]
        free = shard.capacity_bytes - used
        watermark_bp = _bp(used, shard.capacity_bytes)
        eligible = free >= required_bytes
        candidates.append(
            {
                "shard": shard.shard_id,
                "current_used_bytes": used,
                "capacity_bytes": shard.capacity_bytes,
                "free_bytes": free,
                "current_watermark_bp": watermark_bp,
                "required_bytes": required_bytes,
                "eligible": eligible,
                "reason": (
                    f"free_bytes={free} >= required_bytes={required_bytes}"
                    if eligible
                    else f"free_bytes={free} < required_bytes={required_bytes}"
                ),
            }
        )
    eligible_candidates = [row for row in candidates if row["eligible"]]
    eligible_candidates.sort(
        key=lambda row: (
            row["current_watermark_bp"],
            -row["free_bytes"],
            row["shard"],
        )
    )
    max_free = max((row["free_bytes"] for row in candidates), default=0)
    if not eligible_candidates:
        return None, max_free, candidates
    chosen = eligible_candidates[0]
    for row in candidates:
        if row["shard"] == chosen["shard"]:
            row["selected"] = True
            row["selection_reason"] = (
                f"lowest_current_watermark_bp={row['current_watermark_bp']} "
                f"among capacity-eligible targets"
            )
        else:
            row["selected"] = False
            row["selection_reason"] = (
                "capacity_eligible_but_higher_watermark"
                if row["eligible"]
                else "insufficient_capacity"
            )
    return chosen["shard"], max_free, candidates


def _make_batches(
    contracts: list,
    source_shard: str,
    target_shard: str,
    batch_capacity: int,
) -> list[dict]:
    batches: list[dict] = []
    current: list = []
    current_bytes = 0
    for contract in contracts:
        if current and current_bytes + contract.document_bytes > batch_capacity:
            batches.append(_finish_batch(current, current_bytes, source_shard, target_shard))
            current = []
            current_bytes = 0
        current.append(contract)
        current_bytes += contract.document_bytes
        if current_bytes > batch_capacity and len(current) == 1:
            batches.append(
                _finish_batch(
                    current,
                    current_bytes,
                    source_shard,
                    target_shard,
                    oversized_single_contract=True,
                )
            )
            current = []
            current_bytes = 0
    if current:
        batches.append(_finish_batch(current, current_bytes, source_shard, target_shard))
    return batches


def _finish_batch(
    contracts: list,
    total_bytes: int,
    source_shard: str,
    target_shard: str,
    oversized_single_contract: bool = False,
) -> dict:
    return {
        "start": contracts[0].contract_id,
        "end": contracts[-1].contract_id,
        "contracts": [contract.contract_id for contract in contracts],
        "contract_count": len(contracts),
        "bytes": total_bytes,
        "source_shard": source_shard,
        "target_shard": target_shard,
        "batch_capacity_bytes": None,
        "within_batch_capacity": not oversized_single_contract,
        "note": (
            "single_contract_larger_than_batch_capacity"
            if oversized_single_contract
            else "greedy_contiguous_batch"
        ),
    }


def build_migration_plan(
    dataset: Dataset,
    hotspot: dict,
    batch_capacity: int,
) -> dict:
    current_usage = {
        shard.shard_id: shard.declared_used_bytes for shard in dataset.shards
    }
    plans: list[dict] = []
    all_batches: list[dict] = []
    batch_ordinal = 0

    for index, item in enumerate(hotspot.get("hot_range_slices", [])):
        contracts = _slice_contracts(dataset, item["start"], item["end"])
        required_bytes = sum(contract.document_bytes for contract in contracts)
        target, max_free, candidates = _choose_target(
            dataset,
            item["source_shard"],
            required_bytes,
            current_usage,
        )
        selected_row = next(
            (row for row in candidates if row["shard"] == target), None
        )
        plan = {
            "plan_index": index,
            "start": item["start"],
            "end": item["end"],
            "source_shard": item["source_shard"],
            "target_shard": target,
            "split_from_cross_boundary": item["split_from_cross_boundary"],
            "status": "accepted" if target is not None else "rejected",
            "contract_count": len(contracts),
            "contract_ids": [contract.contract_id for contract in contracts],
            "required_bytes": required_bytes,
            "target_selection": {
                "policy": "lowest_capacity_watermark_with_sufficient_free_bytes",
                "chosen_target": target,
                "reason": (
                    selected_row["selection_reason"]
                    if selected_row is not None
                    else "no_target_has_sufficient_capacity"
                ),
                "candidates": candidates,
            },
            "batches": [],
            "rejection": None,
        }
        if target is None:
            gap = max(0, required_bytes - max_free)
            plan["rejection"] = {
                "conclusion": "rejected_capacity_insufficient",
                "required_bytes": required_bytes,
                "largest_single_target_free_bytes": max_free,
                "gap_bytes": gap,
                "error_code": 1012,
            }
            plans.append(plan)
            continue

        batches = _make_batches(contracts, item["source_shard"], target, batch_capacity)
        for batch in batches:
            batch["batch_id"] = f"batch-{batch_ordinal + 1:02d}"
            batch["plan_index"] = index
            batch["batch_ordinal"] = batch_ordinal
            batch["batch_capacity_bytes"] = batch_capacity
            all_batches.append(batch)
            batch_ordinal += 1
        plan["batches"] = [
            {
                "batch_id": batch["batch_id"],
                "start": batch["start"],
                "end": batch["end"],
                "contract_count": batch["contract_count"],
                "bytes": batch["bytes"],
                "source_shard": batch["source_shard"],
                "target_shard": batch["target_shard"],
                "within_batch_capacity": batch["within_batch_capacity"],
                "note": batch["note"],
            }
            for batch in batches
        ]
        current_usage[target] += required_bytes
        projected_used = current_usage[target]
        plan["target_selection"]["projected_used_bytes"] = projected_used
        plan["target_selection"]["capacity_after_bytes"] = (
            projected_used
        )
        plan["target_selection"]["capacity_margin_bytes"] = (
            next(
                shard.capacity_bytes
                for shard in dataset.shards
                if shard.shard_id == target
            )
            - projected_used
        )
        plan["target_selection"]["projected_watermark_bp"] = _bp(
            projected_used,
            next(
                shard.capacity_bytes
                for shard in dataset.shards
                if shard.shard_id == target
            ),
        )
        plans.append(plan)

    rejected = sum(1 for plan in plans if plan["status"] == "rejected")
    return {
        "batch_capacity_bytes": batch_capacity,
        "target_policy": "lowest_watermark_among_targets_with_sufficient_free_bytes",
        "plans": plans,
        "flattened_batches": all_batches,
        "summary": {
            "hot_range_count": len(plans),
            "accepted_range_count": len(plans) - rejected,
            "rejected_range_count": rejected,
            "batch_count": len(all_batches),
            "total_contracts_to_move": sum(
                plan["contract_count"] for plan in plans if plan["status"] == "accepted"
            ),
            "total_bytes_to_move": sum(
                plan["required_bytes"] for plan in plans if plan["status"] == "accepted"
            ),
        },
    }
