"""Deterministic, fully local sample bundle generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from .config import ReliefConfig, parse_config
from .gray import generate_full_gray
from .noise import synthesize_height
from .pngio import write_gray_png


SAMPLE_SEED = 20240517
_SAMPLE_LIGHT = (
    -0.451129236405377,
    -0.5513801778287941,
    0.7017565899639197,
)


def sample_config_dict(seed: int = SAMPLE_SEED) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "canvas": {"width": 320, "height": 240},
        "grid": {"rows": 4, "cols": 4},
        "seed": seed,
        "pattern": {
            "octaves": [
                {"frequency": 2.0, "amplitude": 0.55},
                {"frequency": 5.0, "amplitude": 0.28},
                {"frequency": 11.0, "amplitude": 0.14},
                {"frequency": 23.0, "amplitude": 0.07},
            ],
            "gradient_table_size": 256,
        },
        "light": {"direction": list(_SAMPLE_LIGHT)},
        "emboss": {
            "strength": 60.0,
            "normalization_min": 0,
            "normalization_max": 255,
            "quantization_levels": 256,
        },
        "camera": {
            "azimuth_deg": 45.0,
            "elevation_deg": 35.264389682754654,
            "distance": 2.5,
        },
        "slice": {"preview_row": 1, "preview_col": 2},
        "resources": {
            "concurrency_limit": 4,
            "memory_budget_pixels": 25000,
        },
    }


def sample_config(seed: int = SAMPLE_SEED) -> ReliefConfig:
    return parse_config(sample_config_dict(seed))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_samples(
    output_dir: str | Path = "samples",
    seed: int = SAMPLE_SEED,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    config = sample_config(seed)
    config_data = config.normalized()

    config_path = output / "config.json"
    config_path.write_text(
        json.dumps(config_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pattern_data = {
        "seed": config.seed,
        "canvas": config_data["canvas"],
        "pattern": config_data["pattern"],
        "light": config_data["light"],
        "camera": config_data["camera"],
    }
    pattern_path = output / "pattern.json"
    pattern_path.write_text(
        json.dumps(pattern_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    height = synthesize_height(config)
    gray = generate_full_gray(config)
    height_path = output / "reference_height.npy"
    gray_path = output / "reference_gray.npy"
    png_path = output / "reference_full.png"
    np.save(height_path, height)
    np.save(gray_path, gray)
    write_gray_png(png_path, gray)

    manifest = {
        "schema_version": 1,
        "seed": config.seed,
        "config_version": config.version,
        "canvas": config_data["canvas"],
        "grid": config_data["grid"],
        "files": {
            "config.json": _sha256_file(config_path),
            "pattern.json": _sha256_file(pattern_path),
            "reference_height.npy": _sha256_file(height_path),
            "reference_gray.npy": _sha256_file(gray_path),
            "reference_full.png": _sha256_file(png_path),
        },
    }
    manifest_path = output / "sample_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "output_dir": str(output),
        "config": str(config_path),
        "pattern": str(pattern_path),
        "reference_png": str(png_path),
        "reference_gray": str(gray_path),
        "reference_height": str(height_path),
        "manifest": str(manifest_path),
        "config_version": config.version,
    }
