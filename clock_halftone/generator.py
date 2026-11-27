"""Deterministic, dependency-free clock-movement teaching sample generator."""

from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessError, require
from .io import sha256_file, write_json, write_pgm


DESIGN_SIZE = 512


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    if edge0 == edge1:
        return (x >= edge1).astype(np.float64)
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _gear_fields(
    xx: np.ndarray,
    yy: np.ndarray,
    center: tuple[float, float],
    root_radius: float,
    tip_radius: float,
    tooth_count: int,
    tooth_duty: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    cx, cy = center
    dx = xx - cx
    dy = yy - cy
    radius = np.hypot(dx, dy)
    theta = (np.arctan2(dy, dx) + 2.0 * np.pi) % (2.0 * np.pi)
    phase = (theta * tooth_count / (2.0 * np.pi)) % 1.0
    tooth = (radius >= root_radius) & (radius <= tip_radius) & (phase <= tooth_duty)
    body = radius <= root_radius
    return radius, theta, tooth, body


def _render_escape(
    xx: np.ndarray,
    yy: np.ndarray,
    scale: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    cx, cy = 160.0 * scale, 170.0 * scale
    root_r, tip_r = 74.0 * scale, 92.0 * scale
    hole_r, hub_r = 10.0 * scale, 18.0 * scale
    tooth_count = 18
    radius, theta, tooth, body = _gear_fields(
        xx, yy, (cx, cy), root_r, tip_r, tooth_count, tooth_duty=0.56
    )
    hole = radius < hole_r
    mask = (body | tooth) & ~hole
    phase_shift = float(seed % 360) * math.pi / 180.0
    radial_gradient = 0.61 - 0.22 * np.clip(radius / max(root_r, 1.0), 0.0, 1.0)
    body_tone = radial_gradient + 0.012 * np.cos(3.0 * theta + phase_shift)
    tooth_tone = 0.37 + 0.025 * np.cos(5.0 * theta - 0.4 * phase_shift)
    gray = body_tone
    gray = np.where(tooth, tooth_tone, gray)
    gray = np.where((radius < hub_r) & (radius >= hole_r), 0.30 + 0.03 * (radius / max(hub_r, 1.0)), gray)
    detail = {
        "id": "escape_teeth",
        "part_label": 1,
        "kind": "tooth",
        "bbox": _bbox(cx, cy, tip_r + 4.0 * scale, xx.shape),
        "center": [int(round(cx)), int(round(cy))],
        "root_radius_px": float(root_r),
        "tip_radius_px": float(tip_r),
        "tooth_count": tooth_count,
        "tooth_period_px": float(2.0 * math.pi * 0.5 * (root_r + tip_r) / tooth_count),
        "isolation_radius_px": float(tip_r + 4.0 * scale),
        "truth": {
            "expected_connected_body": True,
            "expected_root_breaks": 0,
            "expected_tip_adhesions": 0,
        },
    }
    return mask, gray, detail


def _render_hairspring(
    xx: np.ndarray,
    yy: np.ndarray,
    scale: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    cx, cy = 365.0 * scale, 145.0 * scale
    inner_r, outer_r = 13.0 * scale, 97.0 * scale
    dx = xx - cx
    dy = yy - cy
    radius = np.hypot(dx, dy)
    theta = np.arctan2(dy, dx)
    mask = (radius >= inner_r) & (radius <= outer_r)
    turns = 8.5
    phase_shift = float(seed % 97) / 97.0
    u = (radius - inner_r) / max(outer_r - inner_r, 1.0)
    phase = turns * u - theta / (2.0 * np.pi) + phase_shift
    profile = 0.5 * (1.0 + np.cos(2.0 * np.pi * phase))
    line_tone = 0.31 + 0.02 * np.cos(2.0 * np.pi * (3.0 * u + phase_shift))
    gap_tone = 0.77 + 0.05 * u
    # A smooth profile keeps the spiral's continuous tonal transition.
    tone = line_tone + (gap_tone - line_tone) * _smoothstep(0.10, 0.92, profile)
    gray = np.where(mask, tone, 0.0)
    detail = {
        "id": "hairspring_spiral",
        "part_label": 2,
        "kind": "spiral",
        "bbox": _bbox(cx, cy, outer_r + 4.0 * scale, xx.shape),
        "center": [int(round(cx)), int(round(cy))],
        "inner_radius_px": float(inner_r),
        "outer_radius_px": float(outer_r),
        "turns": turns,
        "spiral_pitch_px": float((outer_r - inner_r) / turns),
        "isolation_radius_px": float(outer_r + 4.0 * scale),
        "truth": {"continuous_tone": True, "expected_direction": "radial_spiral"},
    }
    return mask, gray, detail


def _render_gear_train(
    xx: np.ndarray,
    yy: np.ndarray,
    scale: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    angle_shift = float(seed % 360) * math.pi / 180.0
    mask = np.zeros(xx.shape, dtype=bool)
    gray = np.zeros(xx.shape, dtype=np.float64)
    details: list[dict[str, Any]] = []

    specs = [
        {
            "id": "gear_teeth",
            "center": (235.0 * scale, 380.0 * scale),
            "root": 78.0 * scale,
            "tip": 96.0 * scale,
            "hole": 13.0 * scale,
            "hub": 24.0 * scale,
            "teeth": 24,
            "duty": 0.58,
            "base": 0.66,
            "falloff": 0.19,
            "tooth_tone": 0.37,
            "phase": angle_shift,
        },
        {
            "id": "pinion_teeth",
            "center": (355.0 * scale, 300.0 * scale),
            "root": 28.0 * scale,
            "tip": 42.0 * scale,
            "hole": 6.0 * scale,
            "hub": 12.0 * scale,
            "teeth": 12,
            "duty": 0.55,
            "base": 0.70,
            "falloff": 0.14,
            "tooth_tone": 0.41,
            "phase": 0.6 * angle_shift,
        },
    ]
    for spec in specs:
        cx, cy = spec["center"]
        radius, theta, tooth, body = _gear_fields(
            xx, yy, (cx, cy), spec["root"], spec["tip"], spec["teeth"], spec["duty"]
        )
        hole = radius < spec["hole"]
        part = (body | tooth) & ~hole
        body_tone = (
            spec["base"]
            - spec["falloff"] * np.clip(radius / max(spec["root"], 1.0), 0.0, 1.0)
            + 0.012 * np.cos(2.0 * theta + spec["phase"])
        )
        tooth_tone = spec["tooth_tone"] + 0.018 * np.cos(8.0 * theta + spec["phase"])
        tone = np.where(tooth, tooth_tone, body_tone)
        tone = np.where(
            (radius < spec["hub"]) & (radius >= spec["hole"]),
            0.33 + 0.08 * (radius / max(spec["hub"], 1.0)),
            tone,
        )
        gray = np.where(part, tone, gray)
        mask |= part
        mid = 0.5 * (spec["root"] + spec["tip"])
        details.append(
            {
                "id": spec["id"],
                "part_label": 3,
                "kind": "tooth",
                "bbox": _bbox(cx, cy, spec["tip"] + 4.0 * scale, xx.shape),
                "center": [int(round(cx)), int(round(cy))],
                "root_radius_px": float(spec["root"]),
                "tip_radius_px": float(spec["tip"]),
                "tooth_count": int(spec["teeth"]),
                "tooth_period_px": float(2.0 * math.pi * mid / spec["teeth"]),
                "isolation_radius_px": float(spec["tip"] + 4.0 * scale),
                "truth": {
                    "expected_connected_body": True,
                    "expected_root_breaks": 0,
                    "expected_tip_adhesions": 0,
                },
            }
        )
    return mask, gray, details


def _render_bore(
    xx: np.ndarray,
    yy: np.ndarray,
    scale: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    cx, cy = 410.0 * scale, 420.0 * scale
    bore_r, outer_r = 14.0 * scale, 37.0 * scale
    radius = np.hypot(xx - cx, yy - cy)
    theta = np.arctan2(yy - cy, xx - cx)
    mask = (radius >= bore_r) & (radius <= outer_r)
    u = (radius - bore_r) / max(outer_r - bore_r, 1.0)
    tone = 0.30 + 0.34 * u + 0.025 * np.cos(5.0 * theta + float(seed % 23))
    gray = np.where(mask, tone, 0.0)
    detail = {
        "id": "arbor_bore",
        "part_label": 4,
        "kind": "bore",
        "bbox": _bbox(cx, cy, outer_r + 5.0 * scale, xx.shape),
        "center": [int(round(cx)), int(round(cy))],
        "bore_radius_px": float(bore_r),
        "outer_radius_px": float(outer_r),
        "isolation_radius_px": float(outer_r + 5.0 * scale),
        "truth": {"expected_hole_open": True, "expected_bore_closures": 0},
    }
    return mask, gray, detail


def _bbox(cx: float, cy: float, radius: float, shape: tuple[int, int]) -> list[int]:
    height, width = shape
    x0 = max(0, int(math.floor(cx - radius)))
    y0 = max(0, int(math.floor(cy - radius)))
    x1 = min(width, int(math.ceil(cx + radius)))
    y1 = min(height, int(math.ceil(cy + radius)))
    return [x0, y0, max(0, x1 - x0), max(0, y1 - y0)]


def _crop_from_bbox(array: np.ndarray, bbox: list[int]) -> np.ndarray:
    x, y, width, height = bbox
    return array[y : y + height, x : x + width]


def generate_clock_sample(
    out_dir: str | Path,
    *,
    seed: int = 20261128,
    size: int = 512,
) -> dict[str, Any]:
    """Generate gray matrix, masks, detail annotations, previews and a manifest."""
    require(isinstance(seed, int) and seed >= 0, "E_CONFIG", "seed 必须是正整数")
    require(isinstance(size, int) and 192 <= size <= 1024, "E_CONFIG", "size 必须在 [192, 1024]")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    scale = size / DESIGN_SIZE
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    background = np.full((size, size), 0.92, dtype=np.float64)
    # A deterministic paper vignette avoids a perfectly synthetic constant field.
    radial = np.hypot(xx - size / 2.0, yy - size / 2.0) / (size / 2.0)
    background -= 0.018 * np.clip(radial, 0.0, 1.0) ** 2

    gray = background.copy()
    mask = np.zeros((size, size), dtype=np.int16)
    details: list[dict[str, Any]] = []

    part_masks: list[tuple[int, np.ndarray, np.ndarray]] = []
    escape_mask, escape_gray, escape_detail = _render_escape(xx, yy, scale, seed)
    hair_mask, hair_gray, hair_detail = _render_hairspring(xx, yy, scale, seed)
    gear_mask, gear_gray, gear_details = _render_gear_train(xx, yy, scale, seed)
    bore_mask, bore_gray, bore_detail = _render_bore(xx, yy, scale, seed)
    part_masks.extend(
        [
            (1, escape_mask, escape_gray),
            (2, hair_mask, hair_gray),
            (3, gear_mask, gear_gray),
            (4, bore_mask, bore_gray),
        ]
    )

    overlap = np.zeros((size, size), dtype=np.int16)
    for label, part_mask, part_gray in part_masks:
        if np.any((overlap != 0) & part_mask):
            raise BusinessError("E_INTERNAL", "生成器零件几何发生重叠")
        overlap[part_mask] = label
        mask[part_mask] = label
        gray[part_mask] = part_gray[part_mask]
    details.extend([escape_detail, hair_detail, *gear_details, bore_detail])

    # Ensure detail ROIs really intersect the declared part and keep them in-bounds.
    for detail in details:
        label = int(detail["part_label"])
        x, y, w, h = detail["bbox"]
        region_mask = _crop_from_bbox(mask, detail["bbox"])
        require(w > 0 and h > 0, "E_DETAIL_REGION", f"细节区域 {detail['id']} 越界")
        require(np.any(region_mask == label), "E_DETAIL_REGION", f"细节区域 {detail['id']} 不含零件像素")
        require(
            np.any(region_mask == 0),
            "E_DETAIL_REGION",
            f"细节区域 {detail['id']} 缺少掩码边界，无法核验边缘偏移",
        )

    gray_u8 = np.clip(np.rint(gray * 255.0), 0, 255).astype(np.uint8)
    mask_i16 = mask.astype(np.int16)

    parts: list[dict[str, Any]] = []
    part_specs = [
        (1, "escape_wheel", "tooth", 0.015, "擒纵轮齿形"),
        (2, "balance_hairspring", "spiral", 0.004, "摆轮游丝螺旋"),
        (3, "gear_train", "tooth", 0.011, "齿轮系齿廓"),
        (4, "arbor_bore", "bore", 0.019, "轴心圆孔"),
    ]
    for label, name, texture, target_bias, display_name in part_specs:
        part_mask = mask_i16 == label
        source_ink = 1.0 - gray_u8[part_mask].astype(np.float64) / 255.0
        target_density = float(np.clip(source_ink.mean() + target_bias, 0.05, 0.95))
        parts.append(
            {
                "label": label,
                "name": name,
                "display_name": display_name,
                "texture_class": texture,
                "target_ink_density": target_density,
                "pixel_count": int(part_mask.sum()),
                "source_ink_mean": float(source_ink.mean()),
            }
        )

    details_doc: dict[str, Any] = {
        "schema": "clock-halftone-details/v1",
        "sample_id": f"watch_movement_seed_{seed}",
        "seed": seed,
        "canvas": {"width": size, "height": size, "bit_depth": 8},
        "mask": {
            "background_label": 0,
            "missing_label": -1,
            "labels": [0, 1, 2, 3, 4],
            "part_labels": [1, 2, 3, 4],
        },
        "parts": parts,
        "detail_regions": details,
        "generator": {
            "name": "deterministic_clock_movement_v1",
            "design_size": DESIGN_SIZE,
            "external_images_read": False,
        },
    }
    details_path = out / "details.json"
    write_json(details_path, details_doc)

    np.save(out / "gray.npy", gray_u8, allow_pickle=False)
    np.save(out / "mask.npy", mask_i16, allow_pickle=False)
    write_pgm(out / "gray_preview.pgm", gray_u8)
    palette = np.zeros_like(mask_i16, dtype=np.uint8)
    for label, level in ((1, 70), (2, 130), (3, 190), (4, 245)):
        palette[mask_i16 == label] = level
    write_pgm(out / "mask_preview.pgm", palette)

    sample_doc: dict[str, Any] = {
        "schema": "clock-halftone-sample/v1",
        "sample_id": details_doc["sample_id"],
        "seed": seed,
        "canvas": {"width": size, "height": size, "bit_depth": 8},
        "files": {
            "gray": "gray.npy",
            "mask": "mask.npy",
            "details": "details.json",
            "gray_preview": "gray_preview.pgm",
            "mask_preview": "mask_preview.pgm",
        },
        "hashes": {
            "gray.npy": sha256_file(out / "gray.npy"),
            "mask.npy": sha256_file(out / "mask.npy"),
            "details.json": sha256_file(details_path),
        },
        "mask_label_set": sorted(int(x) for x in np.unique(mask_i16)),
        "part_labels": [1, 2, 3, 4],
        "background_label": 0,
        "missing_label": -1,
    }
    write_json(out / "sample.json", sample_doc)
    return {
        "sample_dir": str(out.resolve()),
        "sample_id": sample_doc["sample_id"],
        "size": size,
        "seed": seed,
        "mask_label_set": sample_doc["mask_label_set"],
        "files": {key: str((out / value).resolve()) for key, value in sample_doc["files"].items()},
    }


def append_sample_catalog(catalog_path: str | Path, entry: dict[str, Any]) -> None:
    path = Path(catalog_path)
    if path.exists():
        catalog = json.loads(path.read_text(encoding="utf-8"))
    else:
        catalog = {"schema": "clock-halftone-sample-catalog/v1", "samples": []}
    catalog.setdefault("samples", [])
    catalog["samples"] = [
        item for item in catalog["samples"] if item.get("sample_id") != entry.get("sample_id")
    ]
    catalog["samples"].append(entry)
    catalog["samples"].sort(key=lambda item: item.get("sample_id", ""))
    write_json(path, catalog)
