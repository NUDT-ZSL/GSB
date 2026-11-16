"""Fixed-seed input tensor descriptor handling."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessError, E_INPUT_INVALID, E_SHAPE_MISMATCH
from .graph import Graph
from .util import sha256_json, tensor_fingerprint, tensor_summary


@dataclass(frozen=True)
class InputDescriptor:
    raw: dict[str, Any]
    descriptor_hash: str


@dataclass(frozen=True)
class GeneratedInput:
    tensor_id: str
    tensor: np.ndarray
    descriptor: InputDescriptor
    fingerprint: str
    summary: dict[str, Any]


def load_input_descriptor(path: Path) -> InputDescriptor:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(E_INPUT_INVALID, f"cannot load input descriptor: {exc}", stage="input") from exc
    if not isinstance(raw, dict):
        raise BusinessError(E_INPUT_INVALID, "input descriptor must be a JSON object", stage="input")
    return InputDescriptor(raw=raw, descriptor_hash=sha256_json(raw))


def _finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise BusinessError(E_INPUT_INVALID, f"{field} must be a finite number", stage="input")
    return float(value)


def _special_value(value: Any, field: str) -> float:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "nan":
            return float("nan")
        if lowered in {"inf", "+inf", "infinity", "+infinity"}:
            return float("inf")
        if lowered in {"-inf", "-infinity"}:
            return float("-inf")
        raise BusinessError(E_INPUT_INVALID, f"{field} has unsupported special value {value!r}", stage="input")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(E_INPUT_INVALID, f"{field} must be numeric or one of nan/inf/-inf", stage="input")
    return float(value)


def generate_input(descriptor: InputDescriptor, graph: Graph) -> GeneratedInput:
    raw = descriptor.raw
    tensor_id = raw.get("tensor_id", next(iter(graph.input_tensors)))
    if not isinstance(tensor_id, str) or tensor_id not in graph.input_tensors:
        raise BusinessError(
            E_INPUT_INVALID,
            "input tensor_id must reference a graph input tensor",
            stage="input",
            details={"tensor_id": tensor_id, "graph_inputs": sorted(graph.input_tensors)},
        )
    shape_raw = raw.get("shape")
    graph_shape = graph.input_tensors[tensor_id]["shape"]
    if (
        not isinstance(shape_raw, list)
        or len(shape_raw) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in shape_raw)
    ):
        raise BusinessError(E_INPUT_INVALID, "input shape must contain four positive integers", stage="input")
    shape = tuple(int(item) for item in shape_raw)
    if list(shape) != graph_shape:
        raise BusinessError(
            E_SHAPE_MISMATCH,
            "input descriptor shape does not match graph input shape",
            stage="input",
            details={"descriptor_shape": list(shape), "graph_shape": graph_shape},
        )
    if raw.get("dtype", "float64") != "float64":
        raise BusinessError(E_INPUT_INVALID, "input dtype must be float64", stage="input")
    if raw.get("layout", "NCHW") != "NCHW":
        raise BusinessError(E_INPUT_INVALID, "input layout must be NCHW", stage="input")
    generator = raw.get("generator")
    if not isinstance(generator, dict):
        raise BusinessError(E_INPUT_INVALID, "input generator must be an object", stage="input")
    if generator.get("kind", "numpy_pcg64") != "numpy_pcg64":
        raise BusinessError(E_INPUT_INVALID, "input generator.kind must be numpy_pcg64", stage="input")
    seed = generator.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise BusinessError(E_INPUT_INVALID, "input generator.seed must be a non-negative integer", stage="input")
    distribution = generator.get("distribution")
    if not isinstance(distribution, dict):
        raise BusinessError(E_INPUT_INVALID, "input generator.distribution must be an object", stage="input")
    kind = distribution.get("kind")
    rng = np.random.Generator(np.random.PCG64(seed))
    if kind == "uniform":
        low = _finite_float(distribution.get("low"), "distribution.low")
        high = _finite_float(distribution.get("high"), "distribution.high")
        if not low < high:
            raise BusinessError(E_INPUT_INVALID, "uniform distribution requires low < high", stage="input")
        array = rng.uniform(low, high, size=shape).astype(np.float64, copy=False)
    elif kind == "normal":
        loc = _finite_float(distribution.get("loc"), "distribution.loc")
        scale = _finite_float(distribution.get("scale"), "distribution.scale")
        if scale <= 0:
            raise BusinessError(E_INPUT_INVALID, "normal distribution scale must be > 0", stage="input")
        array = rng.normal(loc, scale, size=shape).astype(np.float64, copy=False)
    elif kind == "constant":
        value = _special_value(distribution.get("value"), "distribution.value")
        array = np.full(shape, value, dtype=np.float64)
    else:
        raise BusinessError(E_INPUT_INVALID, "distribution.kind must be uniform, normal, or constant", stage="input")
    special_values = raw.get("special_values", [])
    if not isinstance(special_values, list):
        raise BusinessError(E_INPUT_INVALID, "special_values must be an array", stage="input")
    array = np.ascontiguousarray(array, dtype=np.float64)
    for index, item in enumerate(special_values):
        if not isinstance(item, dict):
            raise BusinessError(E_INPUT_INVALID, f"special_values[{index}] must be an object", stage="input")
        position = item.get("index")
        if (
            not isinstance(position, list)
            or len(position) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in position)
        ):
            raise BusinessError(E_INPUT_INVALID, f"special_values[{index}].index must contain four integers", stage="input")
        for axis, value in enumerate(position):
            if not 0 <= value < shape[axis]:
                raise BusinessError(
                    E_INPUT_INVALID,
                    f"special_values[{index}].index is outside input shape",
                    stage="input",
                    details={"index": position, "shape": list(shape)},
                )
        array[tuple(position)] = np.float64(_special_value(item.get("value"), f"special_values[{index}].value"))
    array.setflags(write=False)
    fingerprint = tensor_fingerprint(array)
    summary = tensor_summary(array)
    summary["descriptor_hash"] = descriptor.descriptor_hash
    summary["generator"] = {"kind": "numpy_pcg64", "seed": seed}
    return GeneratedInput(
        tensor_id=tensor_id,
        tensor=array,
        descriptor=descriptor,
        fingerprint=fingerprint,
        summary=summary,
    )
