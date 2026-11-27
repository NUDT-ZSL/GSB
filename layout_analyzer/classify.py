from __future__ import annotations

from typing import Any

import numpy as np

from .imageops import Foreground
from .model import Block
from .segment import MergedBlock


def classify_blocks(
    merged_blocks: list[MergedBlock],
    rgb: np.ndarray,
    foreground: Foreground,
    scale: float,
    reference_size: tuple[int, int],
) -> list[Block]:
    native_height, native_width = rgb.shape[:2]
    ref_width, ref_height = reference_size
    sx = ref_width / native_width
    sy = ref_height / native_height
    preliminary: list[dict[str, Any]] = []

    for merged in merged_blocks:
        features = _block_features(merged, rgb, foreground, scale, reference_size, sx, sy)
        preliminary.append(features)

    eligible_items = [
        item
        for item in preliminary
        if item["features"]["aspect_ratio"] <= 0.22
        and item["features"]["height_ratio"] >= 0.10
        and item["features"]["width_ratio"] <= 0.12
        and item["features"]["red_ratio"] < 0.08
    ]
    eligible_widths = [item["features"]["width_ref"] for item in eligible_items]
    if eligible_widths:
        main_column_width = float(np.median(eligible_widths))
    else:
        main_column_width = ref_width * 0.055
    annotation_cutoff = 0.84 * main_column_width
    body_like_centers = sorted(
        item["features"]["x_center_ref"]
        for item in eligible_items
        if item["features"]["width_ref"] > annotation_cutoff
    )
    center_gaps = [
        right - left
        for left, right in zip(body_like_centers, body_like_centers[1:])
        if right - left > main_column_width * 1.15
    ]
    if center_gaps:
        column_pitch = float(np.median(center_gaps))
    else:
        column_pitch = main_column_width * 1.9
    annotation_pitch_cutoff = 0.48 * column_pitch

    blocks: list[Block] = []
    for index, item in enumerate(sorted(preliminary, key=lambda value: (value["bbox"][1], value["bbox"][0]))):
        bbox = item["bbox"]
        features = {
            **item["features"],
            "main_column_width_ref": round(main_column_width, 3),
            "annotation_width_cutoff_ref": round(annotation_cutoff, 3),
            "column_pitch_ref": round(column_pitch, 3),
            "annotation_pitch_cutoff_ref": round(annotation_pitch_cutoff, 3),
        }
        block_type, confidence, evidence = _choose_type(features)
        blocks.append(
            Block(
                uid=f"vblk{index + 1:02d}",
                bbox=bbox,
                block_type=block_type,
                confidence=confidence,
                segmentation_confidence=item["merged"].confidence,
                features=features,
                evidence=evidence,
                component_count=len(item["merged"].component_ids),
            )
        )
    return blocks


def _block_features(
    merged: MergedBlock,
    rgb: np.ndarray,
    foreground: Foreground,
    scale: float,
    reference_size: tuple[int, int],
    sx: float,
    sy: float,
) -> dict[str, Any]:
    x0, y0, x1, y1 = merged.native_bbox
    ref_width, ref_height = reference_size
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)
    label_patch = foreground.labels[y0:y1, x0:x1]
    region = np.isin(label_patch, merged.component_ids)
    bbox_area = width * height
    ink_pixels = int(region.sum())
    ink_density = ink_pixels / bbox_area

    if region.any():
        red = (
            (rgb[y0:y1, x0:x1, 0].astype(np.int16) > rgb[y0:y1, x0:x1, 1].astype(np.int16) + 28)
            & (rgb[y0:y1, x0:x1, 0].astype(np.int16) > rgb[y0:y1, x0:x1, 2].astype(np.int16) + 20)
            & (rgb[y0:y1, x0:x1, 1].astype(np.int16) < 180)
        )
        red_ratio = float((red & region).sum() / max(1, ink_pixels))
    else:
        red_ratio = 0.0

    horizontal_transitions = np.count_nonzero(region[:, 1:] != region[:, :-1])
    vertical_transitions = np.count_nonzero(region[1:, :] != region[:-1, :])
    transitions = horizontal_transitions + vertical_transitions
    edge_density = transitions / bbox_area
    half_transitions = max(1.0, 0.5 * transitions)
    stroke_width_ref = (2.0 * ink_pixels / half_transitions) * sx
    internal_columns = _internal_column_count(region, scale)

    component_areas = {
        component.cid: component.area for component in foreground.components
    }
    group_component_area = sum(component_areas.get(cid, 0) for cid in merged.component_ids)
    largest_component_ratio = (
        max((component_areas.get(cid, 0) for cid in merged.component_ids), default=0)
        / max(1, group_component_area)
    )

    bbox_ref = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)
    width_ref = bbox_ref[2] - bbox_ref[0]
    height_ref = bbox_ref[3] - bbox_ref[1]
    features = {
        "width_ref": round(width_ref, 3),
        "height_ref": round(height_ref, 3),
        "aspect_ratio": round(width_ref / max(1.0, height_ref), 4),
        "width_ratio": round(width_ref / ref_width, 4),
        "height_ratio": round(height_ref / ref_height, 4),
        "area_ratio": round((width_ref * height_ref) / (ref_width * ref_height), 5),
        "top_margin_ratio": round(bbox_ref[1] / ref_height, 4),
        "bottom_margin_ratio": round(1.0 - bbox_ref[3] / ref_height, 4),
        "x_center_ref": round((bbox_ref[0] + bbox_ref[2]) / 2.0, 3),
        "y_center_ref": round((bbox_ref[1] + bbox_ref[3]) / 2.0, 3),
        "ink_density": round(ink_density, 4),
        "red_ratio": round(red_ratio, 4),
        "edge_density": round(edge_density, 4),
        "median_stroke_width_ref": round(stroke_width_ref, 3),
        "largest_component_ratio": round(largest_component_ratio, 4),
        "internal_column_count": internal_columns,
        "fragment_count": len(merged.component_ids),
    }
    return {"bbox": bbox_ref, "features": features, "merged": merged}


def _internal_column_count(region: np.ndarray, scale: float) -> int:
    if region.size == 0:
        return 0
    profile = region.sum(axis=0)
    if profile.size == 0 or profile.max() == 0:
        return 0
    active = profile >= max(1.0, profile.max() * 0.08)
    padded = np.concatenate(([False], active, [False]))
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    starts = changes[0::2]
    ends = changes[1::2]
    min_width = max(1, int(round(2 * scale)))
    merge_gap = max(1, int(round(2 * scale)))
    bands: list[tuple[int, int]] = []
    for start, end in zip(starts.tolist(), ends.tolist()):
        if end - start < min_width:
            continue
        if bands and start - bands[-1][1] <= merge_gap:
            bands[-1] = (bands[-1][0], end)
        else:
            bands.append((start, end))
    return len(bands)


def _choose_type(features: dict[str, Any]) -> tuple[str, float, dict[str, Any]]:
    red_ratio = features["red_ratio"]
    aspect = features["aspect_ratio"]
    ink_density = features["ink_density"]
    height_ratio = features["height_ratio"]
    width_ratio = features["width_ratio"]
    top_margin = features["top_margin_ratio"]
    largest = features["largest_component_ratio"]
    width_ref = features["width_ref"]
    annotation_cutoff = features["annotation_width_cutoff_ref"]
    annotation_pitch_cutoff = features["annotation_pitch_cutoff_ref"]

    seal_checks = [
        _check("red_ratio", red_ratio, 0.10, ">="),
        _check("aspect_ratio", aspect, 0.68, ">="),
        _check("aspect_ratio_upper", aspect, 1.48, "<="),
        _check("ink_density", ink_density, 0.035, ">="),
    ]
    if all(item["passed"] for item in seal_checks):
        margin = min(
            (red_ratio - 0.10) / 0.10,
            abs(0.68 - aspect) / 0.68 if aspect < 0.68 else abs(1.48 - aspect) / 1.48,
            max(0.0, (0.035 - ink_density) / 0.035),
        )
        confidence = min(0.99, 0.80 + 0.15 * min(1.0, max(0.0, margin)))
        return "seal", confidence, {
            "rule": "dense_red_square_seal_impression",
            "checks": seal_checks,
            "decision": "red ink and compact square-like extent dominate text rules",
        }

    illustration_checks = [
        _check("width_ratio", width_ratio, 0.10, ">="),
        _check("height_ratio", height_ratio, 0.15, ">="),
        _check("ink_density_lower", ink_density, 0.015, ">="),
        _check("ink_density_upper", ink_density, 0.58, "<="),
        _check("largest_component_ratio", largest, 0.28, ">="),
    ]
    if all(item["passed"] for item in illustration_checks):
        shape_margin = 1.0 if 0.25 <= aspect <= 2.6 else 0.0
        confidence = min(
            0.97,
            0.72
            + 0.16 * shape_margin
            + 0.08 * min(1.0, (largest - 0.28) / 0.40),
        )
        return "illustration", confidence, {
            "rule": "large_line_art_region_with_dominant_component",
            "checks": illustration_checks
            + [{"name": "shape_aspect_window", "value": aspect, "range": [0.25, 2.6], "passed": shape_margin > 0}],
            "decision": "large non-text line-art extent with a dominant connected frame",
        }

    header_checks = [
        _check("top_margin_ratio", top_margin, 0.12, "<="),
        _check("height_ratio", height_ratio, 0.22, "<="),
        _check("aspect_ratio", aspect, 0.18, ">="),
    ]
    if all(item["passed"] for item in header_checks):
        confidence = min(
            0.97,
            0.76
            + 0.12 * min(1.0, (0.12 - top_margin) / 0.10)
            + 0.07 * min(1.0, (0.22 - height_ratio) / 0.20),
        )
        return "header_note", confidence, {
            "rule": "top_margin_header_before_body_region",
            "checks": header_checks,
            "decision": "block starts in the upper margin and is too short to be a main column",
        }

    annotation_checks = [
        _check("width_ref", width_ref, annotation_cutoff, "<="),
        _check("width_ref_vs_column_pitch", width_ref, annotation_pitch_cutoff, "<="),
        _check("height_ratio", height_ratio, 0.30, "<="),
        _check("aspect_ratio", aspect, 0.25, "<="),
    ]
    if all(item["passed"] for item in annotation_checks):
        confidence = min(
            0.97,
            0.73
            + 0.14 * min(1.0, (annotation_cutoff - width_ref) / max(1.0, annotation_cutoff))
            + 0.08 * min(1.0, features["internal_column_count"] / 2.0),
        )
        return "inline_annotation", confidence, {
            "rule": "narrow_short_text_block_against_main_column_width_distribution",
            "checks": annotation_checks
            + [
                {
                    "name": "internal_column_count",
                    "value": features["internal_column_count"],
                    "threshold": 2,
                    "passed": features["internal_column_count"] >= 2,
                }
            ],
            "decision": "narrower than 84% of the median main-column width and vertically short",
        }

    body_checks = [
        _check("aspect_ratio", aspect, 0.22, "<="),
        _check("height_ratio", height_ratio, 0.12, ">="),
        _check("width_ref", width_ref, annotation_cutoff, ">"),
    ]
    confidence = min(
        0.98,
        0.74
        + 0.10 * min(1.0, features["height_ratio"] / 0.50)
        + 0.08 * min(1.0, features["ink_density"] / 0.30),
    )
    return "body_column", confidence, {
        "rule": "tall_vertical_text_column_wider_than_annotation_band",
        "checks": body_checks,
        "decision": "remaining vertical text block is a main body column",
    }


def _check(name: str, value: float, threshold: float, operator: str) -> dict[str, Any]:
    if operator == ">=":
        passed = bool(value >= threshold)
    elif operator == "<=":
        passed = bool(value <= threshold)
    elif operator == ">":
        passed = bool(value > threshold)
    else:
        raise ValueError(operator)
    return {
        "name": name,
        "value": round(float(value), 4),
        "operator": operator,
        "threshold": round(float(threshold), 4),
        "passed": passed,
    }
