"""Safe condition-expression parsing and interval (DNF) normalization.

Grammar (Python-like expression syntax, parsed with ``ast``):

    expr      := or_expr
    or_expr   := and_expr ("or" and_expr)*
    and_expr  := comparison ("and" comparison)*
    comparison:= NAME (("=="|"<"|"<="|">"|">=") VALUE | "in" "[" VALUE, ... "]")
    VALUE     := number | "string"

Forbidden constructs (rejected with E1004): ``not``, ``!=``, ``not in``,
function calls, attribute access, subscripts, arithmetic, boolean constants.
Unknown field names and type mismatches are rejected with E1003.

Every expression is normalized to disjunctive normal form: a tuple of
conjuncts, each conjunct mapping field name to an inclusive ``(lo, hi)``
interval. Intervals are stored as float pairs so NumPy can vectorize
overlap / coverage checks.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from . import errors

# Numeric fields and their inclusive domains.
NUMERIC_FIELDS: dict[str, tuple[float, float]] = {
    "port": (0.0, 65535.0),
    "risk_score": (0.0, 100.0),
}

# Enum fields mapped to integer codes so they share the interval machinery.
ENUM_FIELDS: dict[str, dict[str, int]] = {
    "proto": {"tcp": 1, "udp": 2, "icmp": 3},
    "src_zone": {"dmz": 1, "internal": 2, "external": 3},
}

ALLOWED_FIELDS = set(NUMERIC_FIELDS) | set(ENUM_FIELDS)


class ConditionError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# A conjunct: frozenset-like tuple of (field, lo, hi), sorted by field.
Conjunct = tuple[tuple[str, float, float], ...]


def _domain(field: str) -> tuple[float, float]:
    if field in NUMERIC_FIELDS:
        return NUMERIC_FIELDS[field]
    values = ENUM_FIELDS[field].values()
    return (float(min(values)), float(max(values)))


def _coerce_value(field: str, node: ast.expr) -> float:
    if field in NUMERIC_FIELDS:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return float(node.value)
        raise ConditionError(
            errors.E_CONDITION_SYNTAX,
            f"field '{field}' expects a numeric literal",
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        mapping = ENUM_FIELDS[field]
        if node.value not in mapping:
            raise ConditionError(
                errors.E_CONDITION_SYNTAX,
                f"field '{field}' has no value '{node.value}'",
            )
        return float(mapping[node.value])
    raise ConditionError(
        errors.E_CONDITION_SYNTAX,
        f"field '{field}' expects one of {sorted(ENUM_FIELDS[field])}",
    )


def _comparison_to_conjuncts(node: ast.Compare) -> list[Conjunct]:
    if not isinstance(node.left, ast.Name):
        raise ConditionError(errors.E_CONDITION_SYNTAX, "left side must be a field name")
    field = node.left.id
    if field not in ALLOWED_FIELDS:
        raise ConditionError(errors.E_CONDITION_SYNTAX, f"unknown field '{field}'")
    if len(node.ops) != 1 or len(node.comparators) != 1:
        raise ConditionError(errors.E_CONDITION_SYNTAX, "chained comparisons are not supported")
    op = node.ops[0]
    rhs = node.comparators[0]
    lo, hi = _domain(field)

    forbidden = (ast.NotEq, ast.NotIn)
    if isinstance(op, forbidden):
        raise ConditionError(errors.E_FORBIDDEN_OPERATOR, "operator '!=' / 'not in' is forbidden")
    if isinstance(op, ast.Eq):
        value = _coerce_value(field, rhs)
        interval = (value, value)
    elif isinstance(op, ast.Lt):
        interval = (lo, _coerce_value(field, rhs) - 1e-9)
    elif isinstance(op, ast.LtE):
        interval = (lo, _coerce_value(field, rhs))
    elif isinstance(op, ast.Gt):
        interval = (_coerce_value(field, rhs) + 1e-9, hi)
    elif isinstance(op, ast.GtE):
        interval = (_coerce_value(field, rhs), hi)
    elif isinstance(op, ast.In):
        if not isinstance(rhs, (ast.List, ast.Tuple)):
            raise ConditionError(errors.E_CONDITION_SYNTAX, "'in' expects a list literal")
        conjuncts: list[Conjunct] = []
        for elt in rhs.elts:
            value = _coerce_value(field, elt)
            conjuncts.append(((field, value, value),))
        return conjuncts
    else:
        raise ConditionError(
            errors.E_FORBIDDEN_OPERATOR, f"operator {type(op).__name__} is forbidden"
        )
    if interval[0] > interval[1]:
        return []  # unsatisfiable comparison -> empty DNF
    return [((field, interval[0], interval[1]),)]


def _merge(a: Conjunct, b: Conjunct) -> Conjunct | None:
    """Intersect two conjuncts; None when the intersection is empty."""
    merged = {f: (lo, hi) for f, lo, hi in a}
    for field, lo, hi in b:
        if field in merged:
            new_lo = max(merged[field][0], lo)
            new_hi = min(merged[field][1], hi)
            if new_lo > new_hi:
                return None
            merged[field] = (new_lo, new_hi)
        else:
            merged[field] = (lo, hi)
    return tuple(sorted((f, lo, hi) for f, (lo, hi) in merged.items()))


def _walk(node: ast.expr) -> list[Conjunct]:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: list[Conjunct] = [tuple()]
            for value in node.values:
                child = _walk(value)
                merged: list[Conjunct] = []
                for left in result:
                    for right in child:
                        m = _merge(left, right)
                        if m is not None:
                            merged.append(m)
                result = merged
            return result
        if isinstance(node.op, ast.Or):
            result = []
            for value in node.values:
                result.extend(_walk(value))
            return result
    if isinstance(node, ast.Compare):
        return _comparison_to_conjuncts(node)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        raise ConditionError(errors.E_FORBIDDEN_OPERATOR, "operator 'not' is forbidden")
    if isinstance(node, (ast.Call, ast.Attribute, ast.Subscript, ast.BinOp, ast.Name)):
        raise ConditionError(
            errors.E_FORBIDDEN_OPERATOR,
            f"construct {type(node).__name__} is forbidden in conditions",
        )
    raise ConditionError(
        errors.E_CONDITION_SYNTAX, f"unsupported syntax: {type(node).__name__}"
    )


def parse_condition(text: str) -> tuple[Conjunct, ...]:
    """Parse a condition expression into a canonical DNF tuple.

    Raises ConditionError with a business error code on any violation.
    """
    if not isinstance(text, str) or not text.strip():
        raise ConditionError(errors.E_CONDITION_SYNTAX, "condition must be a non-empty string")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ConditionError(errors.E_CONDITION_SYNTAX, f"syntax error: {exc.msg}") from exc
    conjuncts = _walk(tree.body)
    # Canonicalize: sort conjuncts so results do not depend on source order.
    return tuple(sorted(set(conjuncts)))


# ---------------------------------------------------------------------------
# Interval algebra over conjuncts (NumPy-backed).

import numpy as np  # noqa: E402


def _bounds(conjunct: Conjunct, fields: list[str]) -> tuple[np.ndarray, np.ndarray]:
    table = {f: (lo, hi) for f, lo, hi in conjunct}
    los, his = [], []
    for field in fields:
        lo, hi = table.get(field, _domain(field))
        los.append(lo)
        his.append(hi)
    return np.asarray(los), np.asarray(his)


def conjuncts_overlap(a: Conjunct, b: Conjunct) -> bool:
    fields = sorted({f for f, _, _ in a} | {f for f, _, _ in b})
    a_lo, a_hi = _bounds(a, fields)
    b_lo, b_hi = _bounds(b, fields)
    return bool(np.all(np.maximum(a_lo, b_lo) <= np.minimum(a_hi, b_hi)))


def conjunct_covers(outer: Conjunct, inner: Conjunct) -> bool:
    """True when ``outer`` covers every point of ``inner``."""
    fields = sorted({f for f, _, _ in outer} | {f for f, _, _ in inner})
    o_lo, o_hi = _bounds(outer, fields)
    i_lo, i_hi = _bounds(inner, fields)
    return bool(np.all(o_lo <= i_lo) and np.all(o_hi >= i_hi))


def dnf_overlaps(a: tuple[Conjunct, ...], b: tuple[Conjunct, ...]) -> bool:
    return any(conjuncts_overlap(x, y) for x in a for y in b)


def dnf_covers(outer: tuple[Conjunct, ...], inner: tuple[Conjunct, ...]) -> bool:
    """Conservative coverage: each inner conjunct must be covered by at least
    one single outer conjunct. (Documented limitation: union coverage across
    multiple outer conjuncts is not required for these samples.)"""
    if not inner:
        return False
    return all(
        any(conjunct_covers(o, i) for o in outer) for i in inner
    )
