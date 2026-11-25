from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from .hashing import array_digest, sha256_json
from .pipeline import GenerationContext


def build_cache_key(task: dict[str, Any], config: dict[str, Any],
                    descriptor: dict[str, Any], reference_checksum: str | None = None) -> str:
    payload = {
        "schema": "block-cache-key-v1",
        "task_id": task["task_id"],
        "image_seed": task["image_seed"],
        "text": task["text"],
        "prompt": task.get("prompt", ""),
        "sampler_steps": task["sampler_steps"],
        "noise_scale": task["noise_scale"],
        "precision_profile": task["precision_profile"],
        "image_shape": task["image_shape"],
        "config": config,
        "bounds": {
            key: descriptor[key]
            for key in ("row_start", "row_end", "col_start", "col_end", "block_index")
        },
        "reference_checksum": reference_checksum,
    }
    return sha256_json(payload)


class BlockCache:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def put(self, cache_key: str, product: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        record = {
            "cache_key": cache_key,
            "product_hash": product["image_digest"],
            "image_u8_hash": product["image_u8_digest"],
            "metadata": metadata,
            "product": product,
        }
        with self._lock:
            self._records[cache_key] = record
        return record

    def get(self, cache_key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._records.get(cache_key)

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {k: v for k, v in record.items() if k != "product"}
                for record in self._records.values()
            ]

    def products_by_index(self, task_id: str | None = None) -> dict[int, dict[str, Any]]:
        with self._lock:
            products = {}
            for record in self._records.values():
                metadata = record["metadata"]
                if task_id is not None and metadata.get("task_id") != task_id:
                    continue
                products[int(metadata["block_index"])] = record["product"]
            return products

    def verify_product_hashes(self) -> dict[str, Any]:
        mismatches = []
        checked = 0
        with self._lock:
            for record in self._records.values():
                checked += 1
                actual = array_digest(record["product"]["image"])
                if actual != record["product_hash"]:
                    mismatches.append({
                        "cache_key": record["cache_key"],
                        "expected_hash": record["product_hash"],
                        "actual_hash": actual,
                    })
        return {"checked": checked, "hash_mismatches": mismatches, "ok": not mismatches}


def stitch_blocks(shape: tuple[int, int, int], dtype: np.dtype,
                  products: dict[int, dict[str, Any]],
                  blocks: list[dict[str, Any]]) -> np.ndarray:
    stitched = np.zeros(shape, dtype=dtype)
    by_index = {block["block_index"]: block for block in blocks}
    for index, product in products.items():
        descriptor = by_index[index]
        r0, r1 = descriptor["row_start"], descriptor["row_end"]
        c0, c1 = descriptor["col_start"], descriptor["col_end"]
        stitched[r0:r1, c0:c1, :] = product["image"]
    return stitched


def block_at_coordinate(blocks: list[dict[str, Any]], row: int, col: int) -> dict[str, Any]:
    for block in blocks:
        if block["row_start"] <= row < block["row_end"] and block["col_start"] <= col < block["col_end"]:
            return block
    raise KeyError(f"coordinate ({row}, {col}) is outside all blocks")


def diff_to_error_details(stitched: np.ndarray, expected_global: np.ndarray,
                          blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if stitched.shape != expected_global.shape:
        return {
            "shape_mismatch": True,
            "stitched_shape": list(stitched.shape),
            "global_shape": list(expected_global.shape),
        }
    difference = stitched != expected_global
    if not np.any(difference):
        return None
    flat_index = int(np.argmax(difference))
    coordinate = np.unravel_index(flat_index, difference.shape)
    row, col = int(coordinate[0]), int(coordinate[1])
    channel = int(coordinate[2]) if len(coordinate) == 3 else 0
    descriptor = block_at_coordinate(blocks, row, col)
    actual_value = stitched[row, col, channel]
    expected_value = expected_global[row, col, channel]
    pixel_mask = difference[row, col, :] if difference.ndim == 3 else difference[row, col]
    channel_indices = np.flatnonzero(pixel_mask).tolist() if hasattr(pixel_mask, "ndim") else []
    return {
        "first_coordinate": [row, col, channel],
        "first_pixel_coordinate": [row, col],
        "first_mismatching_channels": [int(x) for x in channel_indices],
        "block_index": int(descriptor["block_index"]),
        "local_coordinate": [row - descriptor["row_start"], col - descriptor["col_start"], channel],
        "actual_value": float(actual_value),
        "expected_value": float(expected_value),
        "delta_value": float(actual_value - expected_value),
        "actual_u8": int(np.rint(np.clip(float(actual_value), 0.0, 1.0) * 255.0)),
        "expected_u8": int(np.rint(np.clip(float(expected_value), 0.0, 1.0) * 255.0)),
        "mismatching_scalar_count": int(np.count_nonzero(difference)),
        "mismatching_pixel_count": int(np.count_nonzero(np.any(difference, axis=-1))) if difference.ndim == 3 else int(np.count_nonzero(difference)),
        "block_pixel_interval": descriptor.get(
            "pixel_interval",
            {
                "half_open_text": (
                    f"rows[{descriptor['row_start']}:{descriptor['row_end']}),"
                    f"cols[{descriptor['col_start']}:{descriptor['col_end']})"
                )
            },
        )["half_open_text"],
    }


def verify_stitch(stitched: np.ndarray, global_image: np.ndarray,
                  blocks: list[dict[str, Any]]) -> dict[str, Any]:
    details = diff_to_error_details(stitched, global_image, blocks)
    if details is None:
        stitched_hash = array_digest(stitched)
        global_hash = array_digest(global_image)
        return {
            "ok": True,
            "bytewise_equal": stitched.tobytes() == global_image.tobytes(),
            "pixel_equal": bool(np.array_equal(stitched, global_image)),
            "stitched_digest": stitched_hash,
            "global_digest": global_hash,
            "mismatch": None,
        }
    return {
        "ok": False,
        "bytewise_equal": False,
        "pixel_equal": False,
        "stitched_digest": array_digest(stitched),
        "global_digest": array_digest(global_image),
        "mismatch": details,
    }
