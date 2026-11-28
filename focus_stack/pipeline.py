"""End-to-end pipeline orchestration shared by full and incremental runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from .config import CoreConfig
from .consistency import smooth_selection
from .fusion import fuse_full
from .io_stack import FocusStack
from .sharpness import (
    compute_sharpness_cube,
    select_planes,
)


@dataclass
class PipelineResult:
    sharpness: np.ndarray
    valid: np.ndarray
    initial_map: np.ndarray
    confidence: np.ndarray
    selected_map: np.ndarray
    boundary_band: np.ndarray
    vote_entropy: np.ndarray
    isolated_corrections: int
    fused: np.ndarray
    ledger: np.ndarray
    participant_mask: np.ndarray
    radii: Tuple[int, int]
    noise_floor: float
    ledger_recompute_mask: Optional[np.ndarray] = None  # bool (h, w)
    ledger_version: int = 1


def run_full(stack: FocusStack, config: CoreConfig) -> PipelineResult:
    sharpness, radii = compute_sharpness_cube(stack, config)
    selection = select_planes(stack, config, sharpness)
    consistency = smooth_selection(
        selection.initial_map,
        selection.confidence,
        stack.n_planes,
        config,
    )
    fusion = fuse_full(
        stack,
        sharpness,
        consistency.smoothed_map,
        selection.valid,
        config,
    )
    return PipelineResult(
        sharpness=sharpness,
        valid=selection.valid,
        initial_map=selection.initial_map,
        confidence=selection.confidence,
        selected_map=consistency.smoothed_map,
        boundary_band=consistency.boundary_band,
        vote_entropy=consistency.vote_entropy,
        isolated_corrections=consistency.isolated_corrections,
        fused=fusion.fused,
        ledger=fusion.ledger,
        participant_mask=fusion.participant_mask,
        radii=radii,
        noise_floor=selection.noise_floor,
    )


def run_region(
    stack: FocusStack,
    config: CoreConfig,
    bounds: Tuple[int, int, int, int],
    full_sharpness_new: np.ndarray,
    full_initial: np.ndarray,
    full_confidence: np.ndarray,
) -> PipelineResult:
    """Recompute full-image downstream stages from the new sharpness cube.

    Selection, confidence, consistency and fusion run on complete arrays with
    the identical arithmetic used by :func:`run_full`; only the requested
    region is returned for splicing. Computing on the same-shaped arrays (rather
    than cropped intermediates) eliminates floating-point path differences, so
    every spliced pixel is bit identical to a from-scratch recomputation and
    every pixel outside the region is left untouched by the caller.
    """
    row0, row1, col0, col1 = bounds
    selection = select_planes(stack, config, full_sharpness_new)
    consistency = smooth_selection(
        selection.initial_map,
        selection.confidence,
        stack.n_planes,
        config,
    )
    fusion = fuse_full(
        stack,
        full_sharpness_new,
        consistency.smoothed_map,
        selection.valid,
        config,
    )
    region_sharp = full_sharpness_new[:, row0:row1, col0:col1]
    return PipelineResult(
        sharpness=region_sharp,
        valid=selection.valid[:, row0:row1, col0:col1],
        initial_map=selection.initial_map[row0:row1, col0:col1],
        confidence=selection.confidence[row0:row1, col0:col1],
        selected_map=consistency.smoothed_map[row0:row1, col0:col1],
        boundary_band=consistency.boundary_band[row0:row1, col0:col1],
        vote_entropy=consistency.vote_entropy[row0:row1, col0:col1],
        isolated_corrections=consistency.isolated_corrections,
        fused=fusion.fused[row0:row1, col0:col1],
        ledger=fusion.ledger[:, row0:row1, col0:col1],
        participant_mask=fusion.participant_mask[:, row0:row1, col0:col1],
        radii=stack.adaptive_radii(config),
        noise_floor=selection.noise_floor,
        ledger_recompute_mask=None,
        ledger_version=1,
    )
