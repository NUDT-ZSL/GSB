#!/usr/bin/env python3
"""Generate the shipped deterministic demo scene.

No external assets, GPU or network access are used.  All numeric scene data
which can vary is derived from the fixed seed below through ``random.Random``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any


DEFAULT_SEED = 20261125
GENERATOR_VERSION = 1


def _round(value: float, digits: int = 5) -> float:
    return round(float(value), digits)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(output_dir: Path, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    bounds_min = [-6.0, -2.0, -10.0]
    bounds_max = [6.0, 8.0, 12.0]

    fog_size = [_round(rng.uniform(9.5, 11.5)), _round(rng.uniform(6.5, 7.5)), _round(rng.uniform(17.0, 19.0))]
    fog_center = [_round(rng.uniform(-0.6, 0.6)), _round(rng.uniform(2.0, 2.8)), _round(rng.uniform(1.0, 2.2))]
    media = [
        {
            "id": "fog_main",
            "kind": "fog",
            "center": fog_center,
            "size": fog_size,
            "extinction": [_round(rng.uniform(0.20, 0.24), 4) for _ in range(3)],
            "albedo": [_round(rng.uniform(0.70, 0.82), 4) for _ in range(3)],
            "ior": 1.0,
        },
        {
            "id": "water_overlap",
            "kind": "water",
            "center": [
                _round(rng.uniform(1.0, 1.8)),
                _round(rng.uniform(0.8, 1.4)),
                _round(rng.uniform(2.0, 2.8)),
            ],
            "size": [_round(rng.uniform(4.5, 5.5)), _round(rng.uniform(3.5, 4.5)), _round(rng.uniform(7.0, 9.0))],
            "extinction": [
                _round(rng.uniform(0.30, 0.38), 4),
                _round(rng.uniform(0.13, 0.18), 4),
                _round(rng.uniform(0.06, 0.10), 4),
            ],
            "albedo": [
                _round(rng.uniform(0.16, 0.24), 4),
                _round(rng.uniform(0.45, 0.58), 4),
                _round(rng.uniform(0.62, 0.74), 4),
            ],
            "ior": _round(rng.uniform(1.31, 1.35), 4),
        },
        {
            "id": "glass_plate",
            "kind": "glass",
            "center": [
                _round(rng.uniform(-2.6, -1.9)),
                _round(rng.uniform(1.8, 2.4)),
                _round(rng.uniform(3.1, 3.8)),
            ],
            "size": [_round(rng.uniform(0.24, 0.36)), _round(rng.uniform(4.8, 5.8)), _round(rng.uniform(4.6, 5.4))],
            "extinction": [_round(rng.uniform(0.035, 0.055), 4) for _ in range(3)],
            "albedo": [_round(rng.uniform(0.01, 0.025), 4) for _ in range(3)],
            "ior": _round(rng.uniform(1.48, 1.54), 4),
        },
        {
            "id": "haze_upper",
            "kind": "fog",
            "center": [
                _round(rng.uniform(-0.5, 0.5)),
                _round(rng.uniform(5.8, 6.3)),
                _round(rng.uniform(3.6, 4.2)),
            ],
            "size": [_round(rng.uniform(5.0, 6.0)), _round(rng.uniform(3.0, 4.0)), _round(rng.uniform(3.0, 4.0))],
            "extinction": [_round(rng.uniform(0.10, 0.14), 4) for _ in range(3)],
            "albedo": [_round(rng.uniform(0.60, 0.72), 4) for _ in range(3)],
            "ior": 1.0,
        },
    ]

    lights = [
        {
            "id": "key",
            "position": [
                _round(rng.uniform(2.4, 3.4)),
                _round(rng.uniform(4.6, 5.4)),
                _round(rng.uniform(-2.6, -1.6)),
            ],
            "intensity": _round(rng.uniform(55.0, 70.0), 4),
        },
        {
            "id": "fill",
            "position": [
                _round(rng.uniform(-3.4, -2.4)),
                _round(rng.uniform(1.6, 2.4)),
                _round(rng.uniform(4.3, 5.3)),
            ],
            "intensity": _round(rng.uniform(35.0, 48.0), 4),
        },
        {
            "id": "rim",
            "position": [
                _round(rng.uniform(-0.3, 0.3)),
                _round(rng.uniform(0.7, 1.3)),
                _round(rng.uniform(7.0, 8.0)),
            ],
            "intensity": _round(rng.uniform(62.0, 78.0), 4),
        },
    ]

    width, height = 48, 32
    samples_per_pixel = 2
    jitter = {
        "sample_offsets": [
            [_round(rng.uniform(0.15, 0.45), 5), _round(rng.uniform(0.55, 0.85), 5)],
            [_round(rng.uniform(0.55, 0.85), 5), _round(rng.uniform(0.15, 0.45), 5)],
        ],
        "step_scales": [1.0, 1.5, 2.0],
        "cutoff_scales": [1.0, 1.5, 2.0],
    }
    scene = {
        "schema_version": 1,
        "name": "fixed-seed-volumetric-demo",
        "seed": seed,
        "bounds": {"min": bounds_min, "max": bounds_max},
        "data": {
            "media": "media.json",
            "lights": "lights.json",
            "jitter": "jitter.json",
        },
        "camera": {
            "position": [_round(rng.uniform(-0.25, 0.25)), _round(rng.uniform(2.0, 2.35)), -9.0],
            "forward": [0.0, _round(rng.uniform(0.02, 0.06), 5), 1.0],
            "up": [0.0, 1.0, 0.0],
            "vertical_fov_deg": _round(rng.uniform(45.0, 50.0), 4),
            "aspect_ratio": _round(width / height, 8),
        },
        "render": {
            "width": width,
            "height": height,
            "samples_per_pixel": samples_per_pixel,
            "step_size": 0.28,
            "max_steps": 240,
            "transmittance_cutoff": 0.08,
            "fixed_scale": 8192,
            "transmittance_tolerance": 0.0002,
            "ray_epsilon": 1e-7,
            "exposure": 6.0,
            "tile": {"width": 16, "height": 16},
            "max_workers": 4,
            "inflight_memory_limit_bytes": 16 * 1024 * 1024,
            "state_dir": "../state",
            "output_dir": "../output",
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "media.json": {"media": media},
        "lights.json": {"lights": lights},
        "jitter.json": jitter,
        "scene.json": scene,
    }
    for name, payload in files.items():
        _write_json(output_dir / name, payload)
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "files": {
            name: {"sha256": _sha256(output_dir / name), "bytes": (output_dir / name).stat().st_size}
            for name in sorted(files)
        },
    }
    _write_json(output_dir / "seed_manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent.parent / "samples")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--force", action="store_true", help="accepted for scripts; generation is deterministic")
    args = parser.parse_args(argv)
    if args.seed < 0:
        parser.error("--seed must be nonnegative")
    manifest = generate(args.output_dir.resolve(), args.seed)
    print(
        json.dumps(
            {
                "ok": True,
                "seed": manifest["seed"],
                "output_dir": str(args.output_dir.resolve()),
                "files": manifest["files"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

