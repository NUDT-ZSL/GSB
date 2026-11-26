"""Pixel-level adjudication between block and one-shot whole generation."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .errors import E_BLOCK_MISMATCH, KFError
from .hashing import array_digest


def compare_keyframe_collections(
    block_frames: Sequence[Dict[str, Any]],
    whole_frames: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare aligned keyframe collections ordinal by ordinal.

    Returns a verdict with the first deviation frame index and pixel coordinate
    when the collections are not byte-identical.
    """
    if len(block_frames) != len(whole_frames):
        return {
            "exact_match": False,
            "reason": "keyframe_count_differs",
            "block_count": len(block_frames),
            "whole_count": len(whole_frames),
            "first_mismatch": {
                "ordinal": min(len(block_frames), len(whole_frames)),
                "frame_index": None,
            },
        }

    per_keyframe: List[Dict[str, Any]] = []
    first_mismatch: Optional[Dict[str, Any]] = None
    max_abs_diff = 0

    for block_item, whole_item in zip(block_frames, whole_frames):
        ordinal = int(whole_item["ordinal"])
        frame_index = int(whole_item["frame_index"])
        block_pixels = np.asarray(block_item["pixels"], dtype=np.uint8)
        whole_pixels = np.asarray(whole_item["pixels"], dtype=np.uint8)

        same_shape = block_pixels.shape == whole_pixels.shape
        equal = same_shape and np.array_equal(block_pixels, whole_pixels)
        block_hash = array_digest(block_pixels)
        whole_hash = array_digest(whole_pixels)
        per_keyframe.append({
            "ordinal": ordinal,
            "frame_index": frame_index,
            "equal": bool(equal),
            "block_hash": block_hash,
            "whole_hash": whole_hash,
        })

        if not equal and first_mismatch is None:
            location: Dict[str, Any] = {
                "ordinal": ordinal,
                "frame_index": frame_index,
                "block_hash": block_hash,
                "whole_hash": whole_hash,
            }
            if same_shape:
                diff = np.not_equal(block_pixels, whole_pixels)
                flat_index = int(np.flatnonzero(diff.ravel())[0])
                coordinate = np.unravel_index(flat_index, diff.shape)
                abs_diff = np.abs(block_pixels.astype(np.int16) - whole_pixels.astype(np.int16))
                max_abs_diff = int(abs_diff.max())
                location.update({
                    "pixel_flat_index": flat_index,
                    "pixel_coordinate_hwc": [int(v) for v in coordinate],
                    "block_value": int(block_pixels[coordinate]),
                    "whole_value": int(whole_pixels[coordinate]),
                    "different_pixel_count": int(diff.sum()),
                    "max_abs_diff": max_abs_diff,
                })
            else:
                location["reason"] = "shape_mismatch"
            first_mismatch = location

    return {
        "exact_match": first_mismatch is None,
        "compared_keyframes": len(whole_frames),
        "identical_keyframes": sum(1 for item in per_keyframe if item["equal"]),
        "first_mismatch": first_mismatch,
        "max_abs_diff": max_abs_diff,
        "per_keyframe": per_keyframe,
    }


def compare_boundary_records(block_records: Sequence[Dict[str, Any]],
                             whole_records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if [r.get("left_ordinal") for r in block_records] != [r.get("left_ordinal") for r in whole_records]:
        return {"exact_match": False, "reason": "boundary_order_differs"}
    mismatches: List[Dict[str, Any]] = []
    for block_record, whole_record in zip(block_records, whole_records):
        for key in ("left_noise_hash", "right_noise_hash", "noise_stats_hash",
                    "schedule_hash", "left_context_hash", "right_context_hash"):
            if block_record.get(key) != whole_record.get(key):
                mismatches.append({
                    "left_ordinal": block_record.get("left_ordinal"),
                    "right_ordinal": block_record.get("right_ordinal"),
                    "field": key,
                    "block_value": block_record.get(key),
                    "whole_value": whole_record.get(key),
                })
    return {"exact_match": not mismatches, "mismatches": mismatches[:8],
            "compared_boundaries": len(whole_records)}


def raise_for_verdict(verdict: Dict[str, Any], details: Optional[Dict[str, Any]] = None) -> None:
    if verdict.get("exact_match"):
        return
    merged = {"pixel_verdict": verdict, **(details or {})}
    raise KFError(
        E_BLOCK_MISMATCH,
        "chunked block generation is not pixel-identical to one-shot generation",
        merged,
    )
