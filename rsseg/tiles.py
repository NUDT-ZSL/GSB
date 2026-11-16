from __future__ import annotations

from pathlib import Path

import numpy as np

from .errors import E_INDEX_INVALID, E_TILE_SHAPE_MISMATCH, StagedError
from .model import TileData, TileMeta
from .overlay import TilePatch
from .util import sha256_bytes, sha256_file, sha256_json


def source_fingerprint(meta: TileMeta, patches: tuple[TilePatch, ...]) -> str:
    file_digest = sha256_file(meta.path)
    patch_digest = sha256_bytes(("|".join(p.digest for p in patches)).encode("ascii"))
    return sha256_json(
        {
            "schema": 1,
            "file_sha256": file_digest,
            "patches_sha256": patch_digest,
            "band_version": meta.band_version,
            "band_order": list(meta.band_order),
        }
    )


def tile_cache_key(
    meta: TileMeta,
    band_content_fingerprint: str,
    classification_fingerprint: str,
) -> str:
    return sha256_json(
        {
            "schema": 1,
            "band_content_fingerprint": band_content_fingerprint,
            "band_version": meta.band_version,
            "band_order": list(meta.band_order),
            "classification_fingerprint": classification_fingerprint,
            "tile_shape": [meta.height, meta.width],
        }
    )


def _reorder_bands(raw: np.ndarray, band_order: tuple[str, ...]) -> np.ndarray:
    canonical = ("red", "green", "nir", "elevation")
    indices = [band_order.index(name) for name in canonical]
    return raw[:, :, indices]


def decode_tile(meta: TileMeta, patches: tuple[TilePatch, ...]) -> TileData:
    try:
        from PIL import Image

        with Image.open(meta.path) as image:
            if image.mode != "RGBA":
                raise StagedError(
                    "load",
                    f"expected RGBA (4 bands), got mode {image.mode}",
                    E_TILE_SHAPE_MISMATCH,
                )
            if image.size != (meta.width, meta.height):
                raise StagedError(
                    "load",
                    f"expected {meta.width}x{meta.height}, got {image.size[0]}x{image.size[1]}",
                    E_TILE_SHAPE_MISMATCH,
                )
            raw = np.asarray(image, dtype=np.uint8).copy()
    except StagedError:
        raise
    except OSError as exc:
        raise StagedError("load", f"cannot decode tile: {exc}", E_TILE_SHAPE_MISMATCH) from exc

    for patch in patches:
        x0, y0, x1, y1 = patch.bbox
        for band_index, band_name in enumerate(meta.band_order):
            channel = meta.band_order.index(band_name)
            raw[y0:y1, x0:x1, channel] = np.clip(
                np.rint(patch.bands[band_index]), 0, 255
            ).astype(np.uint8)

    canonical = _reorder_bands(raw, meta.band_order)
    array_digest = sha256_bytes(
        bytes(str(canonical.shape), "ascii") + b"|" + canonical.astype(np.uint8).tobytes()
    )
    return TileData(
        meta=meta,
        bands=canonical.astype(np.float64) / 255.0,
        array_digest=array_digest,
        overlay_applied=bool(patches),
    )
