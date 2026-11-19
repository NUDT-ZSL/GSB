from __future__ import annotations

from typing import Any

from .canonical import hash_value


def sample_pair_hashes(samples: list[dict[str, Any]]) -> dict[str, str]:
    return {item["pair_id"]: hash_value(item) for item in samples}


def diff_pair_hashes(
    previous: dict[str, str] | None,
    current: dict[str, str],
) -> dict[str, list[str]]:
    if previous is None:
        return {
            "added": sorted(current),
            "removed": [],
            "changed": sorted(current),
        }
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = sorted(
        pair_id
        for pair_id in set(current) & set(previous)
        if current[pair_id] != previous[pair_id]
    )
    return {"added": added, "removed": removed, "changed": changed}


def _quality_states(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        item["pair_id"]: {
            "sample_sha256": item["sample_sha256"],
            "quality_score": item["quality_score"],
            "quality_tier": item["quality_tier"],
            "kept": item["kept"],
            "drop_reasons": item["drop_reasons"],
        }
        for item in payload["pair_metrics"]
    }


def _near_states(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        item["pair_id"]: {
            "signature_group_id": item["signature_group_id"],
            "signature": item["signature"],
            "max_signature_similarity": item["max_signature_similarity"],
            "nearest_pair_id": item["nearest_pair_id"],
        }
        for item in payload["pairs"]
    }


def _consistency_states(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        item["pair_id"]: {
            "prompt_group_id": item["prompt_group_id"],
            "prompt_norm_sha256": item["prompt_norm_sha256"],
            "conflict_flagged": item["conflict_flagged"],
            "conflict_group_ids": item["conflict_group_ids"],
            "eligible": item["eligible"],
        }
        for item in payload["pairs"]
    }


def _sampling_states(payload: dict[str, Any]) -> dict[str, Any]:
    result = {
        pair_id: {"selected": False, "rank_sha256": None}
        for pair_id in payload["unselected_pair_ids"]
    }
    for item in payload["dataset"]:
        result[item["pair_id"]] = {
            "selected": True,
            "rank_sha256": item["rank_sha256"],
        }
    return result


def pair_states(payload: dict[str, Any], node: str) -> dict[str, Any]:
    if node == "quality_clean":
        return _quality_states(payload)
    if node == "near_duplicate":
        return _near_states(payload)
    if node == "pair_consistency":
        return _consistency_states(payload)
    if node == "allocation_sampling":
        return _sampling_states(payload)
    raise ValueError(node)


def all_pair_ids(payload: dict[str, Any], node: str) -> list[str]:
    return sorted(pair_states(payload, node))


def changed_pair_ids(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    node: str,
) -> list[str]:
    current_states = pair_states(current, node)
    if previous is None:
        return sorted(current_states)
    previous_states = pair_states(previous, node)
    return sorted(
        pair_id
        for pair_id in set(previous_states) | set(current_states)
        if previous_states.get(pair_id) != current_states.get(pair_id)
    )
