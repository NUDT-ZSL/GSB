"""Tile-level persistence, version identification and atomic writes.

Each tile is committed as two files:

* ``<id>.npz`` holds gray/depth arrays and the dependency summary;
* ``<id>.manifest.json`` is the commit marker carrying the content version,
  pixel counts and array digests.

Writes always go to a same-directory temporary file followed by
``os.replace`` (atomic on the same volume).  When a previously committed
tile has a different version identifier the old pair is moved into
``archive/<old-version>/`` first, so a state directory can never silently mix
new and old tile generations.
"""

from __future__ import annotations
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

from . import digest, errors
from .model import TaskSpec, TileSpec


def state_path(task: TaskSpec, root: Path) -> Path:
    return root / task.state_dir


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _atomic_write_npz(path: Path, gray: np.ndarray, depth: np.ndarray,
                      dependency: Dict[str, int]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        np.savez_compressed(
            handle,
            gray=gray,
            depth=depth,
            dependency=np.asarray(
                [dependency["min_i"], dependency["min_j"],
                 dependency["max_i"], dependency["max_j"]],
                dtype=np.int64),
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def parameters_digest(task: TaskSpec, terrain_version: str) -> str:
    """Version identifier shared by every tile of a given input state."""
    camera = task.camera
    payload = {
        "terrain": terrain_version,
        "lights": [list(light.direction) for light in task.lights],
        "ambient": task.ambient,
        "camera": {
            "position": list(camera.position),
            "target": list(camera.target),
            "up": list(camera.up),
            "width": camera.width,
            "height": camera.height,
            "near": camera.near,
            "far": camera.far,
        },
        "width_px": task.width_px,
        "height_px": task.height_px,
        "samples": task.samples,
        "ray_step_mm": task.ray_step_mm,
    }
    return digest.digest_canonical(payload)


@dataclass(frozen=True)
class StoredTile:
    tile_id: str
    version: str
    gray: np.ndarray
    depth: np.ndarray
    dependency: Dict[str, int]
    manifest: Dict[str, Any]


def _archive_old(state_dir: Path, tile_id: str, old_manifest: Dict[str, Any]
                 ) -> None:
    old_version = str(old_manifest.get("version", "unknown"))
    archive_dir = state_dir / "archive" / old_version
    archive_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (".npz", ".manifest.json"):
        source = state_dir / f"{tile_id}{suffix}"
        if source.exists():
            shutil.move(str(source), str(archive_dir / source.name))


def commit_tile(task: TaskSpec, root: Path, tile: TileSpec,
                gray: np.ndarray, depth: np.ndarray,
                dependency: Dict[str, int], version: str,
                corrupt_digest: bool = False) -> Dict[str, Any]:
    """Persist one tile, archiving any older generation first.

    ``corrupt_digest`` is used by the exception-injection tests to write a
    manifest whose array digest deliberately mismatches the data.
    """
    state_dir = state_path(task, root)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = state_dir / f"{tile.tile_id}.manifest.json"
    array_digest = digest.digest_tile_arrays(gray, depth)
    if corrupt_digest:
        array_digest = "0" * len(array_digest)
    dependency_digest = digest.digest_canonical(dependency)

    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as handle:
            old_manifest = json.load(handle)
        if old_manifest.get("version") != version:
            _archive_old(state_dir, tile.tile_id, old_manifest)

    npz_path = state_dir / f"{tile.tile_id}.npz"
    _atomic_write_npz(npz_path, gray, depth, dependency)
    manifest = {
        "tile_id": tile.tile_id,
        "version": version,
        "pixel_width": int(tile.width),
        "pixel_height": int(tile.height),
        "declared_pixels": int(tile.width * tile.height),
        "array_digest": array_digest,
        "dependency": dependency,
        "dependency_digest": dependency_digest,
        "format": 1,
    }
    _atomic_write_text(
        manifest_path, json.dumps(manifest, sort_keys=True, indent=2))
    return manifest


def load_tile(task: TaskSpec, root: Path, tile: TileSpec,
              expected_version: str,
              expect_corrupt: bool = False) -> Optional[StoredTile]:
    """Load and fully validate a cached tile.

    Returns ``None`` when nothing is committed.  Raises ``E_DIGEST`` for a
    manifest/npz mismatch, wrong declared pixel count or wrong version, so
    callers can fall back to re-rendering instead of silently using a stale
    or damaged block.
    """
    state_dir = state_path(task, root)
    manifest_path = state_dir / f"{tile.tile_id}.manifest.json"
    npz_path = state_dir / f"{tile.tile_id}.npz"
    if not manifest_path.exists():
        return None
    if not npz_path.exists():
        raise errors.fail(
            errors.E_DIGEST, "tile manifest exists but pixel data is missing",
            tile=tile.tile_id)
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except json.JSONDecodeError as exc:
        raise errors.fail(errors.E_DIGEST,
                          "tile manifest is not valid JSON",
                          tile=tile.tile_id, detail=exc.msg)

    if manifest.get("version") != expected_version:
        raise errors.fail(
            errors.E_DIGEST, "stale tile version",
            tile=tile.tile_id,
            expected=expected_version, got=manifest.get("version"))
    if int(manifest.get("pixel_width", -1)) != tile.width or \
            int(manifest.get("pixel_height", -1)) != tile.height or \
            int(manifest.get("declared_pixels", -1)) != tile.width * tile.height:
        raise errors.fail(
            errors.E_DIGEST, "declared pixel count does not match tile spec",
            tile=tile.tile_id,
            declared=manifest.get("declared_pixels"),
            expected=tile.width * tile.height)

    try:
        with npz_path.open("rb") as handle:
            bundle = np.load(handle)
            gray = bundle["gray"].copy()
            depth = bundle["depth"].copy()
            dep_array = bundle["dependency"].copy()
    except Exception as exc:  # numpy raises a variety of errors on corruption
        raise errors.fail(errors.E_DIGEST,
                          f"tile pixel data unreadable: {exc}",
                          tile=tile.tile_id)

    if gray.shape != (tile.height, tile.width) or \
            depth.shape != (tile.height, tile.width):
        raise errors.fail(
            errors.E_DIGEST, "stored array shape does not match tile spec",
            tile=tile.tile_id, gray_shape=list(gray.shape),
            depth_shape=list(depth.shape))
    actual_digest = digest.digest_tile_arrays(gray, depth)
    if actual_digest != manifest.get("array_digest"):
        raise errors.fail(
            errors.E_DIGEST, "tile array digest mismatch",
            tile=tile.tile_id,
            expected=manifest.get("array_digest"),
            actual=actual_digest)
    dep_list = [int(value) for value in dep_array.tolist()]
    dependency = {"min_i": dep_list[0], "min_j": dep_list[1],
                  "max_i": dep_list[2], "max_j": dep_list[3]}
    if digest.digest_canonical(dependency) != \
            manifest.get("dependency_digest"):
        raise errors.fail(errors.E_DIGEST,
                          "tile dependency digest mismatch",
                          tile=tile.tile_id)
    return StoredTile(tile.tile_id, expected_version, gray, depth,
                      dependency, manifest)


def write_pgm(path: Path, gray: np.ndarray) -> None:
    """Write an 8-bit binary PGM (P5) image atomically."""
    if gray.dtype != np.uint8 or gray.ndim != 2:
        raise errors.fail(errors.E_IO, "PGM input must be uint8 2-D")
    tmp = path.with_name(path.name + ".tmp")
    header = f"P5\n{gray.shape[1]} {gray.shape[0]}\n255\n".encode("ascii")
    with tmp.open("wb") as handle:
        handle.write(header)
        handle.write(np.ascontiguousarray(gray).tobytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_depth(path: Path, depth: np.ndarray) -> None:
    """Write a little-endian float32 depth map atomically."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(np.ascontiguousarray(depth, dtype="<f4").tobytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_pgm_header(path: Path) -> Dict[str, Any]:
    """Read dimensions from an existing PGM before replacing it."""
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        tokens = handle.readline().split()
        while len(tokens) < 4:
            line = handle.readline()
            if not line:
                break
            tokens.extend(line.split())
    if len(tokens) < 4:
        raise errors.fail(errors.E_IO, "cannot parse existing PGM header",
                          path=str(path))
    return {"magic": tokens[0].decode(), "width": int(tokens[1]),
            "height": int(tokens[2]), "maxval": int(tokens[3])}


def read_edit_cursor(task: TaskSpec, root: Path) -> Dict[str, Any]:
    """Return the persisted edit cursor (``{}`` when never appended)."""
    path = state_path(task, root) / "edit_cursor.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        raise errors.fail(errors.E_STATE,
                          "edit cursor is not valid JSON", path=str(path))


def write_edit_cursor(task: TaskSpec, root: Path,
                      payload: Dict[str, Any]) -> None:
    state_dir = state_path(task, root)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "edit_cursor.json"
    _atomic_write_text(path, json.dumps(payload, sort_keys=True, indent=2))


def write_terrain_snapshot(task: TaskSpec, root: Path) -> str:
    """Persist the current edited height field and return its digest."""
    state_dir = state_path(task, root)
    state_dir.mkdir(parents=True, exist_ok=True)
    heights = np.ascontiguousarray(task.terrain.heights, dtype=np.int64)
    version = digest.digest_heights(heights)
    path = state_dir / "terrain_snapshot.npy"
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        np.save(handle, heights)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return version


def read_terrain_snapshot(task: TaskSpec, root: Path):
    """Return the persisted edited height field, or ``None``."""
    path = state_path(task, root) / "terrain_snapshot.npy"
    if not path.exists():
        return None
    try:
        heights = np.load(path)
    except Exception as exc:  # corrupted snapshot must not be trusted
        raise errors.fail(errors.E_DIGEST,
                          f"terrain snapshot unreadable: {exc}")
    if heights.shape != (task.terrain.rows_ny, task.terrain.rows_nx):
        raise errors.fail(errors.E_DIGEST,
                          "terrain snapshot shape does not match task grid")
    return heights.astype(np.int64)
