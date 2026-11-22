from __future__ import annotations

import json
import zlib
from typing import Any, Iterable

import numpy as np

from .config import WorkbenchConfig


SHAPE_SEQUENCE = (12, 10, 8, 6, 5, 4, 3)
SCALE_HINTS = {
    "int8": 0.0140,
    "uint8": 0.0140,
    "int4": 0.1600,
    "uint4": 0.3000,
}
ZERO_POINTS = {
    "int8": 0,
    "uint8": 128,
    "int4": 0,
    "uint4": 8,
}


def model_seed_part(model_tag: str) -> int:
    return zlib.crc32(model_tag.encode("utf-8")) & 0xFFFFFFFF


def layer_shapes(layer_count: int) -> list[tuple[int, int]]:
    shapes: list[tuple[int, int]] = []
    input_dim = 8
    for index in range(layer_count):
        output_dim = SHAPE_SEQUENCE[index] if index < len(SHAPE_SEQUENCE) else 3
        shapes.append((output_dim, input_dim))
        input_dim = output_dim
    return shapes


def _generator(seed: int, *parts: int) -> np.random.Generator:
    sequence = np.random.SeedSequence([int(seed), *[int(part) for part in parts]])
    return np.random.Generator(np.random.PCG64(sequence))


def generate_manifest(config: WorkbenchConfig) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    base = model_seed_part(config.model_tag)
    shapes = layer_shapes(config.layer_count)
    for index, shape in enumerate(shapes):
        dtype = config.quant_dtype_cycle[index % len(config.quant_dtype_cycle)]
        entry: dict[str, Any] = {
            "model_tag": config.model_tag,
            "layer_id": f"layer_{index:02d}",
            "layer_index": index,
            "layer_shape": [int(shape[0]), int(shape[1])],
            "weight_revision": int(config.revision_overrides.get(index, 1)),
            "quant_dtype": dtype,
            "scale_hint": float(SCALE_HINTS.get(dtype, 0.01)),
            "zero_point": int(ZERO_POINTS.get(dtype, 0)),
            "calib_seed": int(config.seed + 10_000 + index),
            "block_policy": {
                "name": "row_col",
                "row_block": int(config.default_block_size),
                "col_block": int(config.default_block_size),
                "group_size": int(max(1, config.default_block_size * 2)),
            },
        }
        if index in config.calib_seed_overrides:
            entry["calib_seed"] = int(config.calib_seed_overrides[index])
        if index in config.manifest_overrides:
            entry.update(config.manifest_overrides[index])
        entries.append(entry)
    # Keep the generated manifest independent of mutable generator state.
    return json.loads(json.dumps(entries, sort_keys=True, allow_nan=False))


def weight_generator(config: WorkbenchConfig, entry: dict[str, Any]) -> np.random.Generator:
    return _generator(
        config.seed,
        model_seed_part(str(entry["model_tag"])),
        int(entry["layer_index"]),
        int(entry["weight_revision"]),
        0x57514947,
    )


def generate_weights(config: WorkbenchConfig, entry: dict[str, Any]) -> np.ndarray:
    shape = tuple(int(x) for x in entry["layer_shape"])
    rng = weight_generator(config, entry)
    return np.ascontiguousarray(rng.normal(0.0, 0.52, size=shape).astype(np.float64))


def initial_activation(config: WorkbenchConfig, entries: Iterable[dict[str, Any]]) -> np.ndarray:
    first = next(iter(entries))
    input_dim = int(first["layer_shape"][1])
    rng = _generator(
        config.seed,
        model_seed_part(config.model_tag),
        int(first["layer_index"]),
        0x41435456,
    )
    return np.ascontiguousarray(
        rng.normal(0.0, 0.65, size=(config.batch_size, input_dim)).astype(np.float64)
    )


def calibration_input(
    config: WorkbenchConfig,
    entry: dict[str, Any],
    *,
    batch_size: int | None = None,
) -> np.ndarray:
    input_dim = int(entry["layer_shape"][1])
    rng = _generator(
        int(entry["calib_seed"]),
        int(entry["layer_index"]),
        int(entry["weight_revision"]),
        0x43414C49,
    )
    size = int(batch_size if batch_size is not None else config.calib_batch_size)
    return np.ascontiguousarray(
        rng.normal(0.0, 0.72, size=(size, input_dim)).astype(np.float64)
    )


def block_policy_for(entry: dict[str, Any], *, batch_size: int) -> dict[str, int | str]:
    policy = entry.get("block_policy", {})
    if not isinstance(policy, dict):
        policy = {}
    return {
        "name": str(policy.get("name", "row_col")),
        "row_block": int(policy.get("row_block", min(2, batch_size))),
        "col_block": int(policy.get("col_block", 2)),
        "group_size": int(policy.get("group_size", 4)),
    }
