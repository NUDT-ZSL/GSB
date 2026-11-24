from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import numpy as np

from .config import sample_config, sample_config_dict
from .pattern import PatternSynthesizer
from .png_io import atomic_write_bytes, write_gray_png
from .renderer import ReliefRenderer


def _np_bytes(array: np.ndarray) -> bytes:
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=False)
    return stream.getvalue()


def generate_sample(directory: str | Path) -> dict[str, Any]:
    """Generate the fixed config and local height/gray truth references."""

    root = Path(directory)
    truth_dir = root / "truth"
    truth_dir.mkdir(parents=True, exist_ok=True)

    config = sample_config()
    config_path = root / "sample_config.json"
    atomic_write_bytes(
        config_path,
        (json.dumps(sample_config_dict(), indent=2) + "\n").encode("utf-8"),
    )

    synthesizer = PatternSynthesizer(config)
    renderer = ReliefRenderer(config, synthesizer)
    stats = renderer.compute_global_stats()
    height = synthesizer.height_patch(0, config.width, 0, config.height)
    grayscale = renderer.render_full(stats)

    height_npy = truth_dir / "height.npy"
    grayscale_npy = truth_dir / "full.npy"
    height_png = truth_dir / "height.png"
    grayscale_png = truth_dir / "full.png"
    atomic_write_bytes(height_npy, _np_bytes(height))
    atomic_write_bytes(grayscale_npy, _np_bytes(grayscale))
    height_u8 = np.floor(
        (height - float(np.min(height)))
        * (255.0 / max(float(np.ptp(height)), np.finfo(float).tiny))
        + 0.5
    ).clip(0, 255).astype(np.uint8)
    write_gray_png(height_png, height_u8)
    write_gray_png(grayscale_png, grayscale)

    truth = {
        "ok": True,
        "config_path": str(config_path),
        "version": config.version,
        "canvas": {"width": config.width, "height": config.height},
        "grid": {"rows": config.grid_rows, "cols": config.grid_cols},
        "global_stats": stats.to_dict(),
        "height_sha256": hashlib.sha256(height.tobytes()).hexdigest(),
        "gray_sha256": hashlib.sha256(grayscale.tobytes()).hexdigest(),
        "outputs": {
            "height_npy": str(height_npy),
            "full_npy": str(grayscale_npy),
            "height_png": str(height_png),
            "full_png": str(grayscale_png),
        },
    }
    atomic_write_bytes(
        truth_dir / "truth.json",
        (json.dumps(truth, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return truth
