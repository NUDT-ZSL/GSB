from __future__ import annotations

from typing import Any

from .errors import BusinessError, E_CONFIG_INVALID
from .models import CompilerConfig


def _round_score(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == -0.0 else rounded


def make_score_detail(
    *,
    weight: float,
    slot_hits: int,
    expansion_depth: int,
    config: CompilerConfig,
    rule_id: str,
) -> dict[str, Any]:
    if weight <= 0:
        raise BusinessError(
            E_CONFIG_INVALID,
            "rule weight must be positive",
            field=f"rules.{rule_id}.weight",
            details={"rule_id": rule_id, "weight": weight},
        )
    if expansion_depth < 1 or expansion_depth > config.max_depth_limit:
        raise BusinessError(
            E_CONFIG_INVALID,
            "expansion depth is outside the configured range",
            field=f"rules.{rule_id}.max_depth",
            details={
                "reason": "expansion_depth_out_of_range",
                "rule_id": rule_id,
                "expansion_depth": expansion_depth,
                "max_depth_limit": config.max_depth_limit,
            },
        )
    slot_points = _round_score(config.slot_points * slot_hits)
    depth_penalty = _round_score(config.depth_penalty * (expansion_depth - 1))
    score = _round_score(weight + slot_points - depth_penalty)
    return {
        "weight": _round_score(weight),
        "slot_hits": slot_hits,
        "slot_points": slot_points,
        "expansion_depth": expansion_depth,
        "depth_penalty": depth_penalty,
        "score": score,
        "formula": "weight + slot_points - depth_penalty",
    }


def recompute_score(detail: dict[str, Any]) -> float:
    return _round_score(
        float(detail["weight"])
        + float(detail["slot_points"])
        - float(detail["depth_penalty"])
    )


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda candidate: (
            -float(candidate["score_detail"]["score"]),
            int(candidate["source_line"]),
            str(candidate["candidate"]),
            str(candidate["rule_id"]),
            str(candidate["candidate_id"]),
        ),
    )

