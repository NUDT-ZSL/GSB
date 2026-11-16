"""Vectorized scalar kernels shared by both execution paths.

Nullability is represented by an explicit boolean bitmap (True == null).
Every kernel returns a :class:`ValueColumn` and never mutates its inputs, so
the naive and fused paths stay byte-identical by construction. ``division_zero``
records rows whose *non-null* divisor evaluated to zero (those rows become null).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .errors import BenchError, ErrorCode
from .model import BOOL, FLOAT, INT, dtype_of


@dataclass(frozen=True)
class ValueColumn:
    type_name: str
    values: np.ndarray
    nullmap: np.ndarray
    division_zero: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.dtype("bool"))
    )

    @property
    def row_count(self) -> int:
        return int(self.values.shape[0])


def _false_marks(row_count: int) -> np.ndarray:
    return np.zeros(row_count, dtype=np.dtype("bool"))


def _numeric_operation(op: str, left: ValueColumn, right: ValueColumn) -> ValueColumn:
    nullmap = left.nullmap | right.nullmap
    valid = ~nullmap
    result_type = FLOAT if FLOAT in (left.type_name, right.type_name) or op == "/" else INT
    out = np.zeros(left.row_count, dtype=dtype_of(result_type))
    marks = _false_marks(left.row_count)
    if op == "/":
        denominator = right.values.astype(np.float64, copy=False)
        zero_divisor = valid & (denominator == 0.0)
        nonzero = valid & ~zero_divisor
        computed = np.zeros_like(out)
        np.divide(
            left.values.astype(np.float64, copy=False),
            denominator,
            out=computed,
            where=nonzero,
        )
        out[nonzero] = computed[nonzero]
        return ValueColumn(result_type, out, nullmap | zero_divisor, zero_divisor)
    left_values = left.values
    right_values = right.values
    if result_type == FLOAT:
        left_values = left_values.astype(np.float64, copy=False)
        right_values = right_values.astype(np.float64, copy=False)
    if op == "%":
        zero_divisor = valid & (right_values == 0)
        nonzero = valid & ~zero_divisor
        result = np.zeros_like(left_values)
        result[nonzero] = np.mod(left_values[nonzero], right_values[nonzero])
        out[nonzero] = result[nonzero]
        return ValueColumn(result_type, out, nullmap | zero_divisor, zero_divisor)
    if op == "+":
        result = left_values + right_values
    elif op == "-":
        result = left_values - right_values
    elif op == "*":
        result = left_values * right_values
    else:  # pragma: no cover - parser guarantees op coverage
        raise BenchError(ErrorCode.E_EXPR, "unsupported arithmetic operator", {"operator": op})
    out[valid] = result[valid]
    return ValueColumn(result_type, out, nullmap, marks)


def _comparison_operation(op: str, left: ValueColumn, right: ValueColumn) -> ValueColumn:
    nullmap = left.nullmap | right.nullmap
    valid = ~nullmap
    out = np.zeros(left.row_count, dtype=np.dtype("bool"))
    left_values = left.values
    right_values = right.values
    if left.type_name == FLOAT or right.type_name == FLOAT:
        left_values = left_values.astype(np.float64, copy=False)
        right_values = right_values.astype(np.float64, copy=False)
    if op == "<":
        computed = left_values < right_values
    elif op == "<=":
        computed = left_values <= right_values
    elif op == ">":
        computed = left_values > right_values
    elif op == ">=":
        computed = left_values >= right_values
    elif op == "==":
        computed = left_values == right_values
    else:  # !=
        computed = left_values != right_values
    out[valid] = computed[valid]
    return ValueColumn(BOOL, out, nullmap, _false_marks(left.row_count))


def _logical_operation(op: str, left: ValueColumn, right: ValueColumn) -> ValueColumn:
    # SQL-style three-valued logic.
    a = left.values
    b = right.values
    an = left.nullmap
    bn = right.nullmap
    if op == "&&":
        values = a & b
        nullmap = (an | bn) & ~(a & b)
    else:  # ||
        values = a | b
        nullmap = (an | bn) & ~(a | b)
    return ValueColumn(BOOL, values, nullmap, _false_marks(left.row_count))


def apply_binary(op: str, left: ValueColumn, right: ValueColumn) -> ValueColumn:
    from .parser import ARITHMETIC, COMPARISON, LOGICAL

    if left.row_count != right.row_count:
        raise BenchError(ErrorCode.E_DATA, "binary operand row counts differ",
                         {"left": left.row_count, "right": right.row_count})
    if op in ARITHMETIC:
        return _numeric_operation(op, left, right)
    if op in COMPARISON:
        return _comparison_operation(op, left, right)
    if op in LOGICAL:
        return _logical_operation(op, left, right)
    raise BenchError(ErrorCode.E_EXPR, "unsupported binary operator", {"operator": op})


def apply_unary(op: str, operand: ValueColumn) -> ValueColumn:
    if op == "-":
        return ValueColumn(operand.type_name, -operand.values, operand.nullmap.copy(),
                           _false_marks(operand.row_count))
    if op == "!":
        return ValueColumn(BOOL, ~operand.values, operand.nullmap.copy(),
                           _false_marks(operand.row_count))
    raise BenchError(ErrorCode.E_EXPR, "unsupported unary operator", {"operator": op})


def apply_ternary(condition: ValueColumn, when_true: ValueColumn, when_false: ValueColumn) -> ValueColumn:
    if condition.type_name != BOOL:
        raise BenchError(ErrorCode.E_TYPE, "ternary condition must be boolean", {"type": condition.type_name})
    result_type = when_true.type_name if when_true.type_name == when_false.type_name else FLOAT
    out = np.zeros(condition.row_count, dtype=dtype_of(result_type))
    choose_true = (~condition.nullmap) & condition.values
    choose_false = (~condition.nullmap) & (~condition.values)
    true_values = when_true.values
    false_values = when_false.values
    if result_type == FLOAT:
        true_values = true_values.astype(np.float64, copy=False)
        false_values = false_values.astype(np.float64, copy=False)
    out[choose_true] = true_values[choose_true]
    out[choose_false] = false_values[choose_false]
    nullmap = np.ones(condition.row_count, dtype=np.dtype("bool"))
    nullmap[choose_true] = when_true.nullmap[choose_true]
    nullmap[choose_false] = when_false.nullmap[choose_false]
    return ValueColumn(result_type, out, nullmap, _false_marks(condition.row_count))


def literal_column(node, row_count: int) -> ValueColumn:
    values = np.full(row_count, node.value, dtype=dtype_of(node.type_name))
    return ValueColumn(node.type_name, values, _false_marks(row_count), _false_marks(row_count))
