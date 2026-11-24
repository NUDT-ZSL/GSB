"""Atomic tile persistence, version recovery and portable PGM images."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil

import numpy as np

from .errors import DungeonError
from .models import (
    Level,
    RenderTask,
    TileRect,
    canonical_level_json,
    canonical_task_json,
)
from .tiles import tile_index

VERSION_FILE = "version.json"
INTERRUPT_FILE = "interrupted.json"
ARCHIVE_DIR = "archived_versions"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def version_identifier(level: Level, task: RenderTask) -> str:
    """Digest of normalized level + task; paths do not affect pixels."""
    payload = {
        "level": canonical_level_json(level),
        "task": canonical_task_json(task),
    }
    return _canonical_digest(payload)


def tile_digest(gray: np.ndarray, outline: np.ndarray, version: str) -> str:
    hasher = hashlib.sha256()
    hasher.update(version.encode("ascii"))
    hasher.update(str(gray.shape).encode("ascii"))
    hasher.update(gray.tobytes())
    hasher.update(outline.tobytes())
    return hasher.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: dict) -> None:
    data = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    atomic_write_bytes(path, data)


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def tile_paths(state_dir: Path, index: int) -> tuple[Path, Path, Path]:
    stem = state_dir / f"tile_{index:04d}"
    return (
        stem.with_name(stem.name + "_gray.npy"),
        stem.with_name(stem.name + "_outline.npy"),
        stem.with_name(stem.name + ".json"),
    )


def prepare_state_dir(state_dir: Path, version: str) -> list[str]:
    """Create/refresh the state directory, archiving foreign versions first."""
    state_dir.mkdir(parents=True, exist_ok=True)
    for tmp in state_dir.glob("*.tmp"):
        tmp.unlink()
    archived: list[str] = []
    version_path = state_dir / VERSION_FILE
    current = read_json(version_path)
    if current is not None and current.get("version") != version:
        archive_root = state_dir / ARCHIVE_DIR
        archive_root.mkdir(exist_ok=True)
        old_version = str(current.get("version", "unknown"))
        destination = archive_root / old_version
        counter = 1
        while destination.exists():
            destination = archive_root / f"{old_version}_{counter}"
            counter += 1
        destination.mkdir()
        for entry in state_dir.iterdir():
            if entry.name in {ARCHIVE_DIR, VERSION_FILE}:
                continue
            shutil.move(str(entry), str(destination / entry.name))
        shutil.move(str(version_path), str(destination / VERSION_FILE))
        archived.append(old_version)
    if current is None or current.get("version") != version:
        atomic_write_json(
            version_path,
            {"version": version, "format": "dungeon-render-v1"},
        )
    return archived


def save_tile(
    state_dir: Path,
    task: RenderTask,
    tile: TileRect,
    gray: np.ndarray,
    outline: np.ndarray,
    version: str,
) -> dict:
    index = tile_index(task, tile)
    gray_path, outline_path, meta_path = tile_paths(state_dir, index)
    digest = tile_digest(gray, outline, version)
    atomic_write_bytes(gray_path, _npy_bytes(gray))
    atomic_write_bytes(outline_path, _npy_bytes(outline))
    metadata = {
        "index": index,
        "col": tile.col,
        "row": tile.row,
        "offset": [tile.x, tile.y],
        "coverage": [tile.x, tile.y, tile.x + tile.width, tile.y + tile.height],
        "shape": [tile.height, tile.width],
        "declared_pixels": tile.pixel_count,
        "digest": digest,
        "version": version,
    }
    atomic_write_json(meta_path, metadata)
    return metadata


def _npy_bytes(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def load_tile(
    state_dir: Path, task: RenderTask, tile: TileRect, version: str
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return stored tile arrays, or raise :class:`DungeonError` on mismatch."""
    index = tile_index(task, tile)
    gray_path, outline_path, meta_path = tile_paths(state_dir, index)
    if not (gray_path.exists() and outline_path.exists() and meta_path.exists()):
        return None
    try:
        metadata = read_json(meta_path)
        gray = np.load(gray_path, allow_pickle=False)
        outline = np.load(outline_path, allow_pickle=False)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise DungeonError(
            "E_DIGEST",
            f"tile {index} state is unreadable",
            {"index": index, "reason": "unreadable"},
        ) from exc

    reason = _invalid_reason(
        metadata, gray, outline, tile, version, index
    )
    if reason:
        raise DungeonError(
            "E_DIGEST",
            f"tile {index} failed digest validation: {reason}",
            {"index": index, "reason": reason},
        )
    return gray, outline


def _invalid_reason(metadata, gray, outline, tile, version, index) -> str | None:
    if not isinstance(metadata, dict):
        return "metadata"
    if metadata.get("version") != version:
        return "version"
    if metadata.get("index") != index:
        return "index"
    if metadata.get("declared_pixels") != tile.pixel_count:
        return "declared_pixels"
    if list(gray.shape) != [tile.height, tile.width]:
        return "gray_shape"
    if list(outline.shape) != [tile.height, tile.width]:
        return "outline_shape"
    if gray.dtype != np.uint8 or outline.dtype != np.uint8:
        return "dtype"
    if metadata.get("digest") != tile_digest(gray, outline, version):
        return "digest"
    if metadata.get("offset") != [tile.x, tile.y]:
        return "offset"
    if metadata.get("coverage") != [
        tile.x,
        tile.y,
        tile.x + tile.width,
        tile.y + tile.height,
    ]:
        return "coverage"
    return None


def invalidate_tile(state_dir: Path, task: RenderTask, tile: TileRect) -> None:
    index = tile_index(task, tile)
    for path in tile_paths(state_dir, index):
        if path.exists():
            path.unlink()


def write_pgm(path: Path, image: np.ndarray, version: str) -> None:
    if image.dtype != np.uint8 or image.ndim != 2:
        raise ValueError("PGM writer requires a uint8 2D array")
    height, width = image.shape
    header = f"P5\n{width} {height}\n255\n# {version}\n".encode("ascii")
    atomic_write_bytes(path, header + image.tobytes())


def read_pgm_header(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("rb") as handle:
        magic = handle.readline().strip()
        if magic != b"P5":
            return None
        width_height = handle.readline().split()
        max_value = handle.readline().strip()
        comment = handle.readline().strip()
    width, height = (int(value) for value in width_height)
    version = comment[2:].decode("ascii") if comment.startswith(b"# ") else ""
    return {
        "width": width,
        "height": height,
        "max_value": int(max_value),
        "version": version,
    }


def read_pgm(path: Path) -> np.ndarray:
    header = read_pgm_header(path)
    if header is None:
        raise DungeonError("E_DIGEST", f"not a PGM image: {path}")
    with path.open("rb") as handle:
        for _ in range(4):
            handle.readline()
        raw = handle.read()
    image = np.frombuffer(raw, dtype=np.uint8)
    return image.reshape(header["height"], header["width"]).copy()


def write_interrupt_marker(
    state_dir: Path, version: str, completed: list[int], rounds: list[list[int]]
) -> Path:
    path = state_dir / INTERRUPT_FILE
    atomic_write_json(
        path,
        {
            "version": version,
            "completed_tiles": completed,
            "rounds_executed": rounds,
            "exit_code": 130,
        },
    )
    return path
