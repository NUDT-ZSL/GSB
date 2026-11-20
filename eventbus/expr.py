from __future__ import annotations

import ast
from typing import Any

from .errors import fail


_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Dict,
    ast.List,
    ast.Tuple,
    ast.Subscript,
    ast.Slice,
    ast.Call,
    ast.keyword,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.USub,
    ast.UAdd,
)

_FUNCTIONS = {
    "abs": abs,
    "int": int,
    "len": len,
    "lower": lambda value: str(value).lower(),
    "max": max,
    "min": min,
    "round": round,
    "str": str,
    "upper": lambda value: str(value).upper(),
}

_NAMES = {"payload", "event", "ops"}


def _validate(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            fail(
                "E_BAD_EXPR",
                "expression contains a disallowed construct",
                node=type(node).__name__,
            )
        if isinstance(node, ast.Name):
            if node.id.startswith("__") or (
                node.id not in _NAMES and node.id not in _FUNCTIONS
            ):
                fail("E_BAD_EXPR", "expression uses an unknown name", name=node.id)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                fail("E_BAD_EXPR", "only whitelisted function calls are allowed")
            if node.keywords:
                fail("E_BAD_EXPR", "keyword arguments are not allowed")


def compile_expression(expression: str, field: str) -> Any:
    if not isinstance(expression, str) or not expression.strip():
        fail("E_BAD_EXPR", "expression must be a non-empty string", field=field)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        fail(
            "E_BAD_EXPR",
            "expression is not valid Python expression syntax",
            field=field,
            reason=str(exc),
        )
    _validate(tree)
    return compile(tree, f"<{field}>", "eval")


def evaluate_expression(
    code: Any,
    payload: dict[str, Any],
    event: dict[str, Any],
    ops: dict[str, Any],
) -> Any:
    local_scope = {"payload": payload, "event": event, "ops": ops}
    try:
        return eval(code, {"__builtins__": {}}, {**_FUNCTIONS, **local_scope})
    except Exception as exc:
        fail(
            "E_TRANSFORM",
            "expression evaluation failed",
            reason=str(exc),
        )

