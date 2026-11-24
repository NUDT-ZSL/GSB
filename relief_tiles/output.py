"""Stable sample, slice and report output helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .config import ReliefConfig
from .pngio import write_gray_png
from .renderer import PreviewResult, RenderResult


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_preview(
    result: PreviewResult,
    config: ReliefConfig,
    output_dir: str | Path,
) -> dict[str, str]:
    output_dir = Path(output_dir)
    stem = f"preview_r{result.row:02d}_c{result.col:02d}"
    png_path = output_dir / f"{stem}.png"
    npy_path = output_dir / f"{stem}.npy"
    json_path = output_dir / f"{stem}.json"
    write_gray_png(png_path, result.gray)
    np.save(npy_path, result.gray)
    _write_json(json_path, result.to_dict())
    return {
        "png": str(png_path),
        "npy": str(npy_path),
        "json": str(json_path),
    }


def save_render(
    result: RenderResult,
    config: ReliefConfig,
    output_dir: str | Path,
) -> dict[str, str]:
    output_dir = Path(output_dir)
    slice_dir = output_dir / "slices"
    slice_dir.mkdir(parents=True, exist_ok=True)
    for row, col in config.iter_block_indices():
        x0, y0, width, height = config.block_bounds(row, col)
        block = result.mosaic[y0 : y0 + height, x0 : x0 + width]
        write_gray_png(slice_dir / f"r{row:02d}_c{col:02d}.png", block)
    mosaic_png = output_dir / "mosaic.png"
    mosaic_npy = output_dir / "mosaic.npy"
    report_path = output_dir / "report.json"
    write_gray_png(mosaic_png, result.mosaic)
    np.save(mosaic_npy, result.mosaic)
    _write_json(report_path, result.report)
    return {
        "mosaic_png": str(mosaic_png),
        "mosaic_npy": str(mosaic_npy),
        "report": str(report_path),
        "slice_dir": str(slice_dir),
    }
