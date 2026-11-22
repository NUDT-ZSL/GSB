"""Pair-level label consistency and independently auditable conflict evidence."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from .errors import (
    E_GROUP_DIRECTION_CONFLICT,
    E_IDENTICAL_SIDE_CONTRADICTION,
    E_LABEL_CROSS_CONTRADICTION,
)
from .neardup import normalize_text


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_consistency(
    pairs: list[dict[str, Any]],
    consistency_config: dict[str, Any],
) -> dict[str, Any]:
    ordered_pairs = sorted(pairs, key=lambda item: str(item["pair_id"]))
    by_prompt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pair_map: dict[str, dict[str, Any]] = {}
    for pair in ordered_pairs:
        pair_id = str(pair["pair_id"])
        chosen = normalize_text(str(pair["chosen"]))
        rejected = normalize_text(str(pair["rejected"]))
        info = {
            "pair_id": pair_id,
            "prompt": str(pair["prompt"]),
            "prompt_digest": text_digest(normalize_text(str(pair["prompt"]))),
            "group": str(pair["annotator_group"]),
            "chosen": chosen,
            "rejected": rejected,
        }
        pair_map[pair_id] = info
        by_prompt[info["prompt"]].append(info)

    pair_conflicts: dict[str, list[dict[str, Any]]] = {
        pair_id: [] for pair_id in pair_map
    }
    conflict_pairs: list[dict[str, Any]] = []

    for _, prompt_pairs in sorted(by_prompt.items()):
        prompt_pairs.sort(key=lambda item: item["pair_id"])
        for index, left in enumerate(prompt_pairs):
            if left["chosen"] == left["rejected"]:
                evidence = {
                    "subtype": "IDENTICAL_CHOSEN_REJECTED",
                    "pair_id": left["pair_id"],
                    "group": left["group"],
                    "prompt_digest": left["prompt_digest"],
                    "chosen_digest": text_digest(left["chosen"]),
                    "rejected_digest": text_digest(left["rejected"]),
                }
                conflict_id = f"X{len(conflict_pairs) + 1:05d}"
                record = {
                    "conflict_id": conflict_id,
                    "pair_ids": [left["pair_id"]],
                    "conflict_types": ["IDENTICAL_SIDES"],
                    "codes": [E_IDENTICAL_SIDE_CONTRADICTION],
                    "evidence": [evidence],
                }
                conflict_pairs.append(record)
                pair_conflicts[left["pair_id"]].append(record)

            for right in prompt_pairs[index + 1 :]:
                swapped = (
                    left["chosen"] == right["rejected"]
                    and left["rejected"] == right["chosen"]
                )
                overlap_roles: list[dict[str, Any]] = []
                if left["chosen"] and left["chosen"] == right["rejected"]:
                    overlap_roles.append(
                        {
                            "text_digest": text_digest(left["chosen"]),
                            "left_role": "chosen",
                            "right_role": "rejected",
                        }
                    )
                if left["rejected"] and left["rejected"] == right["chosen"]:
                    overlap_roles.append(
                        {
                            "text_digest": text_digest(left["rejected"]),
                            "left_role": "rejected",
                            "right_role": "chosen",
                        }
                    )
                if not overlap_roles:
                    continue

                conflict_types: list[str] = []
                codes: list[str] = []
                if swapped:
                    conflict_types.append("CROSS_SIDE_REVERSAL")
                    codes.append(E_LABEL_CROSS_CONTRADICTION)
                else:
                    conflict_types.append("CROSS_SIDE_MEMBERSHIP")
                    codes.append(E_LABEL_CROSS_CONTRADICTION)
                same_group = left["group"] == right["group"]
                if same_group:
                    conflict_types.append("GROUP_DIRECTION_INVERSION")
                    codes.append(E_GROUP_DIRECTION_CONFLICT)

                evidence = {
                    "prompt_digest": left["prompt_digest"],
                    "pair_a": {
                        "pair_id": left["pair_id"],
                        "group": left["group"],
                        "chosen_digest": text_digest(left["chosen"]),
                        "rejected_digest": text_digest(left["rejected"]),
                    },
                    "pair_b": {
                        "pair_id": right["pair_id"],
                        "group": right["group"],
                        "chosen_digest": text_digest(right["chosen"]),
                        "rejected_digest": text_digest(right["rejected"]),
                    },
                    "role_overlaps": overlap_roles,
                    "same_annotator_group": same_group,
                    "swapped_sides": swapped,
                }
                conflict_id = f"X{len(conflict_pairs) + 1:05d}"
                record = {
                    "conflict_id": conflict_id,
                    "pair_ids": [left["pair_id"], right["pair_id"]],
                    "conflict_types": sorted(conflict_types),
                    "codes": sorted(set(codes)),
                    "evidence": [evidence],
                }
                conflict_pairs.append(record)
                pair_conflicts[left["pair_id"]].append(record)
                pair_conflicts[right["pair_id"]].append(record)

    smoothing = float(consistency_config["smoothing"])
    group_members: dict[str, list[str]] = defaultdict(list)
    for pair_id, info in pair_map.items():
        group_members[info["group"]].append(pair_id)
    group_stats: list[dict[str, Any]] = []
    for group in sorted(group_members):
        members = sorted(group_members[group])
        conflict_ids = sorted(
            {
                record["conflict_id"]
                for pair_id in members
                for record in pair_conflicts[pair_id]
                if group in {pair_map[item]["group"] for item in record["pair_ids"]}
            }
        )
        conflicted_pairs = sum(
            1 for pair_id in members if pair_conflicts[pair_id]
        )
        total = len(members)
        smoothed_rate = (
            (total - conflicted_pairs + smoothing) / (total + 2 * smoothing)
            if total
            else 0.0
        )
        group_stats.append(
            {
                "annotator_group": group,
                "pair_count": total,
                "conflicted_pair_count": conflicted_pairs,
                "conflict_ids": conflict_ids,
                "smoothing": smoothing,
                "smoothed_consistency": round(smoothed_rate, 8),
            }
        )

    records: dict[str, dict[str, Any]] = {}
    for pair_id in sorted(pair_map):
        records[pair_id] = {
            "pair_id": pair_id,
            "conflicted": bool(pair_conflicts[pair_id]),
            "conflict_ids": sorted(
                {record["conflict_id"] for record in pair_conflicts[pair_id]}
            ),
            "conflict_types": sorted(
                {
                    conflict_type
                    for record in pair_conflicts[pair_id]
                    for conflict_type in record["conflict_types"]
                }
            ),
            "codes": sorted(
                {
                    code
                    for record in pair_conflicts[pair_id]
                    for code in record["codes"]
                }
            ),
            "evidence": pair_conflicts[pair_id],
        }

    return {
        "normalization": consistency_config.get("normalization", "nfkc_casefold_ws"),
        "smoothing": smoothing,
        "records": records,
        "conflict_pairs": conflict_pairs,
        "group_stats": group_stats,
        "codes": sorted(
            {code for record in conflict_pairs for code in record["codes"]}
        ),
    }
