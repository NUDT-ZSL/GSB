"""Atomic tile persistence, digest checks and version-aware export."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
from PIL import Image

from .errors import E_DIGEST, E_STATE, fail
from .hashing import array_digest, digest_canonical


INTERRUPT_MARKER = "INTERRUPTED.json"
GRACEFUL_EXIT_CODE = 130


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)


def remove_stale_temporaries(directory: Path) -> None:
    if not directory.exists():
        return
    for path in directory.glob("*.tmp"):
        try:
            path.unlink()
        except OSError:
            pass


def archive_changed_version(state_dir: Path, version_id: str) -> Optional[str]:
    """Archive old tile state before processing a changed input version."""

    state_dir.mkdir(parents=True, exist_ok=True)
    tiles_dir = state_dir / "tiles"
    version_path = state_dir / "version.json"
    old_version: Optional[str] = None
    if version_path.exists():
        try:
            old_version = str(json.loads(version_path.read_text(encoding="utf-8")).get("version_id"))
        except (json.JSONDecodeError, OSError):
            old_version = None

    if old_version == version_id:
        remove_stale_temporaries(tiles_dir)
        return None

    if tiles_dir.exists() and any(tiles_dir.iterdir()):
        archive_root = state_dir / "archive"
        archive_root.mkdir(exist_ok=True)
        base_name = old_version or "unknown"
        destination = archive_root / base_name
        counter = 1
        while destination.exists():
            destination = archive_root / f"{base_name}-{counter}"
            counter += 1
        shutil.move(str(tiles_dir), str(destination))
        if version_path.exists():
            shutil.copy2(version_path, destination / "version.json")
        archived = str(destination.relative_to(state_dir)) if destination.is_relative_to(state_dir) else str(destination)
    else:
        archived = None

    tiles_dir.mkdir(parents=True, exist_ok=True)
    remove_stale_temporaries(tiles_dir)
    atomic_write_json(
        version_path,
        {"version_id": version_id, "format": "surface-stitch-state-v1"},
    )
    return archived


@dataclass
class StoredTile:
    tile_id: str
    version_id: str
    arrays: Dict[str, np.ndarray]
    manifest: Dict[str, Any]


def _tile_paths(state_dir: Path, tile_id: str) -> Tuple[Path, Path]:
    tiles_dir = state_dir / "tiles"
    return tiles_dir / f"{tile_id}.manifest.json", tiles_dir / f"{tile_id}.npz"


def commit_tile(
    state_dir: Path,
    tile_id: str,
    version_id: str,
    arrays: Dict[str, np.ndarray],
    metrics: Dict[str, Any],
) -> Dict[str, Any]:
    manifest_path, data_path = _tile_paths(state_dir, tile_id)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_arrays = {
        name: np.ascontiguousarray(array, dtype=np.float32)
        for name, array in arrays.items()
    }
    digest = array_digest(normalized_arrays)
    metrics_digest = digest_canonical(metrics)
    height, width = normalized_arrays["aligned"].shape
    manifest = {
        "status": "complete",
        "format": "surface-stitch-tile-v1",
        "tile_id": tile_id,
        "version_id": version_id,
        "width": int(width),
        "height": int(height),
        "declared_pixels": int(width * height),
        "array_names": sorted(normalized_arrays),
        "array_digest": digest,
        "metrics_digest": metrics_digest,
        "metrics": metrics,
    }
    manifest["block_digest"] = digest_canonical(
        {
            "tile_id": tile_id,
            "version_id": version_id,
            "array_digest": digest,
            "metrics_digest": metrics_digest,
            "declared_pixels": manifest["declared_pixels"],
        }
    )

    temporary_data = data_path.with_name(data_path.name + ".tmp")
    with temporary_data.open("wb") as handle:
        np.savez(handle, **normalized_arrays)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_data, data_path)
    atomic_write_json(manifest_path, manifest)
    return manifest


def verify_tile(
    state_dir: Path,
    tile_id: str,
    width: int,
    height: int,
    version_id: str,
) -> Tuple[Optional[StoredTile], Dict[str, Any]]:
    manifest_path, data_path = _tile_paths(state_dir, tile_id)
    if not manifest_path.exists():
        return None, {
            "tile_id": tile_id,
            "status": "missing",
            "code": E_DIGEST,
            "valid": False,
            "message": "tile manifest/digest is absent",
        }

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise fail(E_DIGEST, "tile manifest is unreadable JSON", tile_id=tile_id, detail=str(exc))

    if manifest.get("version_id") != version_id:
        raise fail(
            E_DIGEST,
            "cached tile version does not match current version",
            tile_id=tile_id,
            expected=version_id,
            got=manifest.get("version_id"),
        )
    declared_pixels = manifest.get("declared_pixels")
    if (
        int(manifest.get("width", -1)) != width
        or int(manifest.get("height", -1)) != height
        or not isinstance(declared_pixels, int)
        or declared_pixels != width * height
    ):
        raise fail(
            E_DIGEST,
            "declared pixel count or dimensions do not match tile",
            tile_id=tile_id,
            expected_pixels=width * height,
            declared_pixels=declared_pixels,
        )
    if not data_path.exists():
        raise fail(E_DIGEST, "tile digest exists but NPZ pixel data is missing", tile_id=tile_id)

    try:
        with np.load(data_path) as bundle:
            arrays = {name: np.asarray(bundle[name]).copy() for name in bundle.files}
    except Exception as exc:
        raise fail(E_DIGEST, f"tile pixel data is unreadable: {exc}", tile_id=tile_id)

    required = {"aligned", "weight", "contribution"}
    if not required.issubset(arrays):
        raise fail(E_DIGEST, "tile NPZ is missing required arrays", tile_id=tile_id, missing=sorted(required - set(arrays)))
    for name in required:
        if arrays[name].shape != (height, width):
            raise fail(E_DIGEST, "stored array shape does not match declaration", tile_id=tile_id, array=name)

    actual_digest = array_digest({name: arrays[name].astype(np.float32) for name in required})
    if actual_digest != manifest.get("array_digest"):
        raise fail(
            E_DIGEST,
            "tile array digest mismatch",
            tile_id=tile_id,
            expected=manifest.get("array_digest"),
            actual=actual_digest,
        )
    metrics = manifest.get("metrics", {})
    if digest_canonical(metrics) != manifest.get("metrics_digest"):
        raise fail(E_DIGEST, "tile metrics digest mismatch", tile_id=tile_id)

    check = {
        "tile_id": tile_id,
        "status": "valid",
        "code": None,
        "valid": True,
        "block_digest": manifest.get("block_digest"),
    }
    stored = StoredTile(tile_id, version_id, arrays, manifest)
    return stored, check


def write_interrupt_marker(state_dir: Path, payload: Dict[str, Any]) -> Path:
    path = state_dir / INTERRUPT_MARKER
    atomic_write_json(path, payload)
    return path


def clear_interrupt_marker(state_dir: Path) -> None:
    path = state_dir / INTERRUPT_MARKER
    if path.exists():
        path.unlink()


def read_png_version(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        with Image.open(path) as image:
            return image.text.get("surface_stitch_version")
    except Exception:
        return None


def read_json_report_version(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("version_id") or payload.get("report", {}).get("version_id"))
    except Exception:
        return None


def export_bytes_if_version_changed(
    path: Path,
    data: bytes,
    version_id: str,
    existing_version: Optional[str],
) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and existing_version == version_id:
        return {"path": str(path), "replaced": False, "reason": "same_version"}
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise fail(E_STATE, f"atomic export failed for {path}: {exc}")
    return {"path": str(path), "replaced": True, "reason": "version_changed_or_missing"}
