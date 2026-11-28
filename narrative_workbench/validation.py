from __future__ import annotations

import itertools
from typing import Any

import numpy as np

from .compositing import ALPHA_EPSILON, PAIR_ALPHA_EPSILON, SampleStack
from .models import CameraFrame, GaussianElement


def _bbox_from_mask(mask: np.ndarray) -> list[int] | None:
    coordinates = np.argwhere(mask)
    if coordinates.size == 0:
        return None
    y0, x0 = np.min(coordinates, axis=0)
    y1, x1 = np.max(coordinates, axis=0)
    return [int(x0), int(y0), int(x1), int(y1)]


def _bbox_intersection(a: list[int], b: list[int]) -> list[int] | None:
    x0 = max(int(a[0]), int(b[0]))
    y0 = max(int(a[1]), int(b[1]))
    x1 = min(int(a[2]), int(b[2]))
    y1 = min(int(a[3]), int(b[3]))
    if x0 > x1 or y0 > y1:
        return None
    return [x0, y0, x1, y1]


def _bbox_area(box: list[int] | None) -> int:
    if box is None:
        return 0
    return max(0, int(box[2]) - int(box[0]) + 1) * max(
        0, int(box[3]) - int(box[1]) + 1
    )


def _bbox_iou(a: list[int] | None, b: list[int] | None) -> float:
    if a is None or b is None:
        return 0.0
    intersection = _bbox_area(_bbox_intersection(a, b))
    union = _bbox_area(a) + _bbox_area(b) - intersection
    return float(intersection / union) if union else 0.0


def _sample_violation_pixels(
    mask: np.ndarray,
    depth_a: np.ndarray,
    depth_b: np.ndarray,
    id_a: str,
    id_b: str,
    limit: int = 16,
) -> list[dict[str, Any]]:
    coordinates = np.argwhere(mask)
    samples: list[dict[str, Any]] = []
    for y, x in coordinates[:limit]:
        da = float(depth_a[y, x])
        db = float(depth_b[y, x])
        expected = [id_a, id_b] if da > db else [id_b, id_a]
        samples.append(
            {
                "pixel": [int(x), int(y)],
                "depths": {id_a: da, id_b: db},
                "depth_gap": abs(da - db),
                "required_near_to_far_order": [expected[1], expected[0]],
            }
        )
    return samples


def analyze_order_and_coverage(
    elements: list[GaussianElement],
    projected: list[dict[str, Any]],
    samples: SampleStack,
    draw_order: list[int],
    rgba_draw: np.ndarray,
    rgba_pixel_reference: np.ndarray,
    rgba_layer_approximation: np.ndarray,
    camera: CameraFrame,
) -> dict[str, Any]:
    count = samples.count
    draw_rank = {stack_index: rank for rank, stack_index in enumerate(draw_order)}
    coverage_mask = np.any(samples.alphas >= ALPHA_EPSILON, axis=0)
    violation_mask = np.zeros((camera.height, camera.width), dtype=bool)
    suspicious_pairs: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for first, second in itertools.combinations(range(count), 2):
        alpha_a = samples.alphas[first]
        alpha_b = samples.alphas[second]
        pair_mask = (alpha_a >= PAIR_ALPHA_EPSILON) & (
            alpha_b >= PAIR_ALPHA_EPSILON
        )
        overlap_count = int(np.count_nonzero(pair_mask))
        if overlap_count == 0:
            continue
        element_a = elements[samples.indices[first]]
        element_b = elements[samples.indices[second]]
        depth_a = samples.depths[first]
        depth_b = samples.depths[second]
        centroid_a = float(projected[first]["centroid_depth"])
        centroid_b = float(projected[second]["centroid_depth"])
        depth_gap = abs(centroid_a - centroid_b)
        close_threshold = max(0.085, 0.032 * max(abs(centroid_a), abs(centroid_b)))
        depth_close = depth_gap <= close_threshold
        interval_overlap = depth_gap <= 2.0 * (
            float(projected[first]["depth_sigma"])
            + float(projected[second]["depth_sigma"])
        )
        rank_a = draw_rank[first]
        rank_b = draw_rank[second]
        a_drawn_first = rank_a < rank_b
        a_farther_at_pixel = depth_a > depth_b
        reversal_mask = pair_mask & (a_farther_at_pixel != a_drawn_first)
        reversal_count = int(np.count_nonzero(reversal_mask))
        bbox_a = [int(v) for v in projected[first]["render_bbox"]]
        bbox_b = [int(v) for v in projected[second]["render_bbox"]]
        overlap_bbox = _bbox_intersection(bbox_a, bbox_b)
        reasons: list[str] = []
        if depth_close:
            reasons.append("depth_proximity")
        if interval_overlap:
            reasons.append("covariance_depth_overlap")
        if overlap_count > 0:
            reasons.append("projected_footprint_overlap")
        if reversal_count > 0:
            reasons.append("per_pixel_order_reversal")
        global_pair_order = [first, second] if rank_a < rank_b else [second, first]
        pair_record = {
            "pair_id": f"{element_a.element_id}|{element_b.element_id}",
            "element_a": element_a.element_id,
            "element_b": element_b.element_id,
            "layer_a": element_a.layer_name,
            "layer_b": element_b.layer_name,
            "centroid_depths": {
                element_a.element_id: centroid_a,
                element_b.element_id: centroid_b,
            },
            "depth_gap": float(depth_gap),
            "depth_close_threshold": float(close_threshold),
            "depth_interval_overlap": bool(interval_overlap),
            "projected_bbox_iou": float(_bbox_iou(bbox_a, bbox_b)),
            "overlap_pixel_count": overlap_count,
            "overlap_bbox": overlap_bbox,
            "global_draw_order_near_to_far": [
                samples.ids[global_pair_order[1]],
                samples.ids[global_pair_order[0]],
            ],
            "per_pixel_order_reversal_count": reversal_count,
            "sort_sensitive": bool(reasons),
            "reasons": reasons,
        }
        suspicious_pairs.append(pair_record)
        if reversal_count == 0:
            continue
        violation_mask |= reversal_mask
        participating_depths = np.concatenate(
            [depth_a[reversal_mask], depth_b[reversal_mask]]
        )
        violation_bbox = _bbox_from_mask(reversal_mask)
        order_at_pixels = (
            [element_b.element_id, element_a.element_id]
            if a_drawn_first
            else [element_a.element_id, element_b.element_id]
        )
        violation = {
            **pair_record,
            "violation_pixel_count": reversal_count,
            "violation_bbox": violation_bbox,
            "conflict_depth": float(np.mean(participating_depths)),
            "conflict_depth_range": [
                float(np.min(participating_depths)),
                float(np.max(participating_depths)),
            ],
            "primitive_mean_violation_depths": {
                element_a.element_id: float(np.mean(depth_a[reversal_mask])),
                element_b.element_id: float(np.mean(depth_b[reversal_mask])),
            },
            "draw_order_violated_at_pixels": order_at_pixels,
            "pixel_samples": _sample_violation_pixels(
                reversal_mask,
                depth_a,
                depth_b,
                element_a.element_id,
                element_b.element_id,
            ),
        }
        violations.append(violation)
    suspicious_pairs.sort(key=lambda item: (-item["per_pixel_order_reversal_count"], item["pair_id"]))
    violations.sort(key=lambda item: (-item["violation_pixel_count"], item["pair_id"]))

    image_delta = np.abs(rgba_draw - rgba_pixel_reference)
    max_draw_pixel_delta = float(np.max(image_delta))
    mean_draw_pixel_delta = float(np.mean(image_delta[..., :3]))
    impossible = bool(np.any(violation_mask))
    degradation: dict[str, Any] = {
        "per_primitive_and_per_pixel_order_coexist": not impossible,
        "degradation_applied": False,
        "reason": None,
        "comparison_reference": "per_pixel_plane_depth_reference",
    }
    if impossible:
        fallback_delta = np.abs(rgba_layer_approximation - rgba_pixel_reference)
        changed_mask = np.any(fallback_delta > 1.0 / 255.0, axis=2)
        changed_count = int(np.count_nonzero(changed_mask))
        degradation = {
            "per_primitive_and_per_pixel_order_coexist": False,
            "degradation_applied": True,
            "reason": "centroid draw order cannot reproduce per-pixel plane-depth order",
            "strategy": "narrative_layer_stacked_approximation",
            "comparison_reference": "per_pixel_plane_depth_reference",
            "range": {
                "bbox": _bbox_from_mask(changed_mask),
                "pixel_count": changed_count,
                "pixel_fraction": float(
                    changed_count / max(1, camera.width * camera.height)
                ),
            },
            "max_channel_difference": float(np.max(fallback_delta)),
            "mean_abs_rgb_difference": float(np.mean(fallback_delta[..., :3])),
            "raw_draw_order_max_channel_difference": max_draw_pixel_delta,
            "layer_fallback_max_channel_difference": float(np.max(fallback_delta)),
        }
    checks = {
        "all_composite_values_finite": bool(
            np.isfinite(rgba_draw).all()
            and np.isfinite(rgba_pixel_reference).all()
            and np.isfinite(rgba_layer_approximation).all()
        ),
        "rgba_in_unit_interval": bool(
            np.min(rgba_draw) >= -1.0e-12 and np.max(rgba_draw) <= 1.0 + 1.0e-12
        ),
        "all_visible_layers_resolved": all(
            elements[samples.indices[index]].layer_index >= 0
            and bool(elements[samples.indices[index]].layer_name)
            for index in range(count)
        ),
        "near_or_far_culled_contribution_count": 0,
        "undefined_contribution_count": int(
            np.count_nonzero(~np.isfinite(samples.alphas))
            + np.count_nonzero(~np.isfinite(samples.depths))
        ),
        "pair_alpha_epsilon": PAIR_ALPHA_EPSILON,
        "sample_alpha_epsilon": ALPHA_EPSILON,
    }
    return {
        "coverage_mask": coverage_mask,
        "violation_mask": violation_mask,
        "suspicious_pairs": suspicious_pairs,
        "sorting_violations": violations,
        "degradation": degradation,
        "metrics": {
            "coverage_pixel_count": int(np.count_nonzero(coverage_mask)),
            "coverage_fraction": float(
                np.count_nonzero(coverage_mask)
                / max(1, camera.width * camera.height)
            ),
            "sort_sensitive_pair_count": len(suspicious_pairs),
            "sorting_violation_pair_count": len(violations),
            "sorting_violation_pixel_count": int(np.count_nonzero(violation_mask)),
            "max_draw_order_vs_pixel_reference_channel_delta": max_draw_pixel_delta,
            "mean_draw_order_vs_pixel_reference_rgb_delta": mean_draw_pixel_delta,
        },
        "checks": checks,
    }
