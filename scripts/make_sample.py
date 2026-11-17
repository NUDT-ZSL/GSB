"""Generate a deterministic local sample description (no external resources).

Usage::

    python scripts/make_sample.py samples/sample.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys


def _unit(x: float, y: float, z: float):
    norm = math.sqrt(x * x + y * y + z * z)
    return [x / norm, y / norm, z / norm]


def build_description() -> dict:
    return {
        "version": 1,
        "camera": {
            "width": 24,
            "height": 24,
            "samples_per_pixel": 4,
        },
        "material": {
            "initial": {"albedo": 0.25, "roughness": 0.7},
            "truth": {"albedo": 0.55, "roughness": 0.25},
            "f0": 0.04,
            "ambient": 0.02,
            "learning_rate": {"albedo": 0.01, "roughness": 0.02},
            "projection": {"mode": "clamp"},
        },
        "lights": [
            {"direction": _unit(0.5, 0.3, 0.8), "color": [0.9, 0.85, 0.8]},
            {"direction": _unit(-0.4, 0.6, 0.7), "color": [0.35, 0.45, 0.6]},
        ],
        "optimization": {
            "iterations": 80,
            "loss_rel_tol": 1.0e-3,
            "loss_abs_tol": 1.0e-8,
            "grad_norm_tol": 1.0e-2,
            "convergence_window": 3,
            "checkpoint_every": 10,
            "tail_size": 5,
        },
        "gradient_check": {
            "step": 1.0e-5,
            "tolerance": 1.0e-6,
            "compare": "absolute_or_relative",
        },
        "scheduler": {
            "tile_width": 8,
            "tile_height": 8,
            "max_workers": 4,
        },
        "memory": {
            "buffer_byte_budget": 4 * 1024 * 1024,
        },
        "output": {
            "output_dir": "out/run",
            "state_dir": "out/state",
            "target_file": "out/target.npy",
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="generate a deterministic sample")
    parser.add_argument("path", nargs="?", default="samples/sample.json")
    args = parser.parse_args(argv)

    description = build_description()
    os.makedirs(os.path.dirname(os.path.abspath(args.path)), exist_ok=True)
    with open(args.path, "w", encoding="utf-8") as handle:
        json.dump(description, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
