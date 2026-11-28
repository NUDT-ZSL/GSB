from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from app.config import TOP_K


def _rank_positions(order: Iterable[str]) -> dict[str, int]:
    return {clip_id: index + 1 for index, clip_id in enumerate(order)}


def pairwise_agreement(
    engine_order: list[str],
    reference_order: list[str],
) -> float:
    engine_rank = _rank_positions(engine_order)
    reference_rank = _rank_positions(reference_order)
    common = sorted(set(engine_rank) & set(reference_rank))
    if len(common) < 2:
        return 1.0
    agreements = 0
    comparisons = 0
    for left_index, left in enumerate(common):
        for right in common[left_index + 1 :]:
            comparisons += 1
            engine_sign = engine_rank[left] - engine_rank[right]
            reference_sign = reference_rank[left] - reference_rank[right]
            if engine_sign == reference_sign:
                agreements += 1
    return agreements / comparisons if comparisons else 1.0


def key_hit_rate(
    engine_order: list[str],
    reference_order: list[str],
    top_k: int = TOP_K,
) -> float:
    if not engine_order or not reference_order:
        return 1.0
    k = min(top_k, len(engine_order), len(reference_order))
    return len(set(engine_order[:k]) & set(reference_order[:k])) / k


def rank_disturbance(
    engine_order: list[str],
    reference_order: list[str],
) -> float:
    engine_rank = _rank_positions(engine_order)
    reference_rank = _rank_positions(reference_order)
    common = sorted(set(engine_rank) & set(reference_rank))
    if not common:
        return 0.0
    return sum(
        abs(engine_rank[clip_id] - reference_rank[clip_id])
        for clip_id in common
    ) / len(common)


def mean_abs_rank_change(
    order: list[str],
    prior_order: list[str],
) -> float:
    ranked = _rank_positions(order)
    prior = _rank_positions(prior_order)
    common = sorted(set(ranked) & set(prior))
    if not common:
        return 0.0
    return sum(abs(ranked[c] - prior[c]) for c in common) / len(common)


def build_request_detail(
    engine: dict[str, Any],
    reference: dict[str, Any],
    *,
    top_k: int = TOP_K,
) -> dict[str, Any]:
    engine_order = list(engine["ranked_order"])
    reference_order = list(reference["ranked_order"])
    engine_focus = {item["clip_id"] for item in engine["focus_review"]}
    reference_quality_values = sorted(
        float(item["manual_quality_score"]) for item in reference["ranking"]
    )
    reference_median_quality = (
        reference_quality_values[len(reference_quality_values) // 2]
        if reference_quality_values
        else 0.0
    )
    reference_focus = {
        item["clip_id"]
        for item in reference["ranking"]
        if item["injury_risk_uplift"]
        or (
            item["rank"] - item["prior_rank"]
            >= engine.get("rank_drop_threshold", 3)
        )
        or (
            item.get("metadata", {}).get("is_repeat_group_tail")
            and float(item["manual_quality_score"]) < reference_median_quality - 4.0
        )
    }
    focus_hit = (
        len(engine_focus & reference_focus) / len(reference_focus)
        if reference_focus
        else 1.0
    )
    pairwise = pairwise_agreement(engine_order, reference_order)
    key_hit = key_hit_rate(engine_order, reference_order, top_k)
    disturbance = rank_disturbance(engine_order, reference_order)
    return {
        "request_id": engine["request_id"],
        "athlete_id": engine["athlete_id"],
        "phase": engine["phase"],
        "candidate_count": len(engine_order),
        "engine_order": engine_order,
        "reference_order": reference_order,
        "prior_order": engine["prior_order"],
        "pairwise_agreement": pairwise,
        "key_hit_rate": key_hit,
        "rank_disturbance": disturbance,
        "focus_hit_rate": focus_hit,
        "mean_abs_rank_change_vs_prior": mean_abs_rank_change(
            engine_order, engine["prior_order"]
        ),
        "engine_focus_count": len(engine_focus),
        "reference_focus_count": len(reference_focus),
        "consistent": (
            engine_order == reference_order
            and pairwise == 1.0
            and key_hit == 1.0
            and disturbance == 0.0
        ),
    }


def _aggregate_group(
    details: list[dict[str, Any]],
    *,
    top_k: int = TOP_K,
) -> dict[str, Any]:
    candidate_count = sum(item["candidate_count"] for item in details)
    pair_count = sum(
        item["candidate_count"] * (item["candidate_count"] - 1) / 2
        for item in details
    )
    top_count = sum(min(top_k, item["candidate_count"]) for item in details)

    engine_focus_count = sum(item["engine_focus_count"] for item in details)
    reference_focus_count = sum(item["reference_focus_count"] for item in details)
    return {
        "request_count": len(details),
        "candidate_count": candidate_count,
        "consistent_request_count": sum(1 for item in details if item["consistent"]),
        "pairwise_agreement": (
            sum(
                item["pairwise_agreement"]
                * item["candidate_count"]
                * (item["candidate_count"] - 1)
                / 2
                for item in details
            )
            / pair_count
            if pair_count
            else 1.0
        ),
        "key_hit_rate": (
            sum(
                item["key_hit_rate"] * min(top_k, item["candidate_count"])
                for item in details
            )
            / top_count
            if top_count
            else 1.0
        ),
        "focus_hit_rate": (
            sum(
                item["focus_hit_rate"] * item["reference_focus_count"]
                for item in details
            )
            / reference_focus_count
            if reference_focus_count
            else 1.0
        ),
        "rank_disturbance": sum(
            item["rank_disturbance"] * item["candidate_count"]
            for item in details
        )
        / candidate_count
        if candidate_count
        else 0.0,
        "mean_abs_rank_change_vs_prior": sum(
            item["mean_abs_rank_change_vs_prior"] * item["candidate_count"]
            for item in details
        )
        / candidate_count
        if candidate_count
        else 0.0,
        "engine_focus_count": engine_focus_count,
        "reference_focus_count": reference_focus_count,
    }


def build_consistency_report(
    request_details: list[dict[str, Any]],
    *,
    top_k: int = TOP_K,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_athlete: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_phase: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for detail in request_details:
        grouped[(detail["athlete_id"], detail["phase"])].append(detail)
        by_athlete[detail["athlete_id"]].append(detail)
        by_phase[detail["phase"]].append(detail)

    groups = []
    for (athlete_id, phase), details in sorted(grouped.items()):
        group = _aggregate_group(details, top_k=top_k)
        group.update(
            {
                "athlete_id": athlete_id,
                "phase": phase,
                "request_ids": [item["request_id"] for item in details],
            }
        )
        groups.append(group)

    athlete_groups = []
    for athlete_id, details in sorted(by_athlete.items()):
        group = _aggregate_group(details, top_k=top_k)
        group["athlete_id"] = athlete_id
        athlete_groups.append(group)

    phase_groups = []
    for phase, details in sorted(by_phase.items()):
        group = _aggregate_group(details, top_k=top_k)
        group["phase"] = phase
        phase_groups.append(group)

    return {
        "group_by": "athlete_id+phase",
        "top_k": top_k,
        "definitions": {
            "pairwise_agreement": "正式精排与独立参考排序的候选对同向比例",
            "key_hit_rate": "正式精排前 K 与参考前 K 的交集 / K",
            "focus_hit_rate": "参考重点回看集合被正式清单覆盖的比例",
            "rank_disturbance": "正式名次与参考名次的平均绝对差",
            "mean_abs_rank_change_vs_prior": "精排名次相对粗排名次的平均绝对变化",
        },
        "groups": groups,
        "by_athlete": athlete_groups,
        "by_phase": phase_groups,
        "overall": _aggregate_group(request_details, top_k=top_k),
        "request_details": request_details,
    }


def recursive_reverse_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: recursive_reverse_fields(item)
            for key, item in reversed(list(value.items()))
        }
    if isinstance(value, list):
        return [recursive_reverse_fields(item) for item in value]
    return value
