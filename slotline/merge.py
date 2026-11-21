from __future__ import annotations

from typing import Any


def _candidate_identity(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        candidate["key"],
        candidate["value"],
        candidate["source_turn"],
        float(candidate["confidence"]),
        candidate["extractor_version"],
        float(candidate["score"]),
    )


def _candidate_order(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        candidate["source_turn"],
        -float(candidate["score"]),
        candidate["value"],
        candidate["extractor_version"],
        float(candidate["confidence"]),
    )


def _distinct(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=_candidate_order):
        identity = _candidate_identity(candidate)
        if identity not in seen:
            seen.add(identity)
            result.append(candidate)
    return result


def _better(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Return the winning candidate under source/score/value precedence."""

    left_key = (
        int(left["source_turn"]),
        float(left["score"]),
    )
    right_key = (
        int(right["source_turn"]),
        float(right["score"]),
    )
    if left_key > right_key:
        return left
    if right_key > left_key:
        return right
    if left["value"] <= right["value"]:
        return left
    return right


def merge_slot(
    slot_key: str,
    previous: dict[str, Any] | None,
    current_candidates: list[dict[str, Any]],
    *,
    turn_id: str,
    turn_index: int,
    score_node: str,
    score_cache_key: str,
    previous_node: str | None,
    previous_cache_key: str | None,
) -> dict[str, Any]:
    if not current_candidates:
        raise ValueError("merge_slot requires current candidates")
    if any(candidate["key"] != slot_key for candidate in current_candidates):
        raise ValueError("candidate key mismatch in merge_slot")

    current = _distinct([dict(candidate) for candidate in current_candidates])
    current_winner = current[0]
    for candidate in current[1:]:
        current_winner = _better(current_winner, candidate)

    previous_winner = previous["winner"] if previous else None
    previous_covered = list(previous["covered"]) if previous else []
    previous_conflict = bool(previous.get("conflict", False)) if previous else False
    previous_reasons = set(previous.get("conflict_reasons", [])) if previous else set()

    if previous_winner is None:
        winner = current_winner
    else:
        winner = _better(previous_winner, current_winner)

    all_candidates = _distinct(
        ([previous_winner] if previous_winner else []) + current + previous_covered
    )
    covered = [
        candidate
        for candidate in all_candidates
        if _candidate_identity(candidate) != _candidate_identity(winner)
    ]
    covered.sort(key=_candidate_order)

    same_source = [candidate for candidate in all_candidates if candidate["source_turn"] == winner["source_turn"]]
    same_source_values = {candidate["value"] for candidate in same_source}
    reasons = set(previous_reasons)
    if len(same_source_values) > 1:
        reasons.add("same_source_candidates")

    max_source = max(candidate["source_turn"] for candidate in all_candidates)
    max_score = max(
        candidate["score"]
        for candidate in all_candidates
        if candidate["source_turn"] == max_source
    )
    top_ties = [
        candidate
        for candidate in all_candidates
        if candidate["source_turn"] == max_source
        and float(candidate["score"]) == float(max_score)
    ]
    if len({(candidate["value"], candidate["extractor_version"]) for candidate in top_ties}) > 1:
        reasons.add("equal_score_value_tiebreak")

    return {
        "schema_version": 1,
        "slot_key": slot_key,
        "turn_id": turn_id,
        "turn_index": turn_index,
        "winner": dict(winner),
        "covered": covered,
        "conflict": previous_conflict or bool(reasons),
        "conflict_reasons": sorted(reasons),
        "score_node": score_node,
        "score_cache_key": score_cache_key,
        "previous_merge_node": previous_node,
        "previous_merge_cache_key": previous_cache_key,
    }


def public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": candidate["key"],
        "value": candidate["value"],
        "source_turn": candidate["source_turn"],
        "confidence": candidate["confidence"],
        "extractor_version": candidate["extractor_version"],
        "score": candidate["score"],
    }
