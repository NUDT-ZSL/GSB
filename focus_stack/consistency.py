"""Neighbourhood consistency: weighted majority vote and boundary band."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

from .config import CoreConfig
from .filters import box_filter


@dataclass
class ConsistencyResult:
    smoothed_map: np.ndarray
    boundary_band: np.ndarray  # bool
    vote_entropy: np.ndarray
    isolated_corrections: int
    critical_peak_mask: np.ndarray
    vote_disagreement_mask: np.ndarray


def _weighted_vote(
    initial_map: np.ndarray,
    confidence: np.ndarray,
    n_planes: int,
    radius: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return winning plane per pixel and normalised vote entropy."""
    h, w = initial_map.shape
    vote_weight = np.empty((n_planes, h, w), dtype=np.float64)
    for plane in range(n_planes):
        weights = np.where(initial_map == plane, confidence, 0.0)
        vote_weight[plane] = box_filter(weights, radius)
    winner = np.argmax(vote_weight, axis=0).astype(np.int64)

    total = vote_weight.sum(axis=0)
    safe_total = np.where(total > 0.0, total, 1.0)
    probs = vote_weight / safe_total
    with np.errstate(divide="ignore", invalid="ignore"):
        log_probs = np.where(probs > 0.0, np.log(probs), 0.0)
    entropy = -(probs * log_probs).sum(axis=0)
    normalised_entropy = entropy / np.log(max(n_planes, 2))
    return winner, normalised_entropy


def _isolated_correction(
    initial_map: np.ndarray,
    confidence: np.ndarray,
    n_planes: int,
    config: CoreConfig,
    *,
    box_fn,
) -> Tuple[np.ndarray, np.ndarray]:
    """Vectorised isolated-pixel correction.

    ``box_fn(array, radius)`` must return box-window means; supplying either the
    full or region variant keeps full and incremental arithmetic identical.
    """
    h, w = initial_map.shape
    window_area = float((2 * config.vote_radius + 1) ** 2)
    confident = (confidence >= config.isolated_confidence).astype(np.float64)
    confident_count = box_fn(confident, config.vote_radius) * window_area

    # Count confident neighbours preferring each focal plane.
    plane_counts = np.empty((n_planes, h, w), dtype=np.float64)
    for plane in range(n_planes):
        votes = ((initial_map == plane) & (confident > 0.5)).astype(np.float64)
        plane_counts[plane] = box_fn(votes, config.vote_radius) * window_area

    own_count = np.take_along_axis(
        plane_counts, initial_map[None, :, :], axis=0
    ).squeeze(0)
    other_counts = plane_counts.copy()
    np.put_along_axis(
        other_counts, initial_map[None, :, :], -1.0, axis=0
    )
    best_other = np.argmax(other_counts, axis=0).astype(np.int64)
    best_other_count = np.take_along_axis(
        other_counts, best_other[None, :, :], axis=0
    ).squeeze(0)

    total_conf = np.maximum(confident_count, 1.0)
    agreement = best_other_count / total_conf
    weak_own = confidence < config.isolated_confidence
    isolated = (
        weak_own
        & (confident_count >= 3.0)
        & (agreement >= config.isolated_min_agreement)
        & (best_other_count > own_count)
    )
    smoothed = np.where(isolated, best_other, initial_map)
    return smoothed.astype(np.int64), isolated


def smooth_selection(
    initial_map: np.ndarray,
    confidence: np.ndarray,
    n_planes: int,
    config: CoreConfig,
) -> ConsistencyResult:
    winner, entropy = _weighted_vote(
        initial_map, confidence, n_planes, config.vote_radius
    )
    critical_peak = confidence <= config.critical_peak_ratio
    smoothed, isolated_mask = _isolated_correction(
        initial_map, confidence, n_planes, config, box_fn=box_filter
    )
    vote_disagreement = entropy >= config.boundary_vote_entropy
    boundary_band = critical_peak | vote_disagreement | isolated_mask
    return ConsistencyResult(
        smoothed_map=smoothed,
        boundary_band=boundary_band,
        vote_entropy=entropy,
        isolated_corrections=int(isolated_mask.sum()),
        critical_peak_mask=critical_peak,
        vote_disagreement_mask=vote_disagreement,
    )


