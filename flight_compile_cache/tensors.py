"""A tiny deterministic tensor runtime used by both compilation paths."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .errors import BusinessRuleError, E_OP_SHAPE_NOT_CLOSED, E_OP_UNSUPPORTED
from .shapes import broadcast_shapes, numel, shape_from_nested
from .util import canonical_json


@dataclass
class Tensor:
    shape: tuple[int, ...]
    data: list[float]

    def __post_init__(self) -> None:
        expected = numel(self.shape)
        if expected is None or len(self.data) != expected:
            raise ValueError(
                f"tensor shape {self.shape} requires {expected} elements, got {len(self.data)}"
            )

    def copy(self) -> "Tensor":
        return Tensor(tuple(self.shape), list(self.data))

    def to_dict(self, *, preview: int = 8) -> dict[str, Any]:
        result: dict[str, Any] = {
            "shape": list(self.shape),
            "dtype": "float64",
            "preview": [round(value, 10) for value in self.data[:preview]],
            "fingerprint": tensor_fingerprint(self),
        }
        if len(self.data) <= 64:
            result["data"] = [round(value, 12) for value in self.data]
        return result


def tensor_fingerprint(tensor: Tensor) -> str:
    payload = {
        "shape": list(tensor.shape),
        "data": [round(value, 12) for value in tensor.data],
    }
    return "tensor_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def tensor_from_value(value: Any) -> Tensor:
    shape, data = shape_from_nested(value)
    return Tensor(shape, data)


def make_input_tensor(shape: Sequence[int], seed_material: Any) -> Tensor:
    """Create reproducible, non-degenerate inputs from a logical seed."""

    digest = hashlib.sha256(canonical_json(seed_material).encode("utf-8")).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    size = math.prod(shape)
    values = [round(rng.uniform(-0.85, 0.85), 8) for _ in range(size)]
    return Tensor(tuple(int(dim) for dim in shape), values)


def _strides(shape: Sequence[int]) -> tuple[int, ...]:
    out = [1] * len(shape)
    for index in range(len(shape) - 2, -1, -1):
        out[index] = out[index + 1] * shape[index + 1]
    return tuple(out)


def _coordinates(flat_index: int, shape: Sequence[int], strides: Sequence[int]) -> tuple[int, ...]:
    coords: list[int] = []
    remainder = flat_index
    for dim, stride in zip(shape, strides):
        coords.append(remainder // stride)
        remainder %= stride
    return tuple(coords)


def _flat_index(coords: Sequence[int], strides: Sequence[int]) -> int:
    return sum(coord * stride for coord, stride in zip(coords, strides))


def _broadcast_input_coords(
    output_coords: Sequence[int], output_rank: int, input_shape: Sequence[int]
) -> tuple[int, ...]:
    offset = output_rank - len(input_shape)
    coords: list[int] = []
    for index, dim in enumerate(input_shape):
        coords.append(0 if dim == 1 else output_coords[offset + index])
    return tuple(coords)


def _elementwise(
    op: str,
    left: Tensor,
    right: Tensor | None = None,
    *,
    factor: float | None = None,
) -> Tensor:
    if right is None:
        out_shape = tuple(left.shape)
        out_size = len(left.data)
        data: list[float] = []
        if op == "relu":
            data = [value if value > 0.0 else 0.0 for value in left.data]
        elif op == "tanh":
            data = [math.tanh(value) for value in left.data]
        elif op == "sigmoid":
            data = [1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, value)))) for value in left.data]
        elif op == "scale":
            scale = float(factor if factor is not None else 1.0)
            data = [value * scale for value in left.data]
        elif op == "identity":
            data = list(left.data)
        else:
            raise BusinessRuleError(E_OP_UNSUPPORTED, f"unsupported unary op {op}")
        return Tensor(out_shape, data)

    out_shape = broadcast_shapes(tuple(left.shape), tuple(right.shape))
    if not all(isinstance(dim, int) for dim in out_shape):
        raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "concrete execution received symbolic dimensions")
    out_shape = tuple(int(dim) for dim in out_shape)
    out_size = math.prod(out_shape)
    out_strides = _strides(out_shape)
    left_strides = _strides(left.shape)
    right_strides = _strides(right.shape)
    data = []
    for flat_index in range(out_size):
        coords = _coordinates(flat_index, out_shape, out_strides)
        left_coords = _broadcast_input_coords(coords, len(out_shape), left.shape)
        right_coords = _broadcast_input_coords(coords, len(out_shape), right.shape)
        a = left.data[_flat_index(left_coords, left_strides)]
        b = right.data[_flat_index(right_coords, right_strides)]
        if op == "add":
            data.append(a + b)
        elif op == "sub":
            data.append(a - b)
        elif op == "mul":
            data.append(a * b)
        elif op == "div":
            data.append(a / b if b != 0.0 else 0.0)
        elif op == "maximum":
            data.append(max(a, b))
        elif op == "minimum":
            data.append(min(a, b))
        else:
            raise BusinessRuleError(E_OP_UNSUPPORTED, f"unsupported binary op {op}")
    return Tensor(out_shape, data)


def _matmul(left: Tensor, right: Tensor) -> Tensor:
    if len(left.shape) < 2 or len(right.shape) < 2:
        raise BusinessRuleError(E_OP_UNSUPPORTED, "matmul requires rank >= 2 tensors")
    m, k = left.shape[-2:]
    k2, n = right.shape[-2:]
    if k != k2:
        raise BusinessRuleError(
            E_OP_SHAPE_NOT_CLOSED, f"matmul inner dimensions differ: {k} vs {k2}"
        )
    left_batch = left.shape[:-2]
    right_batch = right.shape[:-2]
    batch_shape = broadcast_shapes(left_batch, right_batch)
    if not all(isinstance(dim, int) for dim in batch_shape):
        raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "matmul batch shape is not concrete")
    batch_shape = tuple(int(dim) for dim in batch_shape)
    batch_size = math.prod(batch_shape) if batch_shape else 1
    batch_strides = _strides(batch_shape) if batch_shape else ()
    left_batch_strides = _strides(left_batch) if left_batch else ()
    right_batch_strides = _strides(right_batch) if right_batch else ()
    left_matrix_size = m * k
    right_matrix_size = k * n
    data = [0.0] * (batch_size * m * n)
    for batch_flat in range(batch_size):
        batch_coords = (
            _coordinates(batch_flat, batch_shape, batch_strides) if batch_shape else ()
        )
        left_batch_coords = _broadcast_input_coords(
            batch_coords, len(batch_shape), left_batch
        )
        right_batch_coords = _broadcast_input_coords(
            batch_coords, len(batch_shape), right_batch
        )
        left_base = (
            _flat_index(left_batch_coords, left_batch_strides) * left_matrix_size
            if left_batch
            else 0
        )
        right_base = (
            _flat_index(right_batch_coords, right_batch_strides) * right_matrix_size
            if right_batch
            else 0
        )
        out_base = batch_flat * m * n
        for row in range(m):
            left_row = left_base + row * k
            for inner in range(k):
                a = left.data[left_row + inner]
                if a == 0.0:
                    continue
                right_row = right_base + inner * n
                for col in range(n):
                    data[out_base + row * n + col] += a * right.data[right_row + col]
    return Tensor(batch_shape + (m, n), data)


def _reduce_sum(tensor: Tensor, axis: Any, keepdims: bool) -> Tensor:
    axes_raw = axis if isinstance(axis, (list, tuple)) else [axis]
    axes = sorted({int(item) % len(tensor.shape) for item in axes_raw})
    out_shape = []
    for index, dim in enumerate(tensor.shape):
        if index in axes:
            if keepdims:
                out_shape.append(1)
        else:
            out_shape.append(dim)
    out_strides = _strides(out_shape)
    source_strides = _strides(tensor.shape)
    data = [0.0] * math.prod(out_shape)
    for source_flat, value in enumerate(tensor.data):
        source_coords = _coordinates(source_flat, tensor.shape, source_strides)
        out_coords = []
        for index, coord in enumerate(source_coords):
            if index in axes:
                if keepdims:
                    out_coords.append(0)
            else:
                out_coords.append(coord)
        data[_flat_index(out_coords, out_strides)] += value
    return Tensor(tuple(out_shape), data)


def _reshape(tensor: Tensor, target: Sequence[int]) -> Tensor:
    target = [int(value) for value in target]
    if any(value <= 0 for value in target):
        raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, f"invalid concrete reshape target {target}")
    if math.prod(target) != len(tensor.data):
        raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "reshape element count mismatch")
    return Tensor(tuple(target), list(tensor.data))


def _transpose(tensor: Tensor, perm: Sequence[int]) -> Tensor:
    out_shape = tuple(tensor.shape[index] for index in perm)
    source_strides = _strides(tensor.shape)
    out_strides = _strides(out_shape)
    data = [0.0] * len(tensor.data)
    for out_flat in range(len(data)):
        out_coords = _coordinates(out_flat, out_shape, out_strides)
        source_coords = [0] * len(perm)
        for out_axis, source_axis in enumerate(perm):
            source_coords[source_axis] = out_coords[out_axis]
        data[out_flat] = tensor.data[_flat_index(source_coords, source_strides)]
    return Tensor(out_shape, data)


def _concat(tensors: Sequence[Tensor], axis: int) -> Tensor:
    rank = len(tensors[0].shape)
    axis = axis % rank
    out_shape = list(tensors[0].shape)
    out_shape[axis] = sum(tensor.shape[axis] for tensor in tensors)
    out_strides = _strides(out_shape)
    data = [0.0] * math.prod(out_shape)
    axis_offset = 0
    for tensor in tensors:
        source_strides = _strides(tensor.shape)
        for source_flat, value in enumerate(tensor.data):
            coords = list(_coordinates(source_flat, tensor.shape, source_strides))
            coords[axis] += axis_offset
            data[_flat_index(coords, out_strides)] = value
        axis_offset += tensor.shape[axis]
    return Tensor(tuple(out_shape), data)


def _slice(tensor: Tensor, axis: int, start: int, stop: int | None, step: int) -> Tensor:
    rank = len(tensor.shape)
    axis = axis % rank
    selected = list(range(*slice(start, stop, step).indices(tensor.shape[axis])))
    out_shape = list(tensor.shape)
    out_shape[axis] = len(selected)
    out_strides = _strides(out_shape)
    source_strides = _strides(tensor.shape)
    data = [0.0] * math.prod(out_shape)
    for out_flat in range(len(data)):
        coords = list(_coordinates(out_flat, out_shape, out_strides))
        coords[axis] = selected[coords[axis]]
        data[out_flat] = tensor.data[_flat_index(coords, source_strides)]
    return Tensor(tuple(out_shape), data)


def _softmax(tensor: Tensor, axis: int) -> Tensor:
    rank = len(tensor.shape)
    axis = axis % rank
    out_strides = _strides(tensor.shape)
    # Iterate over all slices orthogonal to the softmax axis.
    other_axes = [index for index in range(rank) if index != axis]
    other_shape = [tensor.shape[index] for index in other_axes]
    other_strides = _strides(other_shape) if other_shape else ()
    data = [0.0] * len(tensor.data)
    for other_flat in range(math.prod(other_shape) if other_shape else 1):
        coords = [0] * rank
        if other_shape:
            other_coords = _coordinates(other_flat, other_shape, other_strides)
            for axis_index, coord in zip(other_axes, other_coords):
                coords[axis_index] = coord
        values = []
        for position in range(tensor.shape[axis]):
            coords[axis] = position
            values.append(tensor.data[_flat_index(coords, out_strides)])
        maximum = max(values)
        exps = [math.exp(value - maximum) for value in values]
        total = sum(exps)
        for position, value in enumerate(exps):
            coords[axis] = position
            data[_flat_index(coords, out_strides)] = value / total
    return Tensor(tuple(tensor.shape), data)


def apply_op(
    op: str,
    inputs: Sequence[Tensor],
    attrs: Mapping[str, Any] | None = None,
    *,
    kernel_spec: Mapping[str, Any] | None = None,
) -> Tensor:
    """Execute one operator.

    ``kernel_spec`` is present for a shape-specialized artifact.  It records the
    shapes and axes selected at compile time; execution checks them before
    running, which is the local stand-in for a statically specialized kernel.
    """

    attrs = dict(attrs or {})
    if op == "const":
        return tensor_from_value(attrs["value"])

    if op == "matmul":
        if len(inputs) != 2:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "matmul requires two inputs")
        if kernel_spec is not None:
            expected = tuple(tuple(shape) for shape in kernel_spec.get("input_shapes", []))
            actual = tuple(tuple(tensor.shape) for tensor in inputs)
            if expected and expected != actual:
                raise BusinessRuleError(
                    E_OP_SHAPE_NOT_CLOSED,
                    f"specialized matmul expected {expected}, got {actual}",
                )
        return _matmul(inputs[0], inputs[1])

    if op in {"add", "sub", "mul", "div", "maximum", "minimum"}:
        if len(inputs) != 2:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, f"{op} requires two inputs")
        return _elementwise(op, inputs[0], inputs[1])

    if op in {"relu", "tanh", "sigmoid", "identity"}:
        return _elementwise(op, inputs[0])

    if op == "scale":
        return _elementwise(op, inputs[0], factor=float(attrs.get("factor", 1.0)))

    if op == "reduce_sum":
        if kernel_spec is not None:
            axes = kernel_spec.get("axes")
            keepdims = bool(kernel_spec.get("keepdims"))
            if axes is not None:
                return _reduce_sum(inputs[0], list(axes), keepdims)
        return _reduce_sum(
            inputs[0], attrs.get("axis", attrs.get("axes")), bool(attrs.get("keepdims", False))
        )

    if op == "reshape":
        target = (
            kernel_spec.get("output_shape")
            if kernel_spec is not None and kernel_spec.get("output_shape") is not None
            else attrs.get("shape")
        )
        return _reshape(inputs[0], target)

    if op == "transpose":
        perm = (
            kernel_spec.get("perm")
            if kernel_spec is not None and kernel_spec.get("perm") is not None
            else attrs.get("perm")
        )
        if perm is None:
            perm = list(reversed(range(len(inputs[0].shape))))
        return _transpose(inputs[0], perm)

    if op == "concat":
        axis = (
            kernel_spec.get("axis")
            if kernel_spec is not None and kernel_spec.get("axis") is not None
            else attrs.get("axis", 0)
        )
        return _concat(inputs, int(axis))

    if op == "slice":
        axis = (
            kernel_spec.get("axis")
            if kernel_spec is not None and kernel_spec.get("axis") is not None
            else attrs.get("axis", 0)
        )
        start = (
            kernel_spec.get("start")
            if kernel_spec is not None and kernel_spec.get("start") is not None
            else attrs.get("start", 0)
        )
        stop = (
            kernel_spec.get("stop")
            if kernel_spec is not None and "stop" in kernel_spec
            else attrs.get("stop")
        )
        step = (
            kernel_spec.get("step")
            if kernel_spec is not None and kernel_spec.get("step") is not None
            else attrs.get("step", 1)
        )
        return _slice(inputs[0], int(axis), int(start), stop, int(step))

    if op == "softmax":
        axis = (
            kernel_spec.get("axis")
            if kernel_spec is not None and kernel_spec.get("axis") is not None
            else attrs.get("axis", -1)
        )
        return _softmax(inputs[0], int(axis))

    raise BusinessRuleError(E_OP_UNSUPPORTED, f"unknown operator {op!r}")


def compare_outputs(
    specialized: Mapping[str, Tensor],
    baseline: Mapping[str, Tensor],
    tolerance: float,
    *,
    max_violations: int = 24,
) -> dict[str, Any]:
    """Compare two output dictionaries element by element."""

    missing = sorted(set(baseline) - set(specialized))
    extra = sorted(set(specialized) - set(baseline))
    violations: list[dict[str, Any]] = []
    output_errors: dict[str, Any] = {}
    overall_max = 0.0
    overall_location: dict[str, Any] | None = None
    all_consistent = not missing and not extra

    for name in sorted(set(specialized) & set(baseline)):
        left = specialized[name]
        right = baseline[name]
        output_max = 0.0
        output_location: dict[str, Any] | None = None
        shape_mismatch = left.shape != right.shape
        if shape_mismatch:
            all_consistent = False
            violations.append(
                {
                    "output": name,
                    "kind": "shape_mismatch",
                    "specialized_shape": list(left.shape),
                    "baseline_shape": list(right.shape),
                }
            )
            output_errors[name] = {
                "consistent": False,
                "shape_mismatch": True,
                "specialized_shape": list(left.shape),
                "baseline_shape": list(right.shape),
            }
            continue
        for index, (a, b) in enumerate(zip(left.data, right.data)):
            error = abs(a - b)
            if not math.isfinite(error):
                error = math.inf
            if error > output_max:
                output_max = error
                output_location = {
                    "output": name,
                    "flat_index": index,
                    "index": _unravel(index, left.shape),
                    "specialized": round(a, 12),
                    "baseline": round(b, 12),
                    "abs_error": error if math.isfinite(error) else "inf",
                }
            if error > tolerance:
                all_consistent = False
                if len(violations) < max_violations:
                    violations.append(
                        {
                            "output": name,
                            "kind": "element_mismatch",
                            "flat_index": index,
                            "index": _unravel(index, left.shape),
                            "specialized": round(a, 12),
                            "baseline": round(b, 12),
                            "abs_error": error if math.isfinite(error) else "inf",
                        }
                    )
        if output_max > overall_max:
            overall_max = output_max
            overall_location = output_location
        output_errors[name] = {
            "consistent": output_max <= tolerance,
            "max_abs_error": output_max if math.isfinite(output_max) else "inf",
            "max_location": output_location,
            "shape": list(left.shape),
        }

    if missing:
        violations.append({"kind": "missing_outputs", "outputs": missing})
    if extra:
        violations.append({"kind": "extra_outputs", "outputs": extra})
    return {
        "consistent": all_consistent,
        "tolerance": tolerance,
        "max_abs_error": overall_max if math.isfinite(overall_max) else "inf",
        "max_location": overall_location,
        "violations": violations,
        "violation_count": len(violations),
        "outputs": output_errors,
    }


def _unravel(index: int, shape: Sequence[int]) -> list[int]:
    coords = [0] * len(shape)
    remainder = index
    for axis in range(len(shape) - 1, -1, -1):
        dim = shape[axis]
        coords[axis] = remainder % dim
        remainder //= dim
    return coords


def run_graph(
    nodes: Sequence[Mapping[str, Any]],
    graph_inputs: Mapping[str, Tensor],
    outputs: Sequence[str],
    *,
    kernel_specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Tensor]:
    """Evaluate a topologically ordered node list."""

    values: dict[str, Tensor] = dict(graph_inputs)
    for node in nodes:
        node_id = node["id"]
        node_inputs = [values[reference] for reference in node.get("inputs", [])]
        spec = None if kernel_specs is None else kernel_specs.get(node_id)
        values[node_id] = apply_op(node["op"], node_inputs, node.get("attrs", {}), kernel_spec=spec)
    return {name: values[name] for name in outputs}
