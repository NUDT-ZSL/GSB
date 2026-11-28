"""Multi-plane weighted fusion and the per-pixel contribution ledger."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import CoreConfig
from .io_stack import FocusStack


@dataclass
class FusionResult:
    fused: np.ndarray  # float64 (h, w)
    ledger: np.ndarray  # float64 (planes, h, w), axis 0 sums to 1
    participant_mask: np.ndarray  # bool (planes, h, w)


def participant_planes(
    selected: np.ndarray, n_planes: int, config: CoreConfig
) -> np.ndarray:
    """Boolean (planes, h, w) mask of the selected plane plus its neighbours."""
    offsets = np.arange(-config.fusion_neighbours, config.fusion_neighbours + 1)
    h, w = selected.shape
    mask = np.zeros((n_planes, h, w), dtype=bool)
    for offset in offsets:
        plane = selected + offset
        valid = (plane >= 0) & (plane < n_planes)
        rows, cols = np.where(valid)
        mask[plane[rows, cols], rows, cols] = True
    return mask


def _weighted_fuse(
    stack: FocusStack,
    sharpness: np.ndarray,
    selected: np.ndarray,
    valid_candidates: np.ndarray,
    config: CoreConfig,
) -> FusionResult:
    n_planes = stack.n_planes
    participant = participant_planes(selected, n_planes, config)
    # Only planes that are both local participants and valid candidates may vote.
    eligible = participant & valid_candidates
    # Keep the selected plane eligible even if candidate gating removed it.
    h, w = selected.shape
    rows, cols = np.indices((h, w))
    eligible[selected, rows, cols] = True

    powered = np.power(
        np.maximum(sharpness, 0.0), config.fusion_temperature
    )
    weights = np.where(eligible, powered, 0.0)
    total = weights.sum(axis=0)
    total_safe = np.where(total > 0.0, total, 1.0)
    ledger = weights / total_safe
    # Defensive renormalisation removes any residual rounding at machine scale.
    ledger_sum = ledger.sum(axis=0)
    ledger = ledger / np.where(ledger_sum > 0.0, ledger_sum, 1.0)

    fused = np.zeros((h, w), dtype=np.float64)
    for plane in range(n_planes):
        fused += ledger[plane] * stack.frame_float(plane)
    return FusionResult(fused=fused, ledger=ledger, participant_mask=eligible)


def fuse_full(
    stack: FocusStack,
    sharpness: np.ndarray,
    selected: np.ndarray,
    valid_candidates: np.ndarray,
    config: CoreConfig,
) -> FusionResult:
    return _weighted_fuse(stack, sharpness, selected, valid_candidates, config)


