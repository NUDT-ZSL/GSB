"""Quality report with region based, auditable evidence."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .config import CoreConfig
from .filters import local_variance
from .io_stack import FocusStack
from .pipeline import PipelineResult


def _edge_mask(plane_map: np.ndarray) -> np.ndarray:
    """Pixels adjacent to a focal-plane label change (focus region boundary)."""
    shifted = [
        np.roll(plane_map, 1, axis=0),
        np.roll(plane_map, -1, axis=0),
        np.roll(plane_map, 1, axis=1),
        np.roll(plane_map, -1, axis=1),
    ]
    edge = np.zeros(plane_map.shape, dtype=bool)
    for neighbour in shifted:
        edge |= neighbour != plane_map
    return edge


def _plane_distribution(
    selected: np.ndarray, mask: np.ndarray, n_planes: int
) -> Dict[str, int]:
    distribution: Dict[str, int] = {}
    for plane in range(n_planes):
        distribution[f"plane_{plane}"] = int(
            np.count_nonzero((selected == plane) & mask)
        )
    return distribution


def _region_stats(
    stack: FocusStack,
    result: PipelineResult,
    mask: np.ndarray,
    config: CoreConfig,
    ideal_plane: Optional[np.ndarray],
) -> Dict[str, Any]:
    pixel_count = int(mask.sum())
    stats: Dict[str, Any] = {
        "pixel_count": pixel_count,
        "mean_confidence": (
            float(result.confidence[mask].mean()) if pixel_count else 0.0
        ),
        "plane_distribution": _plane_distribution(
            result.selected_map, mask, stack.n_planes
        ),
    }
    if pixel_count:
        contrast_before = np.zeros(stack.shape, dtype=np.float64)
        for frame in stack.frames:
            contrast_before += local_variance(
                frame.astype(np.float64), config.contrast_radius
            )
        contrast_before /= stack.n_planes
        contrast_after = local_variance(result.fused, config.contrast_radius)
        ratio = contrast_after / np.where(
            contrast_before > 0.0, contrast_before, 1.0
        )
        stats["local_contrast_improvement_ratio"] = float(ratio[mask].mean())
    else:
        stats["local_contrast_improvement_ratio"] = 0.0

    edge = _edge_mask(result.selected_map) & mask
    stats["focus_boundary_pixels"] = int(edge.sum())
    if ideal_plane is not None:
        mismatches = (result.selected_map != ideal_plane) & mask
        stats["mismatch_count"] = int(mismatches.sum())
        stats["boundary_mismatch_count"] = int(
            np.count_nonzero(mismatches & edge)
        )
        stats["boundary_accuracy"] = (
            1.0
            - stats["boundary_mismatch_count"] / max(stats["focus_boundary_pixels"], 1)
        )
        # Store a compact evidence sample (first mismatches, capped).
        evidence_rows, evidence_cols = np.where(mismatches & edge)
        evidence: List[Dict[str, int]] = []
        for row, col in zip(evidence_rows[:10], evidence_cols[:10]):
            evidence.append(
                {
                    "row": int(row),
                    "col": int(col),
                    "selected": int(result.selected_map[row, col]),
                    "ideal": int(ideal_plane[row, col]),
                }
            )
        stats["mismatch_evidence"] = evidence
    return stats


def build_report(
    stack: FocusStack,
    result: PipelineResult,
    config: CoreConfig,
) -> Dict[str, Any]:
    """Build high-confidence vs uncertain (boundary band) quality statistics."""
    boundary = result.boundary_band
    high_confidence = ~boundary
    full_mask = np.ones(stack.shape, dtype=bool)

    ledger_sums = result.ledger.sum(axis=0)
    report = {
        "stack_id": stack.stack_id,
        "image_shape": list(stack.shape),
        "n_planes": stack.n_planes,
        "adaptive_window_radii": list(result.radii),
        "noise_floor": result.noise_floor,
        "isolated_corrections": result.isolated_corrections,
        "ledger": {
            "min_sum": float(ledger_sums.min()),
            "max_sum": float(ledger_sums.max()),
            "normalised": bool(np.allclose(ledger_sums, 1.0, atol=1e-12)),
        },
        "regions": {
            "high_confidence": _region_stats(
                stack, result, high_confidence, config, stack.ideal_plane
            ),
            "uncertain_boundary_band": _region_stats(
                stack, result, boundary, config, stack.ideal_plane
            ),
            "full_image": _region_stats(
                stack, result, full_mask, config, stack.ideal_plane
            ),
        },
        "frame_summaries": [s.to_dict() for s in stack.summaries],
    }
    return report
