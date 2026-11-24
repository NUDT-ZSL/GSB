from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np

from .io_utils import atomic_write_bytes, atomic_write_json, read_json_file, sha256_bytes
from .png_codec import encode_gray_png, read_png_dimensions


def read_output_dimensions(path: Path) -> Tuple[int, ...] | None:
    if not path.exists():
        return None
    suffix = path.suffix.lower()
    try:
        if suffix == ".png":
            width, height = read_png_dimensions(path)
            return (height, width)
        if suffix == ".npy":
            array = np.load(path, mmap_mode="r", allow_pickle=False)
            try:
                return tuple(array.shape)
            finally:
                if hasattr(array, "_mmap"):
                    array._mmap.close()
        if suffix == ".npz":
            with np.load(path, allow_pickle=False) as archive:
                for key in ("gray", "depth", "height"):
                    if key in archive.files:
                        return tuple(archive[key].shape)
    except (OSError, ValueError):
        return None
    return None


def publish_file(
    path: str | Path,
    data: bytes,
    *,
    version: str,
    dimensions: Tuple[int, ...],
    kind: str,
) -> Dict[str, Any]:
    target = Path(path)
    metadata_path = Path(str(target) + ".meta.json")
    payload_sha = sha256_bytes(data)
    existing_dimensions = read_output_dimensions(target)
    existing_metadata = None
    try:
        existing_metadata = read_json_file(metadata_path)
    except (OSError, ValueError):
        existing_metadata = None

    same_version = bool(existing_metadata and existing_metadata.get("version") == version)
    same_dimensions = existing_dimensions == tuple(dimensions)
    if target.exists() and same_version and same_dimensions:
        return {
            "path": str(target),
            "action": "kept_same_version",
            "dimensions": list(dimensions),
            "sha256": existing_metadata.get("sha256", payload_sha),
        }

    if not target.exists():
        reason = "output_missing"
    elif not same_version:
        reason = "version_changed"
    elif not same_dimensions:
        reason = "dimensions_changed"
    else:
        reason = "content_changed"

    atomic_write_bytes(target, data)
    atomic_write_json(
        metadata_path,
        {
            "schema": "terrain-baker-output/v1",
            "kind": kind,
            "version": version,
            "dimensions": list(dimensions),
            "sha256": payload_sha,
        },
    )
    return {
        "path": str(target),
        "action": "atomic_replaced",
        "replace_reason": reason,
        "dimensions": list(dimensions),
        "sha256": payload_sha,
        "metadata_path": str(metadata_path),
    }


def publish_gray_png(path: Path, gray: np.ndarray, version: str) -> Dict[str, Any]:
    metadata = {"global_version": version[:64], "kind": "gray_lambert"}
    data = encode_gray_png(np.asarray(gray, dtype=np.uint8), metadata=metadata)
    return publish_file(
        path,
        data,
        version=version,
        dimensions=tuple(gray.shape),
        kind="gray_png",
    )


def publish_npy(path: Path, array: np.ndarray, version: str, kind: str) -> Dict[str, Any]:
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(array))
    return publish_file(
        path,
        buffer.getvalue(),
        version=version,
        dimensions=tuple(array.shape),
        kind=kind,
    )


def publish_npz(
    path: Path, arrays: Dict[str, np.ndarray], version: str, dimensions: Tuple[int, ...]
) -> Dict[str, Any]:
    buffer = io.BytesIO()
    np.savez(buffer, **arrays)
    return publish_file(path, buffer.getvalue(), version=version, dimensions=dimensions, kind="npz")


def depth_to_uint16_preview(depth: np.ndarray) -> Tuple[np.ndarray, Dict[str, float]]:
    finite = np.isfinite(depth)
    if not np.any(finite):
        return np.zeros(depth.shape, dtype=np.uint16), {"world_min": 0.0, "world_max": 0.0}
    world_min = float(np.min(depth[finite]))
    world_max = float(np.max(depth[finite]))
    preview = np.zeros(depth.shape, dtype=np.uint16)
    if world_max <= world_min:
        preview[finite] = 32767
        return preview, {"world_min": world_min, "world_max": world_max}
    scaled = (depth[finite] - world_min) / (world_max - world_min)
    preview[finite] = 1 + np.floor(np.clip(scaled, 0.0, 1.0) * 65533.0 + 0.5).astype(np.uint16)
    return preview, {"world_min": world_min, "world_max": world_max, "invalid_pixel_value": 0}
