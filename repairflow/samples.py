"""Deterministic local sample generation and loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .config import normalize_config
from .hashing import array_content_hash, sha256_bytes, sha256_json
from .mask import MaskState, Region, make_region_mask, validate_mask_state

DEFAULT_SEED = 20261124
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 240
SAMPLE_VERSION = "RF-SAMPLE-V1"

SAMPLE_REGIONS = (
    (1, (66, 72), (22, 28)),
    (2, (70, 232), (24, 30)),
    (3, (176, 158), (26, 36)),
    (4, (188, 52), (18, 24)),
)

SAMPLE_CONFIG: dict[str, Any] = {
    "context_radius": 5,
    "style": {
        "style_strength": 0.42,
        "color_matrix": [
            [1.06, 0.02, -0.02],
            [0.01, 0.97, 0.04],
            [-0.02, 0.03, 1.07],
        ],
        "edge_preserve": 0.62,
        "iterations": 7,
        "precision": "high",
        "blend": {"feather_px": 4},
    },
    "limits": {
        "max_regions": 64,
        "max_resident_pixels": 150_000_000,
        "max_parallel_regions": 4,
        "resident_node_copies": 6,
    },
}


def generate_deterministic_base(
    seed: int = DEFAULT_SEED,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> np.ndarray:
    """Generate an RGB image using only a local PCG64 stream and fixed arrays."""

    rng = np.random.Generator(np.random.PCG64(int(seed)))
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)
    checker = (((yy // 10).astype(np.int64) + (xx // 10).astype(np.int64)) % 2) * 12.0
    waves = (
        np.sin(xx / 17.0 + yy / 41.0) * 15.0
        + np.cos(yy / 12.0) * 8.0
    )
    red = 72.0 + 120.0 * (xx / max(1, width - 1)) + checker + waves
    green = 58.0 + 96.0 * (yy / max(1, height - 1)) + 0.55 * checker - 0.4 * waves
    blue = 92.0 + 48.0 * ((xx + yy) / max(1, width + height - 2)) + 0.3 * waves
    base = np.stack((red, green, blue), axis=-1)
    base += rng.normal(0.0, 5.5, size=base.shape)
    return np.clip(np.rint(base), 0, 255).astype(np.uint8)


def generate_deterministic_masks(
    shape: tuple[int, int],
) -> MaskState:
    height, width = shape
    scale_y = height / DEFAULT_HEIGHT
    scale_x = width / DEFAULT_WIDTH
    regions = []
    for region_id, center, radii in SAMPLE_REGIONS:
        scaled_center = (
            int(round(center[0] * scale_y)),
            int(round(center[1] * scale_x)),
        )
        scaled_radii = (
            max(1, int(round(radii[0] * scale_y))),
            max(1, int(round(radii[1] * scale_x))),
        )
        regions.append(
            Region(region_id, make_region_mask(shape, scaled_center, scaled_radii))
        )
    state = MaskState(tuple(regions))
    validate_mask_state(state, canvas_shape=shape)
    return state


def build_sample_payload(
    seed: int = DEFAULT_SEED,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> tuple[np.ndarray, MaskState, dict[str, Any]]:
    base = generate_deterministic_base(seed=seed, width=width, height=height)
    masks = generate_deterministic_masks((height, width))
    config = normalize_config(SAMPLE_CONFIG)
    return base, masks, config


def write_ppm(path: str | Path, image: np.ndarray) -> None:
    array = np.asarray(image)
    if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("PPM output requires an HxWx3 uint8 image")
    height, width = array.shape[:2]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(f"P6\n{width} {height}\n255\n".encode("ascii"))
        handle.write(np.ascontiguousarray(array).tobytes())


def mask_overlay(base: np.ndarray, mask_state: MaskState) -> np.ndarray:
    image = np.asarray(base).astype(np.float64) * 0.55
    palette = {
        1: (235, 92, 76),
        2: (56, 172, 225),
        3: (245, 189, 66),
        4: (92, 205, 122),
    }
    for region in mask_state.regions:
        color = np.asarray(palette.get(region.region_id, (230, 230, 230)), dtype=np.float64)
        image[region.mask] = 0.15 * image[region.mask] + 0.85 * color
        boundary = region.mask & ~_erode_once(region.mask)
        image[boundary] = 255.0
    return np.clip(np.rint(image), 0, 255).astype(np.uint8)


def _erode_once(mask: np.ndarray) -> np.ndarray:
    result = mask.copy()
    result[1:, :] &= mask[:-1, :]
    result[:-1, :] &= mask[1:, :]
    result[:, 1:] &= mask[:, :-1]
    result[:, :-1] &= mask[:, 1:]
    return result


def write_sample_bundle(
    output_dir: str | Path = "samples/data",
    *,
    seed: int = DEFAULT_SEED,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> dict[str, Any]:
    output = Path(output_dir)
    masks_dir = output / "masks"
    masks_dir.mkdir(parents=True, exist_ok=True)

    base, masks, config = build_sample_payload(seed=seed, width=width, height=height)
    base_path = output / "base.npy"
    np.save(base_path, base, allow_pickle=False)
    mask_files: dict[str, str] = {}
    for region in masks.regions:
        path = masks_dir / f"region_{region.region_id:03d}.npy"
        np.save(path, region.mask, allow_pickle=False)
        mask_files[str(region.region_id)] = str(path.relative_to(output)).replace("\\", "/")

    config_path = output / "style.json"
    config_path.write_text(
        json.dumps(config, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    base_preview = output / "base.ppm"
    mask_preview = output / "mask_overlay.ppm"
    write_ppm(base_preview, base)
    write_ppm(mask_preview, mask_overlay(base, masks))

    manifest = {
        "schema": SAMPLE_VERSION,
        "seed": int(seed),
        "shape": [int(height), int(width), 3],
        "base_file": "base.npy",
        "base_hash": array_content_hash(base),
        "mask_files": mask_files,
        "mask_checksum": masks.checksum,
        "style_file": "style.json",
        "style_checksum": sha256_json(config),
        "region_ids": list(masks.region_ids),
        "files": {},
    }
    for relative in [
        "base.npy",
        "style.json",
        "base.ppm",
        "mask_overlay.ppm",
        *sorted(mask_files.values()),
    ]:
        payload = (output / relative).read_bytes()
        manifest["files"][relative] = sha256_bytes(payload)
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_sample(data_dir: str | Path = "samples/data") -> tuple[np.ndarray, MaskState, dict[str, Any]]:
    root = Path(data_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"sample manifest not found: {manifest_path}; run samples/generate_samples.py first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SAMPLE_VERSION:
        raise ValueError("unsupported sample schema")
    for relative, expected_hash in manifest.get("files", {}).items():
        payload = (root / relative).read_bytes()
        if sha256_bytes(payload) != expected_hash:
            raise ValueError(f"sample file hash mismatch: {relative}")

    base = np.load(root / manifest["base_file"], allow_pickle=False)
    if array_content_hash(base) != manifest["base_hash"]:
        raise ValueError("sample base hash does not match manifest")
    regions = []
    for region_id in manifest["region_ids"]:
        relative = manifest["mask_files"][str(region_id)]
        mask = np.load(root / relative, allow_pickle=False)
        regions.append(Region(int(region_id), mask))
    mask_state = MaskState(tuple(regions))
    if mask_state.checksum != manifest["mask_checksum"]:
        raise ValueError("sample mask checksum does not match manifest")
    config = json.loads((root / manifest["style_file"]).read_text(encoding="utf-8"))
    normalized = normalize_config(config)
    if sha256_json(normalized) != manifest["style_checksum"]:
        raise ValueError("sample style checksum does not match manifest")
    return base, mask_state, normalized
