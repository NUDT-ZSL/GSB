from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from .atomicio import atomic_write_json
from .config import canonical_json_bytes, sha256_hex, source_content_digest
from .pngio import png_bytes

SEED = 20261125


def _ramp_image(width: int, height: int, seed_offset: int) -> np.ndarray:
    rng = np.random.default_rng(SEED + seed_offset)
    x = np.linspace(0.0, 1.0, width, dtype=np.float64)[None, :]
    y = np.linspace(0.0, 1.0, height, dtype=np.float64)[:, None]
    diagonal = 0.5 + 0.5 * np.sin(2.0 * np.pi * (2.0 * x + 1.3 * y))
    radial = np.sqrt((x - 0.62) ** 2 + (y - 0.42) ** 2)
    radial_ramp = np.clip(radial / 0.75, 0.0, 1.0)
    value = 255.0 * (0.48 * x + 0.22 * y + 0.18 * diagonal + 0.12 * radial_ramp)
    value += rng.normal(0.0, 2.2, size=(height, width))
    return np.clip(np.rint(value), 0, 255).astype(np.uint8)


def _poster_image(width: int, height: int, seed_offset: int) -> np.ndarray:
    rng = np.random.default_rng(SEED + seed_offset)
    x_line = np.linspace(-1.0, 1.0, width)
    y_line = np.linspace(-1.0, 1.0, height)
    x, y = np.meshgrid(x_line, y_line)
    value = np.full((height, width), 238.0, dtype=np.float64)

    background_band = ((x + y + 2.0) * 90.0) % 180.0
    value -= background_band * 0.28

    circle = (x + 0.28) ** 2 + (y + 0.18) ** 2 <= 0.42**2
    circle_shade = 42.0 + 90.0 * ((x + 0.7) / 0.84)
    value[circle] = circle_shade[circle]

    rectangle = (np.abs(x - 0.38) < 0.34) & (np.abs(y + 0.34) < 0.20)
    rectangle_shade = 185.0 - 55.0 * (x + 1.0)
    value[rectangle] = rectangle_shade[rectangle]

    ring = np.abs(np.sqrt((x - 0.35) ** 2 + (y - 0.35) ** 2) - 0.27) < 0.035
    value[ring] = 18.0

    value += rng.normal(0.0, 1.6, size=value.shape)
    return np.clip(np.rint(value), 0, 255).astype(np.uint8)


def _type_ramp_image(width: int, height: int, seed_offset: int) -> np.ndarray:
    rng = np.random.default_rng(SEED + seed_offset)
    y = np.linspace(0.0, 1.0, height)[:, None]
    x = np.linspace(0.0, 1.0, width)[None, :]
    value = 255.0 * (0.18 + 0.72 * y) * np.ones((height, width))

    for band in range(9):
        top = int(height * (0.08 + band * 0.10))
        bottom = min(height, top + max(2, height // 36))
        left = int(width * 0.10)
        right = int(width * (0.90 - 0.05 * (band % 3)))
        value[top:bottom, left:right] = 25.0 + band * 24.0

    for column in range(6):
        x0 = int(width * (0.12 + column * 0.14))
        value[height // 2 : height // 2 + height // 5, x0 : x0 + width // 18] = (
            35.0 + column * 32.0
        )

    value += 0.08 * 255.0 * np.sin(2 * np.pi * 7 * x)
    value += rng.normal(0.0, 1.4, size=value.shape)
    return np.clip(np.rint(value), 0, 255).astype(np.uint8)


def _moire_image(width: int, height: int, seed_offset: int) -> np.ndarray:
    rng = np.random.default_rng(SEED + seed_offset)
    x = np.arange(width)[None, :]
    y = np.arange(height)[:, None]
    fine_a = 128.0 + 72.0 * np.sin(2.0 * np.pi * x / 7.0 + 0.03 * y)
    fine_b = 128.0 + 54.0 * np.sin(2.0 * np.pi * (x * 0.73 + y * 0.19) / 6.0)
    envelope = 0.5 + 0.5 * np.sin(2.0 * np.pi * y / max(1, height))
    value = envelope * fine_a + (1.0 - envelope) * fine_b
    value += rng.normal(0.0, 2.0, size=value.shape)
    return np.clip(np.rint(value), 0, 255).astype(np.uint8)


def sample_jobs() -> List[Dict[str, Any]]:
    return [
        {
            "job_id": "job_circle_320x240",
            "source_id": "gradient_wrap",
            "output_width": 320,
            "output_height": 240,
            "dpi": 300,
            "lpi": 24,
            "angle_deg": 15,
            "dot_shape": "circle",
            "ink_tone": "#174A8B",
            "ink_coverage_max": 0.94,
            "levels": 2,
            "tile_cols": 4,
            "tile_rows": 3,
            "moire_threshold": 0.60,
        },
        {
            "job_id": "job_ellipse_256",
            "source_id": "poster_shapes",
            "output_width": 256,
            "output_height": 256,
            "dpi": 300,
            "lpi": 30,
            "angle_deg": 30,
            "dot_shape": "ellipse",
            "ellipse_aspect": 1.55,
            "ink_tone": "#9B2F2F",
            "ink_coverage_max": 0.90,
            "levels": 4,
            "tile_cols": 4,
            "tile_rows": 4,
            "moire_threshold": 0.54,
        },
        {
            "job_id": "job_square_192x288",
            "source_id": "type_ramp",
            "output_width": 192,
            "output_height": 288,
            "dpi": 240,
            "lpi": 20,
            "angle_deg": 0,
            "dot_shape": "square",
            "ink_tone": "#1F6B45",
            "ink_coverage_max": 0.96,
            "levels": 8,
            "tile_cols": 3,
            "tile_rows": 3,
            "moire_threshold": 0.50,
        },
        {
            "job_id": "job_fine_288",
            "source_id": "poster_shapes",
            "output_width": 288,
            "output_height": 288,
            "dpi": 360,
            "lpi": 48,
            "angle_deg": 22.5,
            "dot_shape": "circle",
            "ink_tone": "#222222",
            "ink_coverage_max": 0.88,
            "levels": 2,
            "tile_cols": 6,
            "tile_rows": 6,
            "moire_threshold": 0.40,
        },
        {
            "job_id": "job_moire_512x384",
            "source_id": "moire_target",
            "output_width": 512,
            "output_height": 384,
            "dpi": 240,
            "lpi": 44,
            "angle_deg": 12,
            "dot_shape": "circle",
            "ink_tone": "#3B245F",
            "ink_coverage_max": 1.0,
            "levels": 2,
            "tile_cols": 8,
            "tile_rows": 6,
            "valid_region": [16, 16, 480, 352],
            "moire_threshold": 0.52,
        },
    ]


def sample_anomalies() -> List[Dict[str, Any]]:
    return [
        {
            "id": "inj_invalid_angle",
            "type": "invalid_angle",
            "job_id": "inj_bad_angle",
            "base_job_id": "job_circle_320x240",
            "mutations": {"angle_deg": 95.0},
            "expected_code": "E_ANGLE",
        },
        {
            "id": "inj_grid_division",
            "type": "grid_not_divisible",
            "job_id": "inj_bad_grid",
            "base_job_id": "job_circle_320x240",
            "mutations": {"tile_cols": 7},
            "expected_code": "E_GRID",
        },
        {
            "id": "inj_missing_shape",
            "type": "missing_dot_shape",
            "job_id": "inj_missing_shape",
            "base_job_id": "job_ellipse_256",
            "mutations": {},
            "remove": ["dot_shape", "shape"],
            "expected_code": "E_SHAPE",
        },
        {
            "id": "inj_corrupt_digest",
            "type": "corrupt_persisted_digest",
            "job_id": "inj_corrupt_digest",
            "base_job_id": "job_square_192x288",
            "tile_index": 0,
            "expected_code": "E_DIGEST",
        },
    ]


def generate_samples(root: str | Path, *, force: bool = False) -> Dict[str, Any]:
    root_path = Path(root)
    samples_dir = root_path / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    (root_path / "state").mkdir(exist_ok=True)
    (root_path / "out").mkdir(exist_ok=True)

    manifest_path = samples_dir / "images.json"
    jobs_path = samples_dir / "jobs.json"
    anomalies_path = samples_dir / "anomalies.json"
    if not force and manifest_path.exists() and jobs_path.exists() and anomalies_path.exists():
        return {"skipped": True, "samples_dir": str(samples_dir)}

    image_specs = [
        ("gradient_wrap", "gradient_wrap.png", 160, 120, _ramp_image),
        ("poster_shapes", "poster_shapes.png", 128, 128, _poster_image),
        ("type_ramp", "type_ramp.png", 96, 144, _type_ramp_image),
        ("moire_target", "moire_target.png", 161, 119, _moire_image),
    ]
    images: List[Dict[str, Any]] = []
    for index, (source_id, file_name, width, height, builder) in enumerate(image_specs):
        pixels = builder(width, height, index * 17 + 3)
        image_path = samples_dir / file_name
        data = png_bytes(pixels)
        image_path.write_bytes(data)
        images.append(
            {
                "source_id": source_id,
                "file": file_name,
                "width": width,
                "height": height,
                "content_sha256": source_content_digest(pixels),
                "png_sha256": sha256_hex(data),
                "seed": SEED,
            }
        )

    manifest = {
        "schema": "halftone-sample-manifest/v1",
        "seed": SEED,
        "images": images,
    }
    jobs_document = {
        "schema": "halftone-jobs/v1",
        "seed": SEED,
        "jobs": sample_jobs(),
    }
    anomalies_document = {
        "schema": "halftone-anomalies/v1",
        "seed": SEED,
        "anomalies": sample_anomalies(),
    }
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(jobs_path, jobs_document)
    atomic_write_json(anomalies_path, anomalies_document)
    return {
        "skipped": False,
        "samples_dir": str(samples_dir),
        "images": len(images),
        "jobs": len(sample_jobs()),
        "anomalies": len(sample_anomalies()),
        "canonical_jobs_sha256": sha256_hex(canonical_json_bytes(jobs_document)),
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local halftone samples")
    parser.add_argument("--root", default=".", help="project root containing samples/")
    parser.add_argument("--force", action="store_true", help="regenerate sample files")
    args = parser.parse_args(argv)
    result = generate_samples(args.root, force=args.force)
    print(__import__("json").dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
