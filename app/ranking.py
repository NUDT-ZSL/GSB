from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.config import (
    FOCUS_RANK_DROP_THRESHOLD,
    SCORE_DECIMALS,
    default_weights,
    validate_weights,
)
from app.scoring import score_candidate


def score_order_key(candidate: dict[str, Any]) -> tuple[float, str]:
    fused = round(float(candidate["scores"]["fused"]), SCORE_DECIMALS)
    return (-fused, str(candidate["clip_id"]))


def _tie_groups(ranking: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[float, list[str]] = {}
    for item in ranking:
        score = round(float(item["scores"]["fused"]), SCORE_DECIMALS)
        groups.setdefault(score, []).append(str(item["clip_id"]))
    return [
        {"fused_score": score, "clip_ids": clip_ids}
        for score, clip_ids in sorted(groups.items(), reverse=True)
        if len(clip_ids) > 1
    ]


def _focus_review(
    ranking: list[dict[str, Any]],
    prior_ranks: dict[str, int],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    focus: list[dict[str, Any]] = []
    uplift_ids: list[str] = []
    dropped_ids: list[str] = []
    quality_values = sorted(
        float(item["manual_quality_score"]) for item in ranking
    )
    median_quality = (
        quality_values[len(quality_values) // 2]
        if quality_values
        else 0.0
    )
    for item in ranking:
        reasons: list[str] = []
        adjustments = item.get("injury_risk_uplift", [])
        if adjustments:
            uplift_ids.append(item["clip_id"])
            reasons.append("伤病标签上调负荷风险")
        prior_rank = prior_ranks.get(item["clip_id"])
        if (
            prior_rank is not None
            and int(item["rank_delta"]) >= FOCUS_RANK_DROP_THRESHOLD
        ):
            dropped_ids.append(item["clip_id"])
            reasons.append(
                f"名次较粗排下滑 {int(item['rank_delta'])} 位"
            )
        if (
            item.get("metadata", {}).get("is_repeat_group_tail")
            and float(item["manual_quality_score"]) < median_quality - 4.0
        ):
            reasons.append("重复组尾段质量明显下滑")
        if reasons:
            focus.append(
                {
                    "clip_id": item["clip_id"],
                    "rank": item["rank"],
                    "prior_rank": prior_rank,
                    "rank_delta": item["rank_delta"],
                    "action_category": item["action_category"],
                    "fused_score": item["scores"]["fused"],
                    "reasons": reasons,
                    "injury_adjustments": adjustments,
                }
            )
    return focus, uplift_ids, dropped_ids


def rank_candidates(
    *,
    request_id: str,
    athlete_id: str,
    phase: str,
    candidates: Iterable[dict[str, Any]],
    profile: dict[str, Any],
    weights: dict[str, float] | None = None,
    prior_ranks: dict[str, int] | None = None,
) -> dict[str, Any]:
    normalized_weights = validate_weights(weights or default_weights(phase))
    prior_ranks = dict(prior_ranks or {})

    scored = [
        score_candidate(clip, profile, phase, normalized_weights)
        for clip in candidates
    ]
    scored.sort(key=score_order_key)

    ranking: list[dict[str, Any]] = []
    for index, candidate in enumerate(scored, start=1):
        clip_id = str(candidate["clip_id"])
        prior_rank = prior_ranks.get(clip_id, index)
        rank_delta = index - prior_rank
        if rank_delta < 0:
            movement = "up"
        elif rank_delta > 0:
            movement = "down"
        else:
            movement = "same"
        ranking.append(
            {
                **candidate,
                "rank": index,
                "prior_rank": prior_rank,
                "rank_delta": rank_delta,
                "rank_change": prior_rank - index,
                "movement": movement,
            }
        )

    focus, uplift_ids, dropped_ids = _focus_review(ranking, prior_ranks)
    ranked_order = [item["clip_id"] for item in ranking]
    prior_order = sorted(
        prior_ranks,
        key=lambda clip_id: (prior_ranks[clip_id], clip_id),
    )
    return {
        "request_id": request_id,
        "athlete_id": athlete_id,
        "phase": phase,
        "weights": normalized_weights,
        "candidate_count": len(ranking),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ranked_order": ranked_order,
        "prior_order": prior_order,
        "ranking": ranking,
        "tie_groups": _tie_groups(ranking),
        "focus_review": focus,
        "focus_review_count": len(focus),
        "injury_uplift_candidates": uplift_ids,
        "rank_drop_candidates": dropped_ids,
        "rank_drop_threshold": FOCUS_RANK_DROP_THRESHOLD,
        "scoring_contract": {
            "score_range": [0.0, 100.0],
            "risk_route": "融合时使用 100 - load_risk；风险越高，风险控制得分越低",
            "tie_break": "融合得分保留 6 位小数后，clip_id 升序",
            "score_decimals": SCORE_DECIMALS,
        },
    }
