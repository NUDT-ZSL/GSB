"""Stage 5: deterministic quota sampling with explicit shortfalls."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..utils import stable_rank


def _target_id(target: dict[str, Any], index: int) -> str:
    return str(
        target.get(
            "target_id",
            f"{target['source_tag']}:{target['quality_tier']}:{index:03d}",
        )
    )


def _ranked(
    samples: list[dict[str, Any]],
    seed: int,
) -> list[dict[str, Any]]:
    return sorted(
        samples,
        key=lambda sample: (
            stable_rank(seed, sample["sample_id"]),
            sample["sample_id"],
        ),
    )


def run_sampling(
    samples: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    sampling = config["sampling"]
    seed = int(sampling["seed"])
    tier_order = list(sampling["tier_order"])
    tier_index = {tier: index for index, tier in enumerate(tier_order)}
    by_stratum: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_stratum[(sample["source_tag"], sample["quality_tier"])].append(sample)
    for sid, members in by_stratum.items():
        by_stratum[sid] = _ranked(members, seed)
    targets = list(sampling.get("targets", []))
    total_target = int(
        sampling.get("total_target")
        or sum(int(target.get("quota", 0)) for target in targets)
    )
    selected_ids: set[str] = set()
    target_records: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    selected_entries: list[dict[str, Any]] = []
    priorities = list(
        config.get(
            "priorities",
            ["progress", "memory", "sampling_target", "dedup_quality"],
        )
    )

    def append_selection(
        target_record: dict[str, Any],
        sample: dict[str, Any],
        *,
        relaxed_from: str | None,
        rank_position: int,
    ) -> None:
        selected_ids.add(sample["sample_id"])
        target_record["achieved"] += 1
        entry = {
            "order": len(selected_entries) + 1,
            "target_id": target_record["target_id"],
            "target_index": target_record["target_index"],
            "target_quota": target_record["quota"],
            "target_quality_tier": target_record["quality_tier"],
            "actual_quality_tier": sample["quality_tier"],
            "relaxed": relaxed_from is not None,
            "relaxed_from_quality_tier": relaxed_from,
            "rank_position": rank_position,
            "sampling_seed": seed,
            "sample": sample,
        }
        selected_entries.append(entry)
        target_record["selected"].append(
            {
                "sample_id": sample["sample_id"],
                "quality_tier": sample["quality_tier"],
                "relaxed": relaxed_from is not None,
                "relaxed_from_quality_tier": relaxed_from,
                "rank_position": rank_position,
            }
        )

    # First pass: satisfy every exact stratum quota in declared target order,
    # stopping only when the global total target is met.
    for target_index, target in enumerate(targets, start=1):
        target_id = _target_id(target, target_index)
        source_tag = target["source_tag"]
        quality_tier = target["quality_tier"]
        quota = int(target.get("quota", 0))
        exact = [
            sample
            for sample in by_stratum.get((source_tag, quality_tier), [])
            if sample["sample_id"] not in selected_ids
        ]
        record = {
            "target_id": target_id,
            "target_index": target_index,
            "source_tag": source_tag,
            "quality_tier": quality_tier,
            "quota": quota,
            "achieved": 0,
            "selected": [],
            "exact_selected": 0,
            "relaxed_selected": 0,
            "seed": seed,
            "shortfall": 0,
            "shortfall_reasons": [],
            "available_exact": len(exact),
        }
        for position, sample in enumerate(exact, start=1):
            if record["achieved"] >= quota:
                break
            if len(selected_ids) >= total_target:
                break
            append_selection(
                record,
                sample,
                relaxed_from=None,
                rank_position=position,
            )
            record["exact_selected"] += 1
        target_records.append(record)

    # Second pass: if total capacity remains and the declared policy gives
    # sampling target precedence over dedup quality, allow bounded tier drops.
    allow_relaxation = bool(sampling.get("allow_quality_relaxation", False))
    max_drop = int(sampling.get("max_tier_drop", 0))
    try:
        relaxation_allowed_by_priority = priorities.index(
            "sampling_target"
        ) < priorities.index("dedup_quality")
    except ValueError:
        relaxation_allowed_by_priority = False
    for record in target_records:
        if record["achieved"] >= record["quota"]:
            continue
        if len(selected_ids) >= total_target:
            break
        target_tier = record["quality_tier"]
        target_position = tier_index.get(target_tier)
        if (
            not allow_relaxation
            or not relaxation_allowed_by_priority
            or target_position is None
            or max_drop <= 0
        ):
            continue
        relaxed_candidates: list[tuple[int, int, dict[str, Any]]] = []
        for drop in range(1, max_drop + 1):
            candidate_position = target_position + drop
            if candidate_position >= len(tier_order):
                break
            candidate_tier = tier_order[candidate_position]
            for position, sample in enumerate(
                by_stratum.get((record["source_tag"], candidate_tier), []),
                start=1,
            ):
                if sample["sample_id"] not in selected_ids:
                    relaxed_candidates.append((drop, position, sample))
        if not relaxed_candidates:
            continue
        for drop, position, sample in relaxed_candidates:
            if record["achieved"] >= record["quota"]:
                break
            if len(selected_ids) >= total_target:
                break
            append_selection(
                record,
                sample,
                relaxed_from=target_tier,
                rank_position=position,
            )
            record["relaxed_selected"] += 1
            decisions.append(
                {
                    "decision": "quality_tier_relaxation",
                    "status": "accepted",
                    "target_id": record["target_id"],
                    "candidate_sample_id": sample["sample_id"],
                    "from_quality_tier": sample["quality_tier"],
                    "to_target_quality_tier": target_tier,
                    "tier_drop": drop,
                    "reason": (
                        "sampling_target has higher strategy priority than "
                        "dedup_quality; bounded tier relaxation fills total target"
                    ),
                    "objective_priority": priorities,
                    "budgets": {
                        "target_quota": record["quota"],
                        "achieved_before_relaxation": record[
                            "exact_selected"
                        ],
                        "total_target": total_target,
                        "selected_before_relaxation": len(selected_ids) - 1,
                        "max_tier_drop": max_drop,
                    },
                }
            )
    if allow_relaxation and not relaxation_allowed_by_priority:
        for record in target_records:
            if record["achieved"] < record["quota"]:
                decisions.append(
                    {
                        "decision": "quality_tier_relaxation",
                        "status": "rejected",
                        "code": "E_QUALITY_POLICY",
                        "target_id": record["target_id"],
                        "reason": (
                            "strategy priority ranks dedup_quality above "
                            "sampling_target, so tier relaxation is refused"
                        ),
                        "objective_priority": priorities,
                        "budgets": {
                            "target_quota": record["quota"],
                            "achieved": record["achieved"],
                            "shortfall": record["quota"] - record["achieved"],
                            "total_target": total_target,
                        },
                    }
                )

    for record in target_records:
        record["shortfall"] = max(0, record["quota"] - record["achieved"])
        if not record["shortfall"]:
            continue
        reasons: list[str] = []
        total_cap_reached = len(selected_ids) >= total_target
        if total_cap_reached:
            reasons.append("TOTAL_CAP_REACHED")
        if record["available_exact"] == 0:
            reasons.append("NO_EXACT_CANDIDATES")
        elif (
            not total_cap_reached
            and record["achieved"]
            < min(record["quota"], record["available_exact"])
        ):
            reasons.append("SAMPLING_RANK_LIMIT")
        if (
            not total_cap_reached
            and allow_relaxation
            and not relaxation_allowed_by_priority
            and record["achieved"] < record["quota"]
        ):
            reasons.append("QUALITY_RELAXATION_DENIED_BY_PRIORITY")
        if (
            not total_cap_reached
            and allow_relaxation
            and relaxation_allowed_by_priority
            and record["relaxed_selected"] == 0
            and record["achieved"] < record["quota"]
        ):
            reasons.append("RELAXED_TIER_EXHAUSTED")
        if not reasons:
            reasons.append("CONSTRAINED_UNDERFILL")
        record["shortfall_reasons"] = reasons
    selected_entries.sort(key=lambda entry: entry["order"])
    return {
        "stage": "sampling",
        "selected": selected_entries,
        "targets": target_records,
        "summary": {
            "seed": seed,
            "total_target": total_target,
            "total_selected": len(selected_entries),
            "total_shortfall": max(0, total_target - len(selected_entries)),
            "quota_shortfall": sum(
                target["shortfall"] for target in target_records
            ),
            "targets": len(target_records),
            "ordering": (
                "target declaration order, then seeded candidate rank order"
            ),
            "quality_relaxation": {
                "enabled": allow_relaxation,
                "allowed_by_priority": relaxation_allowed_by_priority,
                "max_tier_drop": max_drop,
            },
        },
        "arbitration": {
            "priority": priorities,
            "decisions": decisions,
        },
        "completed_sample_ids": sorted(
            sample["sample_id"] for sample in samples
        ),
        "valid_sample_ids": [
            entry["sample"]["sample_id"] for entry in selected_entries
        ],
        "expected_sample_ids": sorted(
            sample["sample_id"] for sample in samples
        ),
    }

