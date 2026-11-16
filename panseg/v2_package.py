"""V2 JSON mask package reader and writer.

The v2 package stores one base64-encoded byte string per instance containing
an explicit per-pixel 0/1 array, plus confidence, crowd flags and a named RGB
palette.  It is self describing and never uses run-length compression.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import E_MASK_LENGTH_MISMATCH, E_PALETTE_MISSING, PipelineError
from .rle import bitmap_to_runs


@dataclass(frozen=True)
class V2RawInstance:
    instance_id: str
    semantic_class: str
    runs: tuple[tuple[int, int], ...]
    pixel_count: int
    confidence: float
    is_crowd: bool
    palette_name: str
    palette_rgb: tuple[int, int, int]


def encode_mask_b64(bitmap: np.ndarray) -> str:
    flat = np.asarray(bitmap, dtype=np.uint8).reshape(-1)
    return base64.b64encode(flat.tobytes()).decode("ascii")


def write_v2_package(
    path: str | Path,
    width: int,
    height: int,
    instances: list[V2RawInstance],
    palette: dict[str, dict[str, Any]],
) -> None:
    total = width * height
    payload_instances = []
    for instance in instances:
        flat = np.zeros(total, dtype=np.uint8)
        for start, length in instance.runs:
            flat[start : start + length] = 1
        payload_instances.append(
            {
                "instance_id": instance.instance_id,
                "semantic_class": instance.semantic_class,
                "palette_name": instance.palette_name,
                "confidence": instance.confidence,
                "is_crowd": instance.is_crowd,
                "mask_b64": base64.b64encode(flat.tobytes()).decode("ascii"),
            }
        )
    document = {
        "format_version": 2,
        "width": width,
        "height": height,
        "palette": palette,
        "instances": payload_instances,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def parse_v2_package(path: str | Path) -> tuple[int, int, list[V2RawInstance], dict[str, dict[str, Any]]]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format_version") != 2:
        raise PipelineError(E_MASK_LENGTH_MISMATCH, "not a v2 mask package")
    width = int(document["width"])
    height = int(document["height"])
    total_pixels = width * height
    palette = document.get("palette") or {}
    raw_instances = document.get("instances") or []
    instances: list[V2RawInstance] = []
    for entry in raw_instances:
        instance_id = entry["instance_id"]
        packed = base64.b64decode(entry["mask_b64"])
        if len(packed) != total_pixels:
            raise PipelineError(
                E_MASK_LENGTH_MISMATCH,
                f"mask length {len(packed)} != pixel total {total_pixels}",
                instance_id=instance_id,
                details={"mask_length": len(packed), "total_pixels": total_pixels},
            )
        palette_name = entry["palette_name"]
        if palette_name not in palette:
            raise PipelineError(
                E_PALETTE_MISSING,
                f"palette entry {palette_name!r} missing for instance {instance_id}",
                instance_id=instance_id,
                details={"palette_name": palette_name},
            )
        rgb = tuple(int(channel) for channel in palette[palette_name]["rgb"])
        bitmap = np.frombuffer(packed, dtype=np.uint8).reshape(height, width)
        if bool(np.any((bitmap != 0) & (bitmap != 1))):
            raise PipelineError(
                E_MASK_LENGTH_MISMATCH,
                "v2 mask must contain only 0/1 pixel values",
                instance_id=instance_id,
            )
        runs, pixel_count = bitmap_to_runs(bitmap)
        instances.append(
            V2RawInstance(
                instance_id=instance_id,
                semantic_class=entry["semantic_class"],
                runs=runs,
                pixel_count=pixel_count,
                confidence=float(entry["confidence"]),
                is_crowd=bool(entry["is_crowd"]),
                palette_name=palette_name,
                palette_rgb=rgb,
            )
        )
    return width, height, instances, palette
