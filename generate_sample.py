#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from image_diag.deterministic import (
    array_digest,
    canonical_json,
    deterministic_noise,
    sha256_json,
)
from image_diag.models import StyleParams


WIDTH = 112
HEIGHT = 112
SEED = 20261126


def _ellipse(shape: tuple[int, int], cx: float, cy: float, rx: float, ry: float) -> np.ndarray:
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    return ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0


def _superellipse(
    shape: tuple[int, int],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    power: float,
) -> np.ndarray:
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    return (np.abs(xx - cx) / rx) ** power + (np.abs(yy - cy) / ry) ** power <= 1.0


def _polygon(shape: tuple[int, int], points: list[list[float]]) -> np.ndarray:
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    points_array = np.asarray(points, dtype=np.float64)
    inside = np.ones(shape, dtype=bool)
    count = len(points_array)
    for index in range(count):
        x1, y1 = points_array[index]
        x2, y2 = points_array[(index + 1) % count]
        cross = (x2 - x1) * (yy - y1) - (y2 - y1) * (xx - x1)
        inside &= cross >= 0.0
    return inside


def build_sample() -> tuple[np.ndarray, np.ndarray, list[dict], StyleParams]:
    shape = (HEIGHT, WIDTH)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    warm = 0.50 + 0.13 * np.sin(xx / 13.0 + yy / 31.0)
    green = 0.48 + 0.10 * np.cos(yy / 15.0 - xx / 41.0)
    blue = 0.46 + 0.09 * np.sin((xx + yy) / 19.0)
    vignette = 1.0 - 0.18 * (
        ((xx - WIDTH / 2) / (WIDTH / 2)) ** 2
        + ((yy - HEIGHT / 2) / (HEIGHT / 2)) ** 2
    )
    noise = deterministic_noise(SEED, shape, salt=11)
    stripes = 0.018 * np.sin(xx * 1.7 + yy * 0.9)
    base = np.stack(
        [
            warm * vignette + 0.055 * noise + stripes,
            green * vignette + 0.050 * noise - 0.4 * stripes,
            blue * vignette + 0.045 * noise + 0.2 * stripes,
        ],
        axis=-1,
    )
    base = np.clip(base, 0.0, 1.0)

    region_defs = [
        {
            "id": "R1",
            "label": 1,
            "kind": "ellipse",
            "geometry": {"cx": 30.0, "cy": 31.0, "rx": 15.0, "ry": 11.0},
        },
        {
            "id": "R2",
            "label": 2,
            "kind": "superellipse",
            "geometry": {"cx": 82.0, "cy": 31.0, "rx": 17.0, "ry": 13.0, "power": 4.0},
        },
        {
            "id": "R3",
            "label": 3,
            "kind": "polygon",
            "geometry": {
                "points": [
                    [49.0, 73.0],
                    [69.0, 71.0],
                    [80.0, 88.0],
                    [62.0, 100.0],
                    [44.0, 89.0],
                ]
            },
        },
    ]
    mask = np.zeros(shape, dtype=np.uint8)
    shapes: list[np.ndarray] = []
    for item in region_defs:
        geometry = item["geometry"]
        if item["kind"] == "ellipse":
            local = _ellipse(shape, **geometry)
        elif item["kind"] == "superellipse":
            local = _superellipse(
                shape,
                geometry["cx"],
                geometry["cy"],
                geometry["rx"],
                geometry["ry"],
                geometry["power"],
            )
        elif item["kind"] == "polygon":
            local = _polygon(shape, geometry["points"])
        else:
            raise ValueError(f"unsupported sample region kind: {item['kind']}")
        if np.any(mask[local] != 0):
            raise ValueError(f"sample regions overlap at {item['id']}")
        mask[local] = item["label"]
        shapes.append(local)

    style = StyleParams.from_dict(
        {
            "strength": 0.74,
            "color_matrix": [
                [0.97, 0.08, -0.03],
                [-0.04, 0.95, 0.11],
                [0.06, -0.05, 1.02],
            ],
            "edge_preserve": 0.31,
            "iterations": 3,
            "precision": "float64",
            "fixed_scale": 4096,
            "fixed_rounding": "half_even",
            "boundary_radius": 3,
            "boundary_sigma": 1.45,
            "feather_radius": 3.2,
            "style_threshold": 0.60,
            "boundary_threshold": 0.085,
            "threshold_policy": "tolerance_band",
            "threshold_tolerance": 0.012,
            "error_tolerance": 0.01,
            "version": 1,
        }
    )
    return base, mask, region_defs, style


def write_sample(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    base, mask, regions, style = build_sample()
    np.save(out_dir / "base.npy", np.asarray(base, dtype=np.float64), allow_pickle=False)
    np.save(out_dir / "mask.npy", np.asarray(mask, dtype=np.uint8), allow_pickle=False)
    manifest = {
        "format": "deterministic-repair-sample-v1",
        "seed": SEED,
        "width": WIDTH,
        "height": HEIGHT,
        "base_file": "base.npy",
        "mask_file": "mask.npy",
        "base_sha256": array_digest(base),
        "mask_sha256": array_digest(mask),
        "regions": regions,
        "style": style.as_dict(),
        "style_sha256": sha256_json(style.as_dict()),
    }
    (out_dir / "sample.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "regions.json").write_text(
        canonical_json(regions) + "\n",
        encoding="utf-8",
    )
    (out_dir / "style.json").write_text(
        canonical_json(style.as_dict()) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the fixed-seed diagnostic sample.")
    parser.add_argument("--out", default="data/sample", help="Output sample directory")
    args = parser.parse_args()
    manifest = write_sample(Path(args.out))
    print(
        json.dumps(
            {
                "out": str(Path(args.out).resolve()),
                "seed": manifest["seed"],
                "size": [manifest["width"], manifest["height"]],
                "base_sha256": manifest["base_sha256"],
                "mask_sha256": manifest["mask_sha256"],
                "style_sha256": manifest["style_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
