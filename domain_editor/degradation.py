"""Target-version degradation bookkeeping."""

from __future__ import annotations

from typing import Any

from .model import LossItem, ProjectState
from .v1 import serialize_v1


def compute_target_losses(
    state: ProjectState,
    target_version: str,
    config: dict[str, Any],
    max_workers: int,
) -> list[LossItem]:
    if target_version == "v1":
        return serialize_v1(state, config, max_workers).losses
    if target_version == "v2":
        return []
    raise ValueError(f"unknown target version {target_version!r}")


def recompute_losses(
    state: ProjectState,
    config: dict[str, Any],
    max_workers: int,
) -> list[LossItem]:
    state.losses = compute_target_losses(state, state.target_version, config, max_workers)
    return state.losses
