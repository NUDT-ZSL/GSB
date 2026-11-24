from __future__ import annotations

import math
from typing import Any

from .fixedpoint import fixed_mul, quantize
from .model import BattleSpec, Model


def _outcome_at_round(
    battle: BattleSpec, units_after: dict[str, Any]
) -> tuple[str, int | None]:
    players_alive = any(
        units_after[unit_id]["alive"] for unit_id in battle.player_unit_ids
    )
    enemies_alive = any(
        units_after[unit_id]["alive"] for unit_id in battle.enemy_unit_ids
    )
    if not players_alive:
        return "defeat", None
    if not enemies_alive:
        return "victory", None
    return "ongoing", None


def aggregate_battle(
    model: Model, battle: BattleSpec, chunk_results: list[dict[str, Any]]
) -> dict[str, Any]:
    ordered_chunks = sorted(chunk_results, key=lambda item: item["chunk_index"])
    if [item["chunk_index"] for item in ordered_chunks] != list(
        range(battle.chunk_count)
    ):
        missing = sorted(
            set(range(battle.chunk_count))
            - {item["chunk_index"] for item in ordered_chunks}
        )
        raise ValueError(f"cannot aggregate {battle.battle_id}; missing chunks {missing}")

    rounds: list[dict[str, Any]] = []
    for chunk in ordered_chunks:
        rounds.extend(chunk["rounds"])
    rounds.sort(key=lambda item: item["round"])

    outcome = "time_limit"
    decisive_round: int | None = None
    victory_round: int | None = None
    defeat_round: int | None = None
    for round_record in rounds:
        state = round_record["units_after"]
        players_alive = any(state[uid]["alive"] for uid in battle.player_unit_ids)
        enemies_alive = any(state[uid]["alive"] for uid in battle.enemy_unit_ids)
        if not players_alive and defeat_round is None:
            defeat_round = round_record["round"]
        if not enemies_alive and victory_round is None:
            victory_round = round_record["round"]
        if decisive_round is None and (not players_alive or not enemies_alive):
            decisive_round = round_record["round"]

    if defeat_round is not None and (
        victory_round is None or defeat_round <= victory_round
    ):
        outcome = "defeat"
        decisive_round = defeat_round
    elif victory_round is not None:
        outcome = "victory"
        decisive_round = victory_round
    else:
        outcome = "time_limit"

    criteria_id = next(
        (level.criteria_id for level in model.levels.values() if level.battle_id == battle.battle_id),
        None,
    )
    if criteria_id is None:
        raise ValueError(f"battle {battle.battle_id} has no level/criteria")
    criterion = model.criteria[criteria_id]

    hp_round_number: int
    if outcome == "victory" and victory_round is not None:
        hp_round_number = victory_round
    elif outcome == "defeat" and defeat_round is not None:
        hp_round_number = defeat_round
    else:
        hp_round_number = min(criterion.max_rounds, battle.round_count)
    hp_round = next(item for item in rounds if item["round"] == hp_round_number)
    units = hp_round["units_after"]
    hp_sum = sum(int(units[uid]["hp_raw"]) for uid in battle.player_unit_ids)
    max_hp_sum = sum(int(units[uid]["max_hp_raw"]) for uid in battle.player_unit_ids)
    hp_ratio_raw = (hp_sum * model.scale) // max_hp_sum if max_hp_sum else 0
    hp_ratio = hp_ratio_raw / model.scale
    min_hp_raw = quantize(
        criterion.min_team_hp_ratio, model.scale, field="min_team_hp_ratio"
    )

    reward_raw = sum(int(chunk["metrics"]["reward_raw"]) for chunk in ordered_chunks)
    expected_reward_raw = quantize(
        criterion.expected_reward_score, model.scale, field="expected_reward_score"
    )
    reward_min_raw = fixed_mul(
        expected_reward_raw,
        quantize(criterion.reward_min_ratio, model.scale),
        model.scale,
    )
    reward_max_raw = fixed_mul(
        expected_reward_raw,
        quantize(criterion.reward_max_ratio, model.scale),
        model.scale,
    )
    reward_ratio = reward_raw / expected_reward_raw if expected_reward_raw else 0.0

    damage_raw = sum(
        int(chunk["metrics"]["damage_applied_raw"]) for chunk in ordered_chunks
    )
    healing_raw = sum(
        int(chunk["metrics"]["healing_applied_raw"]) for chunk in ordered_chunks
    )
    crit_count = sum(int(chunk["metrics"]["critical_count"]) for chunk in ordered_chunks)

    checks = [
        {
            "check_id": "not_certain_defeat",
            "passed": outcome != "defeat",
            "expected": "outcome != defeat",
            "actual": outcome,
        },
        {
            "check_id": "round_budget",
            "passed": victory_round is not None
            and victory_round <= criterion.max_rounds,
            "expected_max_rounds": criterion.max_rounds,
            "actual_victory_round": victory_round,
            "actual_outcome": outcome,
        },
        {
            "check_id": "minimum_team_hp",
            "passed": hp_ratio_raw >= min_hp_raw,
            "expected_min_ratio": criterion.min_team_hp_ratio,
            "actual_ratio": hp_ratio,
            "expected_raw": min_hp_raw,
            "actual_raw": hp_ratio_raw,
        },
        {
            "check_id": "reward_lower_bound",
            "passed": reward_raw >= reward_min_raw,
            "expected_min_raw": reward_min_raw,
            "actual_raw": reward_raw,
            "expected_min_score": criterion.expected_reward_score
            * criterion.reward_min_ratio,
            "actual_score": reward_raw / model.scale,
        },
        {
            "check_id": "reward_upper_bound",
            "passed": reward_raw <= reward_max_raw,
            "expected_max_raw": reward_max_raw,
            "actual_raw": reward_raw,
            "expected_max_score": criterion.expected_reward_score
            * criterion.reward_max_ratio,
            "actual_score": reward_raw / model.scale,
        },
    ]
    if criterion.require_victory:
        checks[1]["passed"] = (
            victory_round is not None and victory_round <= criterion.max_rounds
        )
    balanced = all(check["passed"] for check in checks)

    first_breach = None
    max_residual_abs = 0.0
    breach_count = 0
    for chunk in ordered_chunks:
        residual = chunk["residual"]
        max_residual_abs = max(max_residual_abs, float(residual["max_abs"]))
        breach_count += int(residual["breach_count"])
        breach = residual.get("first_breach")
        if breach is not None and (
            first_breach is None or breach["round"] < first_breach["round"]
        ):
            first_breach = breach

    return {
        "battle_id": battle.battle_id,
        "level_id": next(
            (level.level_id for level in model.levels.values() if level.battle_id == battle.battle_id),
            None,
        ),
        "criteria_id": criteria_id,
        "balanced": balanced,
        "checks": checks,
        "outcome": outcome,
        "decisive_round": decisive_round,
        "victory_round": victory_round,
        "defeat_round": defeat_round,
        "max_rounds_criterion": criterion.max_rounds,
        "hp_at_round": hp_round_number,
        "team_hp_ratio": hp_ratio,
        "team_hp_ratio_raw": hp_ratio_raw,
        "reward_raw": reward_raw,
        "reward_score": reward_raw / model.scale,
        "reward_ratio_vs_expected": reward_ratio,
        "damage_applied_raw": damage_raw,
        "healing_applied_raw": healing_raw,
        "critical_count": crit_count,
        "chunk_count": battle.chunk_count,
        "chunk_indexes": [item["chunk_index"] for item in ordered_chunks],
        "final_state_digest": ordered_chunks[-1]["end_state_digest"],
        "content_digests": [item["content_digest"] for item in ordered_chunks],
        "residual": {
            "max_abs": max_residual_abs,
            "breach_count": breach_count,
            "first_breach": first_breach,
        },
    }


def deep_compare(
    expected: Any,
    actual: Any,
    *,
    abs_tolerance: float,
    relative_tolerance: float,
    path: str = "$",
    round_number: int | None = None,
) -> dict[str, Any] | None:
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            if math.isclose(
                float(expected),
                float(actual),
                rel_tol=relative_tolerance,
                abs_tol=abs_tolerance,
            ):
                return None
        except (TypeError, ValueError):
            pass
        return {
            "path": path,
            "round": round_number,
            "expected": expected,
            "actual": actual,
            "difference": _safe_diff(expected, actual),
        }
    if type(expected) is not type(actual):
        return {
            "path": path,
            "round": round_number,
            "expected_type": type(expected).__name__,
            "actual_type": type(actual).__name__,
        }
    if isinstance(expected, dict):
        if set(expected) != set(actual):
            return {
                "path": path,
                "round": round_number,
                "missing_keys": sorted(set(expected) - set(actual)),
                "extra_keys": sorted(set(actual) - set(expected)),
            }
        for key in sorted(expected):
            mismatch = deep_compare(
                expected[key],
                actual[key],
                abs_tolerance=abs_tolerance,
                relative_tolerance=relative_tolerance,
                path=f"{path}.{key}",
                round_number=round_number,
            )
            if mismatch:
                return mismatch
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return {
                "path": path,
                "round": round_number,
                "expected_length": len(expected),
                "actual_length": len(actual),
            }
        for index, (left, right) in enumerate(zip(expected, actual)):
            current_round = round_number
            if path.endswith("rounds") and isinstance(left, dict):
                current_round = left.get("round", round_number)
            mismatch = deep_compare(
                left,
                right,
                abs_tolerance=abs_tolerance,
                relative_tolerance=relative_tolerance,
                path=f"{path}[{index}]",
                round_number=current_round,
            )
            if mismatch:
                return mismatch
        return None
    if expected != actual:
        return {
            "path": path,
            "round": round_number,
            "expected": expected,
            "actual": actual,
        }
    return None


def _safe_diff(expected: Any, actual: Any) -> Any:
    try:
        return abs(float(expected) - float(actual))
    except (TypeError, ValueError):
        return None
