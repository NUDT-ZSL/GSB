from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .errors import fail


PREDICATE_OPS = {
    "and",
    "or",
    "not",
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "isnan",
    "notnan",
    "between",
    "in",
}
VALUE_OPS = {"add", "sub", "mul", "div", "neg", "abs"}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def predicate_refs(predicate: Any, *, node_id: str = "") -> set[str]:
    if not isinstance(predicate, dict):
        fail("E_BAD_PLAN", "predicate must be an object", node_id=node_id or None)
    op = predicate.get("op")
    if op in {"and", "or"}:
        args = predicate.get("args")
        if not isinstance(args, list) or len(args) < 1:
            fail("E_BAD_PLAN", f"{op} predicate requires a non-empty args array", node_id=node_id or None)
        refs: set[str] = set()
        for arg in args:
            refs.update(predicate_refs(arg, node_id=node_id))
        return refs
    if op == "not":
        if "arg" not in predicate:
            fail("E_BAD_PLAN", "not predicate requires arg", node_id=node_id or None)
        return predicate_refs(predicate["arg"], node_id=node_id)
    if op in {"isnan", "notnan"}:
        column = predicate.get("column")
        if not isinstance(column, str):
            fail("E_BAD_PLAN", f"{op} predicate requires a column", node_id=node_id or None)
        return {column}
    if op in {"eq", "ne", "lt", "le", "gt", "ge"}:
        if "left" in predicate:
            return value_expr_refs(predicate["left"], node_id=node_id) | value_expr_refs(
                predicate.get("right"), node_id=node_id
            )
        column = predicate.get("column")
        if not isinstance(column, str) or "value" not in predicate:
            fail(
                "E_BAD_PLAN",
                f"{op} predicate requires column and value (or left and right)",
                node_id=node_id or None,
            )
        return {column}
    if op == "between":
        column = predicate.get("column")
        if not isinstance(column, str) or "low" not in predicate or "high" not in predicate:
            fail(
                "E_BAD_PLAN",
                "between predicate requires column, low, and high",
                node_id=node_id or None,
            )
        return {column}
    if op == "in":
        column = predicate.get("column")
        values = predicate.get("values")
        if not isinstance(column, str) or not isinstance(values, list):
            fail("E_BAD_PLAN", "in predicate requires column and values array", node_id=node_id or None)
        return {column}
    fail("E_BAD_PLAN", f"unsupported predicate operator {op!r}", node_id=node_id or None)


def value_expr_refs(expr: Any, *, node_id: str = "") -> set[str]:
    if not isinstance(expr, dict):
        fail("E_BAD_PLAN", "value expression must be an object", node_id=node_id or None)
    if "column" in expr:
        column = expr["column"]
        if not isinstance(column, str):
            fail("E_BAD_PLAN", "value expression column must be a string", node_id=node_id or None)
        return {column}
    if "const" in expr:
        value = expr["const"]
        if not isinstance(value, (bool, int, float, str)) or value is None:
            fail("E_BAD_PLAN", "value expression const has unsupported type", node_id=node_id or None)
        return set()
    op = expr.get("op")
    if op not in VALUE_OPS:
        fail("E_BAD_PLAN", f"unsupported value expression operator {op!r}", node_id=node_id or None)
    if op in {"neg", "abs"}:
        if "arg" not in expr:
            fail("E_BAD_PLAN", f"{op} expression requires arg", node_id=node_id or None)
        return value_expr_refs(expr["arg"], node_id=node_id)
    args = expr.get("args")
    if not isinstance(args, list) or len(args) < 2:
        fail("E_BAD_PLAN", f"{op} expression requires at least two args", node_id=node_id or None)
    refs: set[str] = set()
    for arg in args:
        refs.update(value_expr_refs(arg, node_id=node_id))
    return refs


def infer_value_expr_type(expr: Any, schema: dict[str, str], *, node_id: str = "") -> str:
    if not isinstance(expr, dict):
        fail("E_BAD_PLAN", "value expression must be an object", node_id=node_id or None)
    if "column" in expr:
        column = expr["column"]
        if column not in schema:
            fail(
                "E_PLAN_REF",
                f"expression references unknown column {column!r}",
                node_id=node_id or None,
            )
        return schema[column]
    if "const" in expr:
        value = expr["const"]
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int64"
        if isinstance(value, float):
            return "float64"
        return "string"
    op = expr.get("op")
    if op == "neg":
        return infer_value_expr_type(expr["arg"], schema, node_id=node_id)
    if op == "abs":
        return infer_value_expr_type(expr["arg"], schema, node_id=node_id)
    types = [infer_value_expr_type(arg, schema, node_id=node_id) for arg in expr["args"]]
    if "float64" in types or op == "div":
        return "float64"
    if "string" in types:
        fail("E_BAD_PLAN", "string arithmetic is not supported", node_id=node_id or None)
    return "int64"


def _array(expr: dict[str, Any], columns: dict[str, np.ndarray], row_count: int) -> np.ndarray:
    if "column" in expr:
        return np.asarray(columns[expr["column"]])
    if "const" in expr:
        value = expr["const"]
        dtype = object if isinstance(value, str) else None
        return np.full(row_count, value, dtype=dtype)
    raise AssertionError("not a leaf expression")


def eval_value_expr(
    expr: dict[str, Any],
    columns: dict[str, np.ndarray],
    row_count: int,
) -> np.ndarray:
    if "column" in expr or "const" in expr:
        return _array(expr, columns, row_count)
    op = expr["op"]
    if op == "neg":
        return np.negative(eval_value_expr(expr["arg"], columns, row_count))
    if op == "abs":
        return np.abs(eval_value_expr(expr["arg"], columns, row_count))
    values = [eval_value_expr(arg, columns, row_count) for arg in expr["args"]]
    result = values[0]
    for value in values[1:]:
        if op == "add":
            result = np.add(result, value)
        elif op == "sub":
            result = np.subtract(result, value)
        elif op == "mul":
            result = np.multiply(result, value)
        elif op == "div":
            result = np.true_divide(result, value)
        else:
            raise AssertionError(op)
    return np.asarray(result)


def eval_predicate(
    predicate: dict[str, Any],
    columns: dict[str, np.ndarray],
    row_count: int,
) -> np.ndarray:
    op = predicate["op"]
    if op == "and":
        return np.logical_and.reduce(
            [eval_predicate(arg, columns, row_count) for arg in predicate["args"]]
        )
    if op == "or":
        return np.logical_or.reduce(
            [eval_predicate(arg, columns, row_count) for arg in predicate["args"]]
        )
    if op == "not":
        return np.logical_not(eval_predicate(predicate["arg"], columns, row_count))
    if "left" in predicate:
        left = eval_value_expr(predicate["left"], columns, row_count)
        right = eval_value_expr(predicate["right"], columns, row_count)
    else:
        left = np.asarray(columns[predicate["column"]])
        right = predicate.get("value")
    if op == "eq":
        return np.equal(left, right)
    if op == "ne":
        return np.not_equal(left, right)
    if op == "lt":
        return np.less(left, right)
    if op == "le":
        return np.less_equal(left, right)
    if op == "gt":
        return np.greater(left, right)
    if op == "ge":
        return np.greater_equal(left, right)
    if op == "isnan":
        values = np.asarray(columns[predicate["column"]])
        if values.dtype.kind != "f":
            return np.zeros(row_count, dtype=bool)
        return np.isnan(values)
    if op == "notnan":
        values = np.asarray(columns[predicate["column"]])
        if values.dtype.kind != "f":
            return np.ones(row_count, dtype=bool)
        return ~np.isnan(values)
    if op == "between":
        values = np.asarray(columns[predicate["column"]])
        return np.logical_and(
            np.greater_equal(values, predicate["low"]),
            np.less_equal(values, predicate["high"]),
        )
    if op == "in":
        values = np.asarray(columns[predicate["column"]])
        return np.isin(values, np.asarray(predicate["values"]))
    raise AssertionError(op)


def combine_predicates(predicates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    values = list(predicates)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return {"op": "and", "args": values}

