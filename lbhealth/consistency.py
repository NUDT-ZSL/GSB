"""Cross-round and per-round traffic consistency checks."""

from __future__ import annotations

from typing import Any

from .config import Config
from .models import HealthState, RoundResult


def validate_round(config: Config, result: RoundResult) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    member_by_id = config.member_by_id
    expected_available = [
        member.id
        for member in config.members
        if result.state_after.member_states[member.id].state == HealthState.HEALTHY
    ]
    if list(result.available_members) != expected_available:
        errors.append(
            {
                "code": "E_CONSISTENCY",
                "field": "available_members",
                "expected": expected_available,
                "actual": list(result.available_members),
            }
        )

    details_by_id = {detail.member_id: detail for detail in result.weight_details}
    expected_sum = 0
    for member in config.members:
        state = result.state_after.member_states[member.id]
        available = state.state == HealthState.HEALTHY
        if available:
            index = min(max(0, state.slow_start_index), len(config.pool.slow_start_weight_table) - 1)
            percent = config.pool.slow_start_weight_table[index]
            expected_weight = (member.weight * percent) // 100
        else:
            index = 0
            percent = 0
            expected_weight = 0
        actual_weight = int(result.effective_weights.get(member.id, 0))
        expected_sum += expected_weight
        if actual_weight != expected_weight:
            errors.append(
                {
                    "code": "E_CONSISTENCY",
                    "member": member.id,
                    "field": f"effective_weights.{member.id}",
                    "expected": expected_weight,
                    "actual": actual_weight,
                }
            )
        detail = details_by_id.get(member.id)
        if detail is None:
            errors.append(
                {
                    "code": "E_CONSISTENCY",
                    "member": member.id,
                    "field": "weight_details",
                    "expected": "present",
                    "actual": "missing",
                }
            )
            continue
        expected_detail = {
            "state": state.state.value,
            "base_weight": member.weight,
            "slow_start_index": index,
            "slow_start_percent": percent,
            "effective_weight": expected_weight,
            "available": available,
        }
        actual_detail = detail.to_dict()
        for key, value in expected_detail.items():
            if actual_detail.get(key) != value:
                errors.append(
                    {
                        "code": "E_CONSISTENCY",
                        "member": member.id,
                        "field": f"weight_details.{key}",
                        "expected": value,
                        "actual": actual_detail.get(key),
                    }
                )
    if result.effective_weight_sum != expected_sum or sum(result.effective_weights.values()) != expected_sum:
        errors.append(
            {
                "code": "E_CONSISTENCY",
                "field": "effective_weight_sum",
                "expected": expected_sum,
                "actual": result.effective_weight_sum,
            }
        )
    if result.full_weight_sum != config.pool.full_weight_sum:
        errors.append(
            {
                "code": "E_CONSISTENCY",
                "field": "full_weight_sum",
                "expected": config.pool.full_weight_sum,
                "actual": result.full_weight_sum,
            }
        )
    all_full = all(
        result.effective_weights.get(member.id, 0) == member.weight
        for member in config.members
        if result.state_after.member_states[member.id].state == HealthState.HEALTHY
    ) and bool(result.available_members)
    return {
        "status": "ok" if not errors else "error",
        "code": "OK" if not errors else "E_CONSISTENCY",
        "round": result.round_no,
        "available_members": list(result.available_members),
        "effective_weights": {
            member.id: int(result.effective_weights.get(member.id, 0))
            for member in config.members
        },
        "effective_weight_sum": expected_sum,
        "full_weight_sum": config.pool.full_weight_sum,
        "all_available_members_at_full_weight": all_full,
        "weight_formula": "healthy member: floor(base_weight * slow_start_percent / 100); otherwise 0",
        "errors": errors,
    }
