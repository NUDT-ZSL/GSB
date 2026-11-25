"""Fixed three-tier memory budget admission."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from .errors import EMemoryBudget
from .models import Task


@dataclass(frozen=True)
class Budgets:
    total_feature_buffer_size: int = 64 * 1024 * 1024
    single_pass_workspace_size: int = 16 * 1024 * 1024
    model_workspace_size: int = 32 * 1024 * 1024

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _mode_profiles(mode: str) -> tuple[str, ...]:
    if mode == "both":
        return ("train", "infer")
    if mode in {"train", "infer"}:
        return (mode,)
    raise ValueError(f"unknown mode: {mode}")


def task_profile(task: Task, mode: str) -> dict[str, Any]:
    profiles = _mode_profiles(mode)
    per_mode = {
        selected: {
            "total_feature_buffer_size": task.feature_buffer_bytes(selected),
            "single_pass_workspace_size": task.single_pass_workspace_bytes(selected),
            "model_workspace_size": task.model_workspace_bytes(selected),
        }
        for selected in profiles
    }
    maxima = {
        tier: max(profile[tier] for profile in per_mode.values())
        for tier in (
            "total_feature_buffer_size",
            "single_pass_workspace_size",
            "model_workspace_size",
        )
    }
    return {
        "case_id": task.case_id,
        "mode": mode,
        "mode_profiles": per_mode,
        "maxima": maxima,
        "policy": {
            "train": "full float64 feature plus retained gradient input",
            "infer": "resident quantized feature plus current dequantized block",
            "both": "admit only if both mode-specific working sets fit",
        },
    }


def validate_budget(task: Task, mode: str, budgets: Budgets) -> dict[str, Any]:
    profile = task_profile(task, mode)
    limits = budgets.to_dict()
    violations = {
        tier: {
            "required": profile["maxima"][tier],
            "limit": limits[tier],
        }
        for tier in limits
        if profile["maxima"][tier] > limits[tier]
    }
    if violations:
        raise EMemoryBudget(
            "task exceeds fixed memory budget",
            {
                "case_id": task.case_id,
                "mode": mode,
                "violations": violations,
                "profile": profile,
            },
        )
    return {"ok": True, "case_id": task.case_id, "profile": profile, "limits": limits}
