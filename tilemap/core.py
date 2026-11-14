"""Deterministic tile-pyramid generation for large map images.

Pyramid conventions used by this project:

* Level 0 is the source image at native resolution.
* Every higher level halves both source dimensions (rounded up).
* The maximum level is the first level whose whole map fits into a single
  tile, therefore the top level always contains exactly one tile.
* Tiles are named ``z_x_y.png`` where ``x`` is the column and ``y`` is the
  row (row-major order, i.e. sorted by level, then row, then column).
* Areas outside the source image (and outside a downscaled level image)
  are filled with fully transparent pixels.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

from PIL import Image, UnidentifiedImageError

PathLike = Union[str, os.PathLike[str]]

__all__ = [
    "MANIFEST_FILENAME",
    "SUPPORTED_FORMATS",
    "TILE_FILENAME_PATTERN",
    "ConfigError",
    "LevelGeometry",
    "ManifestError",
    "SourceError",
    "TileWriteError",
    "TilemapError",
    "ceil_div",
    "compute_max_level",
    "generate_tiles",
    "level_dimensions",
    "load_source",
]

# Formats accepted as a tile source. Anything else is rejected before any
# output is written; this makes "unsupported format" deterministic.
SUPPORTED_FORMATS = frozenset({"PNG", "JPEG", "BMP", "GIF", "WEBP", "TIFF"})

TILE_FILENAME_PATTERN = "z_x_y.png"
MANIFEST_FILENAME = "manifest.json"

TILE_OK = "ok"
TILE_EMPTY = "empty"
TILE_FAILED = "failed"


class TilemapError(Exception):
    """Base class for expected, user-facing errors."""


class SourceError(TilemapError):
    """The source image is missing, unreadable, corrupt or unsupported."""


class ConfigError(TilemapError):
    """Invalid command-line values or an unusable output location."""


class ManifestError(TilemapError):
    """The manifest could not be written."""


class TileWriteError(TilemapError):
    """A single tile file could not be written."""


def ceil_div(value: int, divisor: int) -> int:
    """Ceiling division for non-negative integers."""
    return -(-value // divisor)


@dataclass(frozen=True)
class LevelGeometry:
    """Geometry of one pyramid level."""

    level: int
    scale: int
    width: int
    height: int
    cols: int
    rows: int

    @property
    def total_tiles(self) -> int:
        return self.cols * self.rows


def compute_max_level(width: int, height: int, tile_size: int) -> int:
    """Return the first level whose entire image fits in one tile.

    ``level 0`` is native resolution, so for example a 500x300 source with
    256 px tiles yields level 0 = 2x2 tiles and level 1 = 1x1 tile.
    """
    if width <= 0 or height <= 0:
        raise ValueError("image width and height must be positive")
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")

    level = 0
    while (
        ceil_div(width, 1 << level) > tile_size
        or ceil_div(height, 1 << level) > tile_size
    ):
        level += 1
        if level >= 63:  # pragma: no cover - would require an absurd image
            raise ValueError("cannot derive a reasonable maximum level")
    return level


def level_dimensions(
    width: int, height: int, tile_size: int, level: int
) -> LevelGeometry:
    """Return the scaled dimensions and tile grid for ``level``."""
    if level < 0:
        raise ValueError("level must be >= 0")
    scale = 1 << level
    level_width = ceil_div(width, scale)
    level_height = ceil_div(height, scale)
    return LevelGeometry(
        level=level,
        scale=scale,
        width=level_width,
        height=level_height,
        cols=ceil_div(level_width, tile_size),
        rows=ceil_div(level_height, tile_size),
    )


def load_source(
    path: PathLike, *, max_pixels: int = -1
) -> tuple[Image.Image, dict[str, Any]]:
    """Validate a source image and return it as RGBA plus metadata.

    Validation happens before any output directory is created, so a broken
    source never leaves tiles or a manifest behind.

    ``max_pixels`` follows Pillow's decompression-bomb guard: ``-1`` keeps
    the Pillow default, ``0`` disables the guard for trusted huge maps, and
    a positive value sets an explicit limit.
    """
    source_path = Path(path)

    if not source_path.exists():
        raise SourceError(f"source image does not exist: {source_path}")
    if not source_path.is_file():
        raise SourceError(f"source path is not a file: {source_path}")

    previous_max_pixels = Image.MAX_IMAGE_PIXELS
    if max_pixels == 0:
        Image.MAX_IMAGE_PIXELS = None
    elif max_pixels > 0:
        Image.MAX_IMAGE_PIXELS = max_pixels

    try:
        # First pass: format detection plus Pillow's integrity check. This
        # catches truncated/corrupt PNG data before decoding pixels.
        with Image.open(source_path) as probe:
            image_format = probe.format
            source_width, source_height = probe.size
            source_mode = probe.mode
            probe.verify()

        if image_format not in SUPPORTED_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_FORMATS))
            raise SourceError(
                f"unsupported source image format: {image_format!r} "
                f"(supported: {supported})"
            )

        # Second pass: actually decode, then normalise to RGBA so padding,
        # empty-tile detection and PNG output behave identically for every
        # supported input format.
        with Image.open(source_path) as image:
            image.load()
            rgba = image.convert("RGBA")
    except SourceError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise SourceError(
            f"cannot read source image: {source_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    except MemoryError as exc:
        size_hint = ""
        if "source_width" in locals() and "source_height" in locals():
            size_hint = (
                f": {source_width}x{source_height} "
                f"(~{source_width * source_height * 4 / 2**20:.0f} MiB as RGBA)"
            )
        raise SourceError(
            f"source image does not fit in memory: {source_path}{size_hint}; "
            f"raise --max-pixels or use a smaller source"
        ) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max_pixels

    metadata = {
        "path": source_path.as_posix(),
        "format": image_format,
        "width": source_width,
        "height": source_height,
        "mode": source_mode,
        "file_size_bytes": source_path.stat().st_size,
    }
    return rgba, metadata


def _format_error(exc: BaseException) -> str:
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _try_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _save_png_atomic(tile: Image.Image, out_path: Path) -> None:
    """Write a PNG via a temporary file so a failed write cannot corrupt or
    delete a previously generated tile.

    Errors are re-raised as :class:`TileWriteError` naming the final tile
    path, so the recorded message is stable across runs (the random temp
    filename never leaks into the manifest).
    """
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{out_path.stem}.", suffix=".tmp", dir=str(out_path.parent)
        )
        os.close(fd)
    except OSError as exc:
        raise TileWriteError(
            f"cannot create temporary file in {out_path.parent}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        try:
            tile.save(temp_name, format="PNG")
            os.replace(temp_name, out_path)
            temp_name = ""
        except OSError as exc:
            raise TileWriteError(
                f"cannot write tile {out_path}: "
                f"{type(exc).__name__}: {exc.strerror or exc}"
            ) from exc
    finally:
        if temp_name:
            _try_unlink(Path(temp_name))


def _render_tile(
    level_image: Image.Image,
    level: int,
    col: int,
    row: int,
    tile_size: int,
    scale: int,
    source_width: int,
    source_height: int,
    out_path: Path,
) -> dict[str, Any]:
    """Crop one tile, pad with transparency, save it and describe it.

    The crop is taken strictly from this tile's own area of the level image,
    so neighbouring pixels can never leak into a tile.
    """
    left = col * tile_size
    top = row * tile_size
    right = min(left + tile_size, level_image.width)
    bottom = min(top + tile_size, level_image.height)

    tile = Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))
    if right > left and bottom > top:
        tile.paste(level_image.crop((left, top, right, bottom)), (0, 0))

    is_empty = tile.getchannel("A").getbbox() is None
    status = TILE_EMPTY if is_empty else TILE_OK
    error: Optional[str] = None

    try:
        _save_png_atomic(tile, out_path)
    except Exception as exc:  # noqa: BLE001 - per-tile isolation is required
        status = TILE_FAILED
        error = (
            str(exc)
            if isinstance(exc, TileWriteError)
            else _format_error(exc)
        )
    finally:
        tile.close()

    region_width = tile_size * scale
    region_height = tile_size * scale
    source_x = col * region_width
    source_y = row * region_height
    covered_width = max(
        0, min(source_x + region_width, source_width) - source_x
    )
    covered_height = max(
        0, min(source_y + region_height, source_height) - source_y
    )

    return {
        "level": level,
        "x": col,
        "y": row,
        "path": out_path.name,
        "status": status,
        "empty": status == TILE_EMPTY,
        "error": error,
        "source_region": {
            "x": source_x,
            "y": source_y,
            "width": region_width,
            "height": region_height,
        },
        "source_covered": {
            "x": source_x,
            "y": source_y,
            "width": covered_width,
            "height": covered_height,
        },
    }


def _write_manifest(manifest: dict[str, Any], path: Path) -> None:
    """Atomically write the manifest as deterministic UTF-8 JSON."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    manifest,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                handle.write("\n")
            os.replace(temp_name, path)
            temp_name = ""
        finally:
            if temp_name:
                _try_unlink(Path(temp_name))
    except OSError as exc:
        raise ManifestError(
            f"cannot write manifest: {path}: {type(exc).__name__}: {exc}"
        ) from exc


def generate_tiles(
    source: PathLike,
    out_dir: PathLike,
    *,
    tile_size: int = 256,
    start_level: int = 0,
    manifest_path: Optional[PathLike] = None,
    max_pixels: int = -1,
    on_level: Optional[Callable[[dict[str, Any]], None]] = None,
    on_tile_error: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Build the tile pyramid and its manifest.

    ``start_level`` skips lower levels entirely: they get no tile files and
    are recorded with ``"skipped": true`` in the manifest.

    Tile write errors are isolated per tile; the remaining tiles are still
    generated and the manifest records the failure plus its error message.
    """
    if tile_size <= 0:
        raise ConfigError(f"tile_size must be a positive integer: {tile_size}")

    # Source validation deliberately happens before mkdir/out-of-scope writes.
    source_image, source_meta = load_source(source, max_pixels=max_pixels)

    try:
        source_width = source_meta["width"]
        source_height = source_meta["height"]
        max_level = compute_max_level(source_width, source_height, tile_size)

        if not 0 <= start_level <= max_level:
            raise ConfigError(
                f"start_level must be between 0 and {max_level}: {start_level}"
            )

        output_dir = Path(out_dir)
        if output_dir.exists() and not output_dir.is_dir():
            raise ConfigError(
                f"output path is not a directory: {output_dir}"
            )
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"cannot create output directory: {output_dir}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        levels: list[dict[str, Any]] = []

        for level in range(max_level + 1):
            geometry = level_dimensions(
                source_width, source_height, tile_size, level
            )
            level_record: dict[str, Any] = {
                "level": level,
                "scale": geometry.scale,
                "cols": geometry.cols,
                "rows": geometry.rows,
                "level_width": geometry.width,
                "level_height": geometry.height,
                "source_region": {
                    "x": 0,
                    "y": 0,
                    "width": source_width,
                    "height": source_height,
                },
                "skipped": level < start_level,
                "total": 0,
                "success": 0,
                "ok": 0,
                "empty": 0,
                "failed": 0,
                "tiles": [],
            }

            if level_record["skipped"]:
                levels.append(level_record)
                if on_level is not None:
                    on_level(level_record)
                continue

            level_image = source_image
            if level > 0:
                level_image = source_image.resize(
                    (geometry.width, geometry.height),
                    Image.Resampling.LANCZOS,
                )

            tiles: list[dict[str, Any]] = []
            try:
                # Row-major order: level, row (y), column (x) ascending.
                for row in range(geometry.rows):
                    for col in range(geometry.cols):
                        out_path = output_dir / f"{level}_{col}_{row}.png"
                        record = _render_tile(
                            level_image=level_image,
                            level=level,
                            col=col,
                            row=row,
                            tile_size=tile_size,
                            scale=geometry.scale,
                            source_width=source_width,
                            source_height=source_height,
                            out_path=out_path,
                        )
                        tiles.append(record)
                        if record["status"] == TILE_FAILED and on_tile_error:
                            on_tile_error(record)
            finally:
                if level_image is not source_image:
                    level_image.close()

            ok = sum(1 for tile in tiles if tile["status"] == TILE_OK)
            empty = sum(1 for tile in tiles if tile["status"] == TILE_EMPTY)
            failed = sum(1 for tile in tiles if tile["status"] == TILE_FAILED)

            level_record.update(
                {
                    "total": len(tiles),
                    "success": ok + empty,
                    "ok": ok,
                    "empty": empty,
                    "failed": failed,
                    "tiles": tiles,
                }
            )
            levels.append(level_record)
            if on_level is not None:
                on_level(level_record)

        totals = {
            key: sum(level[key] for level in levels)
            for key in ("total", "success", "ok", "empty", "failed")
        }
        manifest: dict[str, Any] = {
            "schema": "tilemap-manifest/1",
            "generator": "tilemap 1.0.0",
            "source": source_meta,
            "tile_size": tile_size,
            "tile_pattern": TILE_FILENAME_PATTERN,
            "max_level": max_level,
            "start_level": start_level,
            "summary": totals,
            "levels": levels,
        }

        destination = (
            Path(manifest_path)
            if manifest_path is not None
            else output_dir / MANIFEST_FILENAME
        )
        _write_manifest(manifest, destination)
        return manifest
    finally:
        source_image.close()
