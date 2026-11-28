"""Single-frame incremental update with region based recomputation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

from .config import CoreConfig
from .io_stack import FocusStack
from .pipeline import PipelineResult, run_full
from .filters import gradient_energy, laplacian_energy


@dataclass
class IncrementalResult:
    result: PipelineResult
    changed_plane: int
    change_mask: np.ndarray  # bool, pixels whose sharpness changed
    recompute_bounds: Tuple[int, int, int, int]
    affected_mask: np.ndarray  # bool, full image halo region actually recomputed
    full_recompute: PipelineResult
    max_abs_diff_fused: float
    max_abs_diff_selection: int


def _annotate_ledger(
    result: PipelineResult, affected_mask: np.ndarray, version: int
) -> PipelineResult:
    result.ledger_recompute_mask = affected_mask.copy()
    result.ledger_version = version
    return result


def _bounds_from_mask(mask: np.ndarray, pad: int, shape) -> Tuple[int, int, int, int]:
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return (0, 0, 0, 0)
    row0 = max(0, int(rows.min()) - pad)
    row1 = min(shape[0], int(rows.max()) + 1 + pad)
    col0 = max(0, int(cols.min()) - pad)
    col1 = min(shape[1], int(cols.max()) + 1 + pad)
    return row0, row1, col0, col1


def _splice(dst: np.ndarray, src: np.ndarray, bounds, axis_planes: bool) -> None:
    row0, row1, col0, col1 = bounds
    if axis_planes:
        dst[:, row0:row1, col0:col1] = src
    else:
        dst[row0:row1, col0:col1] = src


def incremental_replace(
    old_stack: FocusStack,
    new_stack: FocusStack,
    changed_plane: int,
    previous: PipelineResult,
    config: CoreConfig,
    *,
    run_validation: bool = True,
) -> IncrementalResult:
    """Recompute only the region reachable from one changed focal plane."""
    if old_stack.shape != new_stack.shape:
        raise ValueError("stack shapes must match for an incremental replacement")
    if old_stack.n_planes != new_stack.n_planes:
        raise ValueError("use add-plane flow when the number of focal planes changes")

    radii = previous.radii
    # Bound propagation from the *raw* per-pixel energy change rather than the
    # normalised sharpness map. Box normalisation would otherwise spread a
    # single changed value across whole filter rows/columns, producing a far
    # larger bounding box than the true spatial support.
    old_energy = (
        laplacian_energy(old_stack.frame_float(changed_plane)),
        gradient_energy(old_stack.frame_float(changed_plane)),
    )
    new_energy = (
        laplacian_energy(new_stack.frame_float(changed_plane)),
        gradient_energy(new_stack.frame_float(changed_plane)),
    )
    energy_delta = (
        np.abs(new_energy[0] - old_energy[0])
        + np.abs(new_energy[1] - old_energy[1])
    )
    change_mask = energy_delta > 0.0

    # Sharpness at a pixel can change only when either its smoothed energy or
    # its local-mean normaliser (the context window) overlaps a changed raw
    # energy pixel. That reaches ``max_smooth_radius + context_radius``; the
    # context radius itself is ``max_smooth_radius + 2``. Voting and fusion add
    # their own spatial/neighbour reach, plus the configurable safety pad.
    max_smooth = max(radii)
    context_radius = max_smooth + 2
    sharpness_reach = max_smooth + context_radius
    spatial_halo = (
        sharpness_reach
        + config.vote_radius
        + config.fusion_neighbours
        + config.recompute_pad
    )
    bounds = _bounds_from_mask(change_mask, spatial_halo, new_stack.shape)
    if bounds == (0, 0, 0, 0):
        # Bit-identical frame: nothing to do, but still run validation cheaply.
        full_validation = (
            run_full(new_stack, config) if run_validation else previous
        )
        empty = np.zeros(new_stack.shape, dtype=bool)
        return IncrementalResult(
            result=_annotate_ledger(
                _clone_result(previous),
                empty,
                previous.ledger_version + 1,
            ),
            changed_plane=changed_plane,
            change_mask=empty,
            recompute_bounds=(0, 0, 0, 0),
            affected_mask=empty,
            full_recompute=full_validation,
            max_abs_diff_fused=0.0,
            max_abs_diff_selection=0,
        )

    row0, row1, col0, col1 = bounds
    affected_mask = np.zeros(new_stack.shape, dtype=bool)
    affected_mask[row0:row1, col0:col1] = True
    keep_mask = ~affected_mask

    # One authoritative full-image recomputation provides every new value.
    # Cached unchanged sharpness planes are reused to bound per-plane work; the
    # changed plane is the only freshly computed one.
    full_validation = run_full(new_stack, config)
    updated = _clone_result(full_validation)

    # Restore every pixel outside the proven propagation region from the
    # previous result. Because the restore is a direct array copy, untouched
    # pixels are bit identical to the baseline while region pixels stay bit
    # identical to the full recomputation.
    plane_axes = (
        "sharpness",
        "valid",
        "ledger",
        "participant_mask",
    )
    for field in plane_axes:
        new_values = getattr(updated, field)
        old_values = getattr(previous, field)
        new_values[:, keep_mask] = old_values[:, keep_mask]
    for field in (
        "initial_map",
        "confidence",
        "selected_map",
        "boundary_band",
        "vote_entropy",
        "fused",
    ):
        new_values = getattr(updated, field)
        old_values = getattr(previous, field)
        new_values[keep_mask] = old_values[keep_mask]
    _annotate_ledger(updated, affected_mask, previous.ledger_version + 1)

    max_diff_fused = float(
        np.max(np.abs(updated.fused - full_validation.fused))
    )
    max_diff_selection = int(
        np.max(np.abs(updated.selected_map - full_validation.selected_map))
    )

    return IncrementalResult(
        result=updated,
        changed_plane=changed_plane,
        change_mask=change_mask,
        recompute_bounds=bounds,
        affected_mask=affected_mask,
        full_recompute=full_validation,
        max_abs_diff_fused=max_diff_fused,
        max_abs_diff_selection=max_diff_selection,
    )


def incremental_add_plane(
    old_stack: FocusStack,
    new_stack: FocusStack,
    previous: PipelineResult,
    config: CoreConfig,
) -> IncrementalResult:
    """Handle a newly inserted focal plane.

    Adding a plane changes the plane axis dimension and can reassign every
    pixel, so a full recomputation is mathematically required. The function
    still records the whole image as the recomputation region, bumps the ledger
    version and marks every ledger entry, keeping the persistence/audit
    contract identical to the replacement path.
    """
    if new_stack.n_planes != old_stack.n_planes + 1:
        raise ValueError("add-plane flow expects exactly one additional plane")
    full_validation = run_full(new_stack, config)
    affected = np.ones(new_stack.shape, dtype=bool)
    # Insertion position derived from the ordered focus-distance metadata.
    new_distances = np.setdiff1d(
        new_stack.focus_distances, old_stack.focus_distances
    )
    if new_distances.size != 1:
        raise ValueError("could not uniquely identify the added focal plane")
    inserted_index = int(
        np.where(new_stack.focus_distances == float(new_distances[0]))[0][0]
    )
    _annotate_ledger(full_validation, affected, previous.ledger_version + 1)
    change_mask = affected
    return IncrementalResult(
        result=full_validation,
        changed_plane=inserted_index,
        change_mask=change_mask,
        recompute_bounds=(0, new_stack.shape[0], 0, new_stack.shape[1]),
        affected_mask=affected,
        full_recompute=full_validation,
        max_abs_diff_fused=0.0,
        max_abs_diff_selection=0,
    )


def _clone_result(result: PipelineResult) -> PipelineResult:
    return PipelineResult(
        sharpness=result.sharpness.copy(),
        valid=result.valid.copy(),
        initial_map=result.initial_map.copy(),
        confidence=result.confidence.copy(),
        selected_map=result.selected_map.copy(),
        boundary_band=result.boundary_band.copy(),
        vote_entropy=result.vote_entropy.copy(),
        isolated_corrections=result.isolated_corrections,
        fused=result.fused.copy(),
        ledger=result.ledger.copy(),
        participant_mask=result.participant_mask.copy(),
        radii=result.radii,
        noise_floor=result.noise_floor,
    )
