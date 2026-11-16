from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import E_PATCH_OUT_OF_RANGE, Failure, InputError
from .model import TileMeta
from .util import read_json, sha256_bytes


@dataclass(frozen=True)
class TilePatch:
    patch_id: str
    row: int
    col: int
    bbox: tuple[int, int, int, int]
    bands: np.ndarray  # (4, h, w), values in index band_order
    digest: str


@dataclass
class OverlayData:
    patches_by_tile: dict[tuple[int, int], tuple[TilePatch, ...]]
    failures: list[Failure]
    content_fingerprints: dict[tuple[int, int], str]


def _ints(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(v, bool) or not isinstance(v, int) for v in value):
        return None
    return tuple(int(v) for v in value)  # type: ignore[return-value]


def _patch_failure(patch_id: str, row: int | None, col: int | None, message: str) -> Failure:
    prefix = f"patch {patch_id}: " if patch_id else ""
    return Failure(E_PATCH_OUT_OF_RANGE, "overlay", prefix + message, row, col)


def load_overlay(path: str | Path | None, metas: list[TileMeta]) -> OverlayData:
    if path is None:
        return OverlayData({}, [], {})
    try:
        raw = read_json(path)
    except FileNotFoundError:
        return OverlayData({}, [], {})
    except (OSError, ValueError) as exc:
        raise InputError(E_PATCH_OUT_OF_RANGE, "overlay", f"cannot read overlay: {exc}") from exc

    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("patches", [])
    else:
        raise InputError(E_PATCH_OUT_OF_RANGE, "overlay", "overlay must be an object or array")
    if not isinstance(entries, list):
        raise InputError(E_PATCH_OUT_OF_RANGE, "overlay", "overlay.patches must be an array")

    meta_map = {meta.stable_key: meta for meta in metas}
    failures: list[Failure] = []
    grouped: dict[tuple[int, int], list[TilePatch]] = {}

    for ordinal, entry in enumerate(entries):
        if not isinstance(entry, dict):
            failures.append(_patch_failure(f"#{ordinal}", None, None, "patch must be an object"))
            continue
        patch_id = str(entry.get("id", f"patch-{ordinal}"))
        tile_value = entry.get("tile", [entry.get("row"), entry.get("col")])
        bbox = _ints(entry.get("bbox", [entry.get("x"), entry.get("y"), None, None]))
        row: int | None = None
        col: int | None = None
        if isinstance(tile_value, (list, tuple)) and len(tile_value) == 2:
            if all(isinstance(v, int) and not isinstance(v, bool) for v in tile_value):
                row, col = int(tile_value[0]), int(tile_value[1])
        if row is None or col is None:
            failures.append(_patch_failure(patch_id, row, col, "tile must be [row, col]"))
            continue
        meta = meta_map.get((row, col))
        if meta is None:
            failures.append(_patch_failure(patch_id, row, col, "target tile does not exist"))
            continue
        if bbox is None:
            # Accept x/y/width/height as an alternate spelling.
            try:
                x = int(entry["x"])
                y = int(entry["y"])
                width = int(entry["width"])
                height = int(entry["height"])
                bbox = (x, y, x + width, y + height)
            except (KeyError, TypeError, ValueError):
                failures.append(_patch_failure(patch_id, row, col, "bbox must be [x0, y0, x1, y1]"))
                continue
        x0, y0, x1, y1 = bbox
        if x0 < 0 or y0 < 0 or x1 > meta.width or y1 > meta.height or x1 <= x0 or y1 <= y0:
            failures.append(
                _patch_failure(
                    patch_id,
                    row,
                    col,
                    f"bbox {list(bbox)} is outside tile {meta.width}x{meta.height}",
                )
            )
            continue
        h, w = y1 - y0, x1 - x0

        bands_value = entry.get("bands")
        values_value = entry.get("values")
        try:
            if bands_value is not None:
                bands = np.asarray(bands_value)
                if bands.ndim != 3 or bands.shape != (4, h, w):
                    raise ValueError(f"bands must have shape [4, {h}, {w}]")
            elif values_value is not None:
                values = np.asarray(values_value)
                if values.ndim != 3 or values.shape != (h, w, 4):
                    raise ValueError(f"values must have shape [{h}, {w}, 4]")
                bands = np.transpose(values, (2, 0, 1))
            else:
                raise ValueError("bands or values is required")
            if not np.issubdtype(bands.dtype, np.number):
                raise ValueError("patch bands must be numeric")
            bands = np.ascontiguousarray(bands.astype(np.float64, copy=False))
            if not np.all(np.isfinite(bands)):
                raise ValueError("patch bands must be finite")
        except (TypeError, ValueError) as exc:
            failures.append(_patch_failure(patch_id, row, col, str(exc)))
            continue

        digest_payload = (
            patch_id.encode("utf-8")
            + b"|"
            + bytes(str((x0, y0, x1, y1)), "utf-8")
            + b"|"
            + np.asarray(bands, dtype="<f8").tobytes()
        )
        patch = TilePatch(
            patch_id=patch_id,
            row=row,
            col=col,
            bbox=(x0, y0, x1, y1),
            bands=bands,
            digest=sha256_bytes(digest_payload),
        )
        grouped.setdefault((row, col), []).append(patch)

    valid: dict[tuple[int, int], tuple[TilePatch, ...]] = {}
    fingerprints: dict[tuple[int, int], str] = {}
    for key in sorted(grouped):
        patches = tuple(sorted(grouped[key], key=lambda p: (p.patch_id, p.bbox)))
        valid[key] = patches
        fingerprints[key] = sha256_bytes(
            ("|".join(p.digest for p in patches)).encode("ascii")
        )
    return OverlayData(valid, failures, fingerprints)
