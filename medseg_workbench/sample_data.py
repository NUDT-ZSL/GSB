from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np

from .io_utils import atomic_write_json


SAMPLE_SEED = 20261125
SAMPLE_SHAPE = (24, 48, 48)
SAMPLE_CHUNK = (8, 16, 16)


def _ellipsoid(
    shape: Tuple[int, int, int],
    center: Tuple[float, float, float],
    radii: Tuple[float, float, float],
) -> np.ndarray:
    z, y, x = np.indices(shape, dtype=np.float32)
    cz, cy, cx = center
    rz, ry, rx = radii
    return (
        ((z - cz) / rz) ** 2
        + ((y - cy) / ry) ** 2
        + ((x - cx) / rx) ** 2
        <= 1.0
    )


def generate_sample(root: Path | str, seed: int = SAMPLE_SEED, *, force: bool = False) -> Dict[str, Any]:
    root = Path(root)
    volume_dir = root / "volume"
    truth_dir = root / "truth"
    task_path = root / "task.json"
    if root.exists() and force:
        shutil.rmtree(root)
    volume_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)
    shape = SAMPLE_SHAPE
    truth = np.zeros(shape, dtype=bool)
    truth |= _ellipsoid(shape, (12.0, 24.0, 24.0), (6.5, 12.0, 12.0))
    truth |= _ellipsoid(shape, (6.0, 12.0, 36.0), (4.0, 7.5, 6.5))
    truth |= _ellipsoid(shape, (18.0, 36.0, 12.0), (4.5, 6.5, 6.5))
    truth |= _ellipsoid(shape, (12.0, 24.0, 10.0), (3.0, 5.0, 4.0))

    volume = rng.normal(loc=42.0, scale=11.0, size=shape)
    tissue_noise = rng.normal(loc=158.0, scale=14.0, size=shape)
    volume[truth] = tissue_noise[truth]

    # Deterministic salt-and-pepper outliers and thin imaging streaks.
    salt_count = 2200
    z_idx = rng.integers(0, shape[0], salt_count)
    y_idx = rng.integers(0, shape[1], salt_count)
    x_idx = rng.integers(0, shape[2], salt_count)
    salt_values = rng.choice([0.0, 238.0], size=salt_count)
    volume[z_idx, y_idx, x_idx] = salt_values

    for _ in range(10):
        z0 = int(rng.integers(1, shape[0] - 1))
        y0 = int(rng.integers(4, shape[1] - 4))
        x0 = int(rng.integers(0, shape[2] - 12))
        length = int(rng.integers(8, 20))
        volume[z0, y0 : y0 + 2, x0 : min(shape[2], x0 + length)] += rng.normal(
            28.0, 5.0, size=(2, min(length, shape[2] - x0))
        )

    volume = np.clip(volume, 0, 255).astype(np.uint8)
    truth_u8 = truth.astype(np.uint8)

    for index in range(shape[0]):
        np.save(volume_dir / f"slice_{index:04d}.npy", volume[index])
        np.save(truth_dir / f"slice_{index:04d}.npy", truth_u8[index])

    task = {
        "schema_version": "1.0",
        "name": "deterministic-boundary-demo",
        "seed": int(seed),
        "volume_dir": "volume",
        "truth_dir": "truth",
        "volume_pattern": "slice_{index:04d}.npy",
        "shape": list(shape),
        "spacing": [1.25, 0.8, 0.8],
        "chunk_size": list(SAMPLE_CHUNK),
        "region_growth": {
            "threshold": [105, 215],
            "seed_stride": [4, 8, 8],
            "growth_radius": 6,
        },
        "morphology": {
            "dilate_kernel": 3,
            "erode_kernel": 3,
            "dilate_iterations": 1,
            "erode_iterations": 1,
        },
        "parallelism": 4,
        "logic_time_budget_ms": 80,
        "pixel_memory_budget": 300000,
        "state_dir": "state",
        "temp_dir": "tmp",
        "export_dir": "exports",
    }
    atomic_write_json(task_path, task)
    return {"root": str(root), "task": str(task_path), "shape": list(shape)}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="samples/demo", type=Path)
    parser.add_argument("--seed", default=SAMPLE_SEED, type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    info = generate_sample(args.out, args.seed, force=args.force)
    print(info["task"])


if __name__ == "__main__":
    main()
