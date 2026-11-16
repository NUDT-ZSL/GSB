from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    E_INDEX_INVALID,
    Failure,
    InputError,
    tile_id,
)
from .model import TileMeta
from .util import read_json, sha256_json, stable_pair


CANONICAL_BANDS = ("red", "green", "nir", "elevation")


@dataclass
class IndexData:
    path: Path
    base_dir: Path
    raw: dict[str, Any]
    index_hash: str
    index_version: str
    band_version: str
    band_order: tuple[str, ...]
    tile_size: tuple[int, int] | None
    tiles: list[TileMeta]
    failures: list[Failure]


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _parse_neighbors(value: Any) -> tuple[tuple[int, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("neighbors must be a list")
    pairs: list[tuple[int, int]] = []
    for item in value:
        if isinstance(item, dict):
            item = [item.get("row"), item.get("col")]
        row, col = stable_pair(item)
        if row < 0 or col < 0:
            raise ValueError("neighbor coordinates must be non-negative")
        pairs.append((row, col))
    if len(set(pairs)) != len(pairs):
        raise ValueError("neighbors contain duplicates")
    return tuple(sorted(pairs))


def _parse_band_order(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("band_order must contain four band names")
    order = tuple(str(v) for v in value)
    if set(order) != set(CANONICAL_BANDS) or len(set(order)) != 4:
        raise ValueError("band_order must be a permutation of red, green, nir, elevation")
    return order


def load_index(path: str | Path) -> IndexData:
    index_path = Path(path).resolve()
    base_dir = index_path.parent
    try:
        raw = read_json(index_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise InputError(E_INDEX_INVALID, "index", f"cannot read tile index: {exc}") from exc
    if not isinstance(raw, dict):
        raise InputError(E_INDEX_INVALID, "index", "tile index must be a JSON object")
    entries = raw.get("tiles")
    if not isinstance(entries, list):
        raise InputError(E_INDEX_INVALID, "index", "tile index must contain a tiles array")
    try:
        band_order = _parse_band_order(raw.get("band_order", list(CANONICAL_BANDS)))
    except ValueError as exc:
        raise InputError(E_INDEX_INVALID, "index", str(exc)) from exc
    band_version = str(raw.get("band_version", "v1"))
    index_version = str(raw.get("index_version", "1"))
    tile_size_raw = raw.get("tile_size")
    tile_size: tuple[int, int] | None = None
    if tile_size_raw is not None:
        try:
            if not isinstance(tile_size_raw, list) or len(tile_size_raw) != 2:
                raise ValueError
            tile_size = (_as_int(tile_size_raw[0], "tile_size[0]"), _as_int(tile_size_raw[1], "tile_size[1]"))
            if tile_size[0] <= 0 or tile_size[1] <= 0:
                raise ValueError
        except ValueError as exc:
            raise InputError(E_INDEX_INVALID, "index", "tile_size must contain two positive integers") from exc

    failures: list[Failure] = []
    parsed: list[dict[str, Any]] = []
    seen: dict[tuple[int, int], int] = {}
    duplicate_ids: set[tuple[int, int]] = set()
    for ordinal, entry in enumerate(entries):
        if not isinstance(entry, dict):
            failures.append(Failure(E_INDEX_INVALID, "index", f"tiles[{ordinal}] is not an object"))
            continue
        try:
            row = _as_int(entry.get("row"), "row")
            col = _as_int(entry.get("col"), "col")
            if row < 0 or col < 0:
                raise ValueError("row and col must be non-negative")
            bounds_raw = entry.get("bounds")
            if not isinstance(bounds_raw, list) or len(bounds_raw) != 4:
                raise ValueError("bounds must be [xmin, ymin, xmax, ymax]")
            bounds = tuple(_as_int(v, "bounds") for v in bounds_raw)
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                raise ValueError("bounds must have positive width and height")
            path_value = entry.get("path")
            if not isinstance(path_value, str) or not path_value:
                raise ValueError("path must be a non-empty string")
            neighbors = _parse_neighbors(entry.get("neighbors", []))
            band_version_tile = str(entry.get("band_version", band_version))
            order_tile = _parse_band_order(entry.get("band_order", list(band_order)))
        except (TypeError, ValueError) as exc:
            failures.append(
                Failure(
                    E_INDEX_INVALID,
                    "index",
                    f"tiles[{ordinal}]: {exc}",
                    entry.get("row") if isinstance(entry.get("row"), int) else None,
                    entry.get("col") if isinstance(entry.get("col"), int) else None,
                )
            )
            continue
        key = (row, col)
        if key in seen:
            duplicate_ids.add(key)
            failures.append(Failure(E_INDEX_INVALID, "index", "duplicate row/col", row, col))
        else:
            seen[key] = ordinal
        expected = (bounds[3] - bounds[1], bounds[2] - bounds[0])
        if tile_size is not None and expected != (tile_size[1], tile_size[0]):
            failures.append(
                Failure(
                    E_INDEX_INVALID,
                    "index",
                    "bounds size does not match tile_size",
                    row,
                    col,
                )
            )
        parsed.append(
            {
                "row": row,
                "col": col,
                "path": (base_dir / path_value).resolve(),
                "bounds": bounds,
                "neighbors": neighbors,
                "band_version": band_version_tile,
                "band_order": order_tile,
                "expected_shape": expected,
            }
        )

    unique: dict[tuple[int, int], dict[str, Any]] = {}
    for item in parsed:
        key = (item["row"], item["col"])
        if key in duplicate_ids:
            continue
        if key in unique:
            continue
        unique[key] = item

    for key, item in sorted(unique.items()):
        for nrow, ncol in item["neighbors"]:
            if (nrow, ncol) == key:
                failures.append(Failure(E_INDEX_INVALID, "index", "tile cannot be its own neighbor", key[0], key[1]))
                continue
            other = unique.get((nrow, ncol))
            if other is None:
                failures.append(
                    Failure(E_INDEX_INVALID, "index", f"unknown neighbor r{nrow}_c{ncol}", key[0], key[1])
                )
            elif key not in other["neighbors"]:
                failures.append(
                    Failure(E_INDEX_INVALID, "index", "adjacency is not symmetric", key[0], key[1])
                )
                failures.append(
                    Failure(E_INDEX_INVALID, "index", "adjacency is not symmetric", nrow, ncol)
                )

    bad_ids = {(f.row, f.col) for f in failures if f.row is not None and f.col is not None}
    bad_ids -= duplicate_ids
    metas: list[TileMeta] = []
    for key, item in sorted(unique.items()):
        if key in bad_ids:
            continue
        adjacency_payload = {
            "row": item["row"],
            "col": item["col"],
            "neighbors": [list(v) for v in item["neighbors"]],
            "band_version": item["band_version"],
        }
        metas.append(
            TileMeta(
                row=item["row"],
                col=item["col"],
                path=item["path"],
                bounds=item["bounds"],
                neighbors=item["neighbors"],
                band_version=item["band_version"],
                band_order=item["band_order"],
                expected_shape=item["expected_shape"],
                adjacency_digest=sha256_json(adjacency_payload),
            )
        )

    return IndexData(
        path=index_path,
        base_dir=base_dir,
        raw=raw,
        index_hash=sha256_json(raw),
        index_version=index_version,
        band_version=band_version,
        band_order=band_order,
        tile_size=tile_size,
        tiles=metas,
        failures=failures,
    )


def tile_sort_key(meta: TileMeta) -> tuple[int, int]:
    return meta.row, meta.col


def validate_image_header(meta: TileMeta) -> Failure | None:
    try:
        from PIL import Image

        with Image.open(meta.path) as image:
            if image.mode != "RGBA":
                return Failure(
                    "E_TILE_SHAPE_MISMATCH",
                    "validation",
                    f"expected RGBA (4 bands), got mode {image.mode}",
                    meta.row,
                    meta.col,
                )
            if image.size != (meta.width, meta.height):
                return Failure(
                    "E_TILE_SHAPE_MISMATCH",
                    "validation",
                    f"expected {meta.width}x{meta.height}, got {image.size[0]}x{image.size[1]}",
                    meta.row,
                    meta.col,
                )
    except OSError as exc:
        return Failure(
            "E_TILE_SHAPE_MISMATCH",
            "validation",
            f"cannot decode tile: {exc}",
            meta.row,
            meta.col,
        )
    return None
