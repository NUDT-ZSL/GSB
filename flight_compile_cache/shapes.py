"""Symbolic and concrete tensor-shape rules for the demonstration graphs."""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping, Sequence

from .errors import (
    BusinessRuleError,
    E_CONST_SHAPE_MISMATCH,
    E_GRAPH_SCHEMA,
    E_INVALID_DIMENSION,
    E_INPUT_RANK_MISMATCH,
    E_INPUT_SHAPE_MISMATCH,
    E_MISSING_INPUT,
    E_OP_SHAPE_NOT_CLOSED,
    E_OP_UNSUPPORTED,
    E_REQUEST_SCHEMA,
    E_SHAPE_SYMBOL_CONFLICT,
)

Shape = tuple[Any, ...]
Dim = int | str

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_symbol(dim: Any) -> bool:
    return isinstance(dim, str) and bool(_SYMBOL_RE.match(dim))


def validate_dim_value(dim: Any, *, path: str = "", allow_symbol: bool = True) -> None:
    if isinstance(dim, bool):
        raise BusinessRuleError(E_INVALID_DIMENSION, f"invalid boolean dimension {dim!r}", path=path)
    if isinstance(dim, int):
        if dim <= 0:
            raise BusinessRuleError(
                E_INVALID_DIMENSION,
                f"tensor dimension must be positive, got {dim}",
                path=path,
            )
        return
    if allow_symbol and is_symbol(dim):
        return
    raise BusinessRuleError(
        E_INVALID_DIMENSION,
        f"invalid tensor dimension {dim!r}; expected a positive integer or symbolic name",
        path=path,
    )


def validate_shape(shape: Any, *, path: str = "", allow_symbol: bool = True) -> tuple[Any, ...]:
    if not isinstance(shape, (list, tuple)) or not shape:
        raise BusinessRuleError(E_INVALID_DIMENSION, "shape must be a non-empty list", path=path)
    for index, dim in enumerate(shape):
        validate_dim_value(dim, path=f"{path}[{index}]", allow_symbol=allow_symbol)
    return tuple(shape)


def shape_list(shape: Sequence[Any]) -> list[Any]:
    return [int(dim) if isinstance(dim, int) and not isinstance(dim, bool) else dim for dim in shape]


def numel(shape: Sequence[Any]) -> int | None:
    value = 1
    for dim in shape:
        if not isinstance(dim, int) or isinstance(dim, bool):
            return None
        value *= int(dim)
    return value


def broadcast_shapes(left: Sequence[Any], right: Sequence[Any], *, path: str = "") -> tuple[Any, ...]:
    """NumPy-style trailing broadcast for shape inference.

    Symbols are retained.  Concrete values are checked immediately; symbolic
    equality is checked again after a request binds the symbols.
    """

    width = max(len(left), len(right))
    out: list[Any] = []
    for offset in range(width):
        a = left[len(left) - width + offset] if offset >= width - len(left) else 1
        b = right[len(right) - width + offset] if offset >= width - len(right) else 1
        out.append(_merge_broadcast_dim(a, b, path=path))
    return tuple(out)


def _merge_broadcast_dim(a: Any, b: Any, *, path: str) -> Any:
    if a == b:
        return a
    if a == 1:
        return b
    if b == 1:
        return a
    if isinstance(a, int) and isinstance(b, int):
        raise BusinessRuleError(
            E_OP_SHAPE_NOT_CLOSED,
            f"cannot broadcast dimensions {a} and {b}",
            path=path,
        )
    # A symbolic dimension can match a concrete one after binding.  Preserve the
    # symbolic one so the declared graph shape remains readable.
    if is_symbol(a):
        return a
    return b


def _require_int(value: Any, *, name: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessRuleError(
            E_OP_SHAPE_NOT_CLOSED,
            f"{name} must be an integer, got {value!r}",
            path=path,
        )
    return value


def _normalize_axis(axis: Any, rank: int, *, path: str) -> int:
    value = _require_int(axis, name="axis", path=path)
    if value < 0:
        value += rank
    if value < 0 or value >= rank:
        raise BusinessRuleError(
            E_OP_SHAPE_NOT_CLOSED,
            f"axis {axis} is out of range for rank {rank}",
            path=path,
        )
    return value


def infer_shape(
    op: str,
    input_shapes: Sequence[Sequence[Any]],
    attrs: Mapping[str, Any] | None = None,
    *,
    path: str = "",
) -> tuple[Any, ...]:
    """Infer the output shape for a supported operator."""

    attrs = dict(attrs or {})
    shapes = [tuple(shape) for shape in input_shapes]

    if op == "const":
        if "value" not in attrs and "shape" not in attrs:
            raise BusinessRuleError(E_CONST_SHAPE_MISMATCH, "const node needs value or shape", path=path)
        if "shape" in attrs:
            return validate_shape(attrs["shape"], path=f"{path}.attrs.shape")
        return shape_from_nested(attrs["value"], path=f"{path}.attrs.value")[0]

    if op == "matmul":
        if len(shapes) != 2:
            raise BusinessRuleError(
                E_OP_SHAPE_NOT_CLOSED, "matmul requires exactly two inputs", path=path
            )
        left, right = shapes
        if len(left) < 2 or len(right) < 2:
            raise BusinessRuleError(
                E_OP_UNSUPPORTED,
                "matmul supports rank >= 2 tensors; scalar/vector matmul is not used",
                path=path,
            )
        left_batch = left[:-2]
        right_batch = right[:-2]
        batch = broadcast_shapes(left_batch, right_batch, path=path)
        k_left = left[-1]
        k_right = right[-2]
        if isinstance(k_left, int) and isinstance(k_right, int) and k_left != k_right:
            raise BusinessRuleError(
                E_OP_SHAPE_NOT_CLOSED,
                f"matmul inner dimensions differ: {k_left} vs {k_right}",
                path=path,
            )
        if is_symbol(k_left) and is_symbol(k_right) and k_left != k_right:
            raise BusinessRuleError(
                E_OP_SHAPE_NOT_CLOSED,
                f"matmul symbolic inner dimensions differ: {k_left} vs {k_right}",
                path=path,
            )
        return tuple(batch) + (left[-2], right[-1])

    if op in {"add", "sub", "mul", "div", "maximum", "minimum"}:
        if len(shapes) != 2:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, f"{op} requires two inputs", path=path)
        return broadcast_shapes(shapes[0], shapes[1], path=path)

    if op in {"relu", "tanh", "sigmoid", "identity", "softmax"}:
        if len(shapes) != 1:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, f"{op} requires one input", path=path)
        if op == "softmax":
            _normalize_axis(attrs.get("axis", -1), len(shapes[0]), path=path)
        return tuple(shapes[0])

    if op == "scale":
        if len(shapes) != 1:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "scale requires one input", path=path)
        return tuple(shapes[0])

    if op == "reduce_sum":
        if len(shapes) != 1:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "reduce_sum requires one input", path=path)
        shape = tuple(shapes[0])
        raw_axes = attrs.get("axis", attrs.get("axes"))
        axes: list[int]
        if isinstance(raw_axes, (list, tuple)):
            axes = [_normalize_axis(axis, len(shape), path=path) for axis in raw_axes]
        else:
            axes = [_normalize_axis(raw_axes, len(shape), path=path)]
        keepdims = bool(attrs.get("keepdims", False))
        out: list[Any] = []
        for index, dim in enumerate(shape):
            if index in axes:
                if keepdims:
                    out.append(1)
            else:
                out.append(dim)
        return tuple(out)

    if op == "reshape":
        if len(shapes) != 1:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "reshape requires one input", path=path)
        target = attrs.get("shape")
        if not isinstance(target, (list, tuple)) or not target:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "reshape needs attrs.shape", path=path)
        total = numel(shapes[0])
        target_ints = sum(1 for value in target if isinstance(value, int) and value > 0)
        out: list[Any] = []
        unknown = 0
        for value in target:
            if value == -1:
                unknown += 1
                out.append(-1)
            else:
                validate_dim_value(value, path=f"{path}.attrs.shape")
                out.append(value)
        if unknown > 1:
            raise BusinessRuleError(
                E_OP_SHAPE_NOT_CLOSED, "reshape may contain at most one -1", path=path
            )
        if unknown == 1 and total is not None:
            known = math.prod(value for value in out if value != -1)
            if known == 0 or total % known != 0:
                raise BusinessRuleError(
                    E_OP_SHAPE_NOT_CLOSED,
                    f"cannot reshape {tuple(shapes[0])} to {tuple(target)}",
                    path=path,
                )
            out[out.index(-1)] = total // known
        if total is not None and all(isinstance(value, int) and value > 0 for value in out):
            if math.prod(out) != total:
                raise BusinessRuleError(
                    E_OP_SHAPE_NOT_CLOSED,
                    f"reshape element count differs: {total} vs {math.prod(out)}",
                    path=path,
                )
        if target_ints and total is not None and numel(tuple(out)) is None:
            # Rare symbolic target with a concrete source; leave it symbolic.
            pass
        return tuple(out)

    if op == "transpose":
        if len(shapes) != 1:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "transpose requires one input", path=path)
        shape = tuple(shapes[0])
        perm = attrs.get("perm")
        if perm is None:
            perm = list(reversed(range(len(shape))))
        if not isinstance(perm, (list, tuple)) or sorted(perm) != list(range(len(shape))):
            raise BusinessRuleError(
                E_OP_SHAPE_NOT_CLOSED, "transpose perm must be a permutation of axes", path=path
            )
        return tuple(shape[index] for index in perm)

    if op == "concat":
        if not shapes:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "concat requires at least one input", path=path)
        rank = len(shapes[0])
        if any(len(shape) != rank for shape in shapes):
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "concat inputs must have equal rank", path=path)
        axis = _normalize_axis(attrs.get("axis", 0), rank, path=path)
        out = list(shapes[0])
        total: Any = 0
        symbolic: str | None = None
        for shape in shapes:
            for index in range(rank):
                if index == axis:
                    continue
                if shape[index] != out[index]:
                    raise BusinessRuleError(
                        E_OP_SHAPE_NOT_CLOSED,
                        f"concat non-axis dimensions differ at axis {index}: {shape[index]} vs {out[index]}",
                        path=path,
                    )
            dim = shape[axis]
            if isinstance(dim, int) and not isinstance(dim, bool):
                total += dim
            else:
                symbolic = symbolic or dim
        out[axis] = symbolic if symbolic is not None else total
        return tuple(out)

    if op == "slice":
        if len(shapes) != 1:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "slice requires one input", path=path)
        shape = list(shapes[0])
        axis = _normalize_axis(attrs.get("axis", 0), len(shape), path=path)
        start = attrs.get("start", 0)
        stop = attrs.get("stop")
        step = attrs.get("step", 1)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, step)):
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "slice start/step must be integers", path=path)
        if step == 0:
            raise BusinessRuleError(E_OP_SHAPE_NOT_CLOSED, "slice step cannot be zero", path=path)
        dim = shape[axis]
        if isinstance(dim, int) and not isinstance(dim, bool):
            normalized = slice(start, stop, step).indices(dim)
            shape[axis] = len(range(*normalized))
        elif stop is None and start == 0 and step == 1:
            shape[axis] = dim
        else:
            # Preserve a stable symbol for a partial symbolic slice.  Concrete
            # request validation will compare the declared output shape.
            shape[axis] = dim if stop is None else (dim if is_symbol(dim) else dim)
        return tuple(shape)

    raise BusinessRuleError(E_OP_UNSUPPORTED, f"unknown operator {op!r}", path=path)


def concrete_shape(shape: Sequence[Any], binding: Mapping[str, int], *, path: str = "") -> tuple[int, ...]:
    out: list[int] = []
    for index, dim in enumerate(shape):
        if isinstance(dim, bool):
            raise BusinessRuleError(E_INVALID_DIMENSION, "boolean dimension", path=f"{path}[{index}]")
        if isinstance(dim, int):
            value = dim
        elif isinstance(dim, str):
            if dim not in binding:
                raise BusinessRuleError(
                    E_SHAPE_SYMBOL_CONFLICT,
                    f"symbol {dim!r} is not bound by request inputs",
                    path=f"{path}[{index}]",
                )
            value = binding[dim]
        else:
            raise BusinessRuleError(
                E_INVALID_DIMENSION, f"invalid dimension {dim!r}", path=f"{path}[{index}]"
            )
        if value <= 0:
            raise BusinessRuleError(
                E_INVALID_DIMENSION,
                f"resolved tensor dimension must be positive, got {value}",
                path=f"{path}[{index}]",
            )
        out.append(int(value))
    return tuple(out)


def bind_input_shapes(
    graph_inputs: Sequence[Mapping[str, Any]],
    request_shapes: Mapping[str, Sequence[Any]],
    *,
    base_path: str = "input_shapes",
) -> dict[str, int]:
    """Bind graph input symbols to concrete request dimensions."""

    binding: dict[str, int] = {}
    for graph_input in graph_inputs:
        name = graph_input["name"]
        declared = graph_input["shape"]
        if name not in request_shapes:
            raise BusinessRuleError(
                E_MISSING_INPUT,
                f"missing request input {name!r}",
                path=f"{base_path}.{name}",
            )
        actual = request_shapes[name]
        if not isinstance(actual, (list, tuple)):
            raise BusinessRuleError(
                E_REQUEST_SCHEMA,
                f"input shape for {name!r} must be a list",
                path=f"{base_path}.{name}",
            )
        if len(actual) != len(declared):
            raise BusinessRuleError(
                E_INPUT_RANK_MISMATCH,
                f"input {name!r} rank {len(actual)} does not match graph rank {len(declared)}",
                path=f"{base_path}.{name}",
            )
        for axis, (expected, observed) in enumerate(zip(declared, actual)):
            if isinstance(observed, bool) or not isinstance(observed, int):
                raise BusinessRuleError(
                    E_INVALID_DIMENSION,
                    f"input {name!r} dimension {axis} must be an integer",
                    path=f"{base_path}.{name}[{axis}]",
                )
            if observed <= 0:
                raise BusinessRuleError(
                    E_INVALID_DIMENSION,
                    f"input {name!r} dimension {axis} must be positive, got {observed}",
                    path=f"{base_path}.{name}[{axis}]",
                )
            if isinstance(expected, int):
                if observed != expected:
                    raise BusinessRuleError(
                        E_INPUT_SHAPE_MISMATCH,
                        f"input {name!r} axis {axis} expected {expected}, got {observed}",
                        path=f"{base_path}.{name}[{axis}]",
                    )
            elif isinstance(expected, str):
                previous = binding.get(expected)
                if previous is not None and previous != observed:
                    raise BusinessRuleError(
                        E_SHAPE_SYMBOL_CONFLICT,
                        f"symbol {expected!r} bound to both {previous} and {observed}",
                        path=f"{base_path}.{name}[{axis}]",
                    )
                binding[expected] = observed
            else:
                raise BusinessRuleError(
                    E_GRAPH_SCHEMA,
                    f"graph input {name!r} has invalid declared dimension {expected!r}",
                    path=f"graph.inputs.{name}[{axis}]",
                )
    return binding


def shape_from_nested(value: Any, *, path: str = "") -> tuple[tuple[int, ...], list[float]]:
    """Flatten a JSON nested array and derive its rectangular shape."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (), [float(value)]
    if not isinstance(value, (list, tuple)):
        raise BusinessRuleError(E_CONST_SHAPE_MISMATCH, "constant value must be numeric or nested lists", path=path)
    if not value:
        raise BusinessRuleError(E_CONST_SHAPE_MISMATCH, "constant arrays must be non-empty", path=path)
    child_shape: tuple[int, ...] | None = None
    flat: list[float] = []
    for index, item in enumerate(value):
        item_shape, item_flat = shape_from_nested(item, path=f"{path}[{index}]")
        if child_shape is None:
            child_shape = item_shape
        elif child_shape != item_shape:
            raise BusinessRuleError(
                E_CONST_SHAPE_MISMATCH,
                "constant array is not rectangular",
                path=f"{path}[{index}]",
            )
        flat.extend(item_flat)
    return (len(value),) + (child_shape or ()), flat


def shapes_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return tuple(left) == tuple(right)


def describe_shape(shape: Iterable[Any]) -> str:
    return "[" + ",".join(str(dim) for dim in shape) + "]"

