"""Deterministic quota sampling, shortfall attribution, and sampling evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import numpy as np

from .canonical import stable_uint64


def _selection_rank(
    pair_id: str,
    group: str,
    tier: str,
    score: float,
    seed: int,
) -> tuple[float, int, str]:
    tie_hash = stable_uint64(f"{seed}|{group}|{tier}|{pair_id}")
    return (-score, tie_hash, pair_id)


def compute_sampling(
    pairs: list[dict[str, Any]],
    quality_records: dict[str, dict[str, Any]],
    neardup_result: dict[str, Any],
    consistency_result: dict[str, Any],
    sampling_config: dict[str, Any],
    *,
    seed: int,
) -> dict[str, Any]:
    quotas = sorted(
        sampling_config["quotas"],
        key=lambda item: (str(item["annotator_group"]), str(item["quality_tier"])),
    )
    quota_candidates: dict[tuple[str, str], list[dict[str, Any]]] = {
        (str(quota["annotator_group"]), str(quota["quality_tier"])): []
        for quota in quotas
    }
    exclusions: dict[str, list[str]] = {}
    exclusion_counter: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)

    for pair in sorted(pairs, key=lambda item: str(item["pair_id"])):
        pair_id = str(pair["pair_id"])
        quality = quality_records[pair_id]
        key = (str(pair["annotator_group"]), str(quality["quality_tier"]))
        if key not in quota_candidates:
            exclusions[pair_id] = ["NO_MATCHING_QUOTA"]
            continue
        reasons: list[str] = []
        if not quality["pass"]:
            reasons.append("QUALITY_FAIL")
        assignment = neardup_result["assignments"][pair_id]
        if not assignment["is_representative"]:
            reasons.append("NON_REPRESENTATIVE")
        if consistency_result["records"][pair_id]["conflicted"]:
            reasons.append("CONFLICT")
        if reasons:
            exclusions[pair_id] = sorted(reasons)
            for reason in reasons:
                exclusion_counter[key][reason] += 1
            continue
        quota_candidates[key].append(
            {
                "pair_id": pair_id,
                "pair_score": float(quality["pair_score"]),
                "quality_tier": str(quality["quality_tier"]),
                "annotator_group": str(pair["annotator_group"]),
                "selection_hash": f"{stable_uint64(f'{seed}|{key[0]}|{key[1]}|{pair_id}'):016x}",
            }
        )

    for key, candidates in quota_candidates.items():
        candidates.sort(
            key=lambda item: _selection_rank(
                item["pair_id"],
                key[0],
                key[1],
                item["pair_score"],
                seed,
            )
        )

    target_by_key = {
        (str(quota["annotator_group"]), str(quota["quality_tier"])): int(
            quota["target"]
        )
        for quota in quotas
    }
    selected_by_key: dict[tuple[str, str], list[str]] = {
        key: [] for key in target_by_key
    }
    cursors = {key: 0 for key in target_by_key}
    used: set[str] = set()
    sequence: list[dict[str, Any]] = []
    max_total = sampling_config.get("max_total")
    max_total = int(max_total) if max_total is not None else None

    while max_total is None or len(sequence) < max_total:
        made_progress = False
        for quota_index, quota in enumerate(quotas):
            key = (str(quota["annotator_group"]), str(quota["quality_tier"]))
            if len(selected_by_key[key]) >= target_by_key[key]:
                continue
            candidates = quota_candidates[key]
            cursor = cursors[key]
            while cursor < len(candidates) and candidates[cursor]["pair_id"] in used:
                cursor += 1
            cursors[key] = cursor
            if cursor >= len(candidates):
                continue
            candidate = candidates[cursor]
            cursors[key] = cursor + 1
            used.add(candidate["pair_id"])
            selected_by_key[key].append(candidate["pair_id"])
            sequence.append(
                {
                    "step": len(sequence) + 1,
                    "quota_index": quota_index,
                    "quota_key": f"{key[0]}/{key[1]}",
                    "annotator_group": key[0],
                    "quality_tier": key[1],
                    "target": target_by_key[key],
                    "pair_id": candidate["pair_id"],
                    "pair_score": candidate["pair_score"],
                    "selection_hash": candidate["selection_hash"],
                }
            )
            made_progress = True
            if max_total is not None and len(sequence) >= max_total:
                break
        if not made_progress:
            break

    quota_stats: list[dict[str, Any]] = []
    for quota_index, quota in enumerate(quotas):
        key = (str(quota["annotator_group"]), str(quota["quality_tier"]))
        target = target_by_key[key]
        selected = len(selected_by_key[key])
        shortfall = target - selected
        eligible = len(quota_candidates[key])
        if shortfall == 0:
            reason = None
        elif max_total is not None and len(sequence) >= max_total:
            reason = "GLOBAL_TOTAL_CAP_REACHED"
        elif eligible == 0:
            reason = "NO_ELIGIBLE_CANDIDATES"
        elif eligible < target:
            reason = "ELIGIBLE_SUPPLY_BELOW_TARGET"
        else:
            reason = "OTHER_QUOTA_ALLOCATION_STOPPED"
        quota_stats.append(
            {
                "quota_index": quota_index,
                "quota_key": f"{key[0]}/{key[1]}",
                "annotator_group": key[0],
                "quality_tier": key[1],
                "target": target,
                "eligible_count": eligible,
                "selected_count": selected,
                "shortfall": shortfall,
                "shortfall_reason": reason,
                "excluded_counts": dict(sorted(exclusion_counter[key].items())),
            }
        )

    actual_by_key = np.asarray(
        [len(selected_by_key[key]) for key in sorted(selected_by_key)], dtype=np.int64
    )
    target_array = np.asarray(
        [target_by_key[key] for key in sorted(target_by_key)], dtype=np.int64
    )
    return {
        "sampling_seed": seed,
        "target_total": int(target_array.sum()),
        "actual_total": int(actual_by_key.sum()),
        "global_shortfall": int(target_array.sum() - actual_by_key.sum()),
        "max_total": max_total,
        "selected_pair_ids": [item["pair_id"] for item in sequence],
        "selected_count": len(sequence),
        "quota_stats": quota_stats,
        "exclusions": {pair_id: exclusions[pair_id] for pair_id in sorted(exclusions)},
        "sequence": sequence,
    }
