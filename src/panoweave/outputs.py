"""Persistence helpers for render artifacts and hashes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from .pipeline import RenderResult
from .pngutil import heatmap_png, mask_png, weights_rgb, write_png


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_render_outputs(result: RenderResult, output_dir: str | Path) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {
        "panorama_png": root / "panorama.png",
        "panorama_npy": root / "panorama.npy",
        "valid_mask_npy": root / "valid_mask.npy",
        "valid_mask_png": root / "valid_mask.png",
        "weight_sum_npy": root / "weight_sum.npy",
        "weight_map_npy": root / "weight_map.npy",
        "weight_map_png": root / "weight_map.png",
        "weight_heatmap_png": root / "weight_heatmap.png",
        "manifest_json": root / "render_manifest.json",
    }
    write_png(paths["panorama_png"], result.panorama_rgb)
    np.save(paths["panorama_npy"], result.panorama_rgb, allow_pickle=False)
    np.save(paths["valid_mask_npy"], result.valid_mask, allow_pickle=False)
    write_png(paths["valid_mask_png"], mask_png(result.valid_mask))
    np.save(paths["weight_sum_npy"], result.weight_sum, allow_pickle=False)
    np.save(paths["weight_map_npy"], result.weight_map, allow_pickle=False)
    write_png(paths["weight_map_png"], weights_rgb(result.weight_map))
    write_png(paths["weight_heatmap_png"], heatmap_png(result.weight_sum))
    file_hashes = {
        key: file_sha256(path)
        for key, path in paths.items()
        if key != "manifest_json"
    }
    manifest = {
        "schema": 1,
        "render": result.report(),
        "files": file_hashes,
    }
    paths["manifest_json"].write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    file_hashes["manifest_json"] = file_sha256(paths["manifest_json"])
    return {
        "root": str(root),
        "files": file_hashes,
        "render": result.report(),
    }

