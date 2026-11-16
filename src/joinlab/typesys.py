"""Small, explicit SQL-like type system and canonical value handling."""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import SchemaError, TypeMismatchError


SUPPORTED_TYPES = frozenset({"integer", "real", "text", "boolean", "date"})
NUMERIC_TYPES = frozenset({"integer", "real"})
COMPARISON_OPS = frozenset({"=", "<>", "!=", "<", "<=", ">", ">="})
ORDERING_OPS = frozenset({"<", "<=", ">", ">="})


def normalize_type(type_name: Any, path: str) -> str:
    if not isinstance(type_name, str):
        raise SchemaError("column type must be a string", path)
    value = type_name.strip().lower()
    if value not in SUPPORTED_TYPES:
        raise SchemaError(
            f"unsupported column type {type_name!r}; expected one of {sorted(SUPPORTED_TYPES)}",
            path,
            supported=sorted(SUPPORTED_TYPES),
        )
    return value


def type_comparable(left: str, right: str, op: str) -> bool:
    """Return whether the pair can be compared under *op*.

    Numeric types are mutually comparable.  Date values are represented as
    ISO-8601 strings and are intentionally comparable with text.  Boolean is
    restricted to equality, mirroring the deliberately small experiment
    language rather than full SQL three-valued logic.
    """

    if op not in COMPARISON_OPS:
        return False
    if left in NUMERIC_TYPES and right in NUMERIC_TYPES:
        return True
    if left == right:
        return True
    if {left, right} == {"date", "text"}:
        return True
    return False


def literal_comparable(column_type: str, literal_type: str, op: str) -> bool:
    if literal_type == "null":
        return False
    if column_type in NUMERIC_TYPES:
        return literal_type in {"integer", "real"}
    if column_type == "text":
        return literal_type == "text"
    if column_type == "date":
        return literal_type == "text"
    if column_type == "boolean":
        return literal_type == "boolean" and op == "="
    return False


def validate_value(value: Any, type_name: str, path: str, nullable: bool = True) -> Any:
    """Validate a JSON value against a declared type and return it normalized."""

    if value is None:
        if nullable:
            return None
        raise SchemaError("null is not allowed for a non-nullable column", path)
    if type_name == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError("expected integer", path)
        return value
    if type_name == "real":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError("expected real number", path)
        number = float(value)
        if not math.isfinite(number):
            raise SchemaError("real value must be finite", path)
        return number
    if type_name == "boolean":
        if not isinstance(value, bool):
            raise SchemaError("expected boolean", path)
        return value
    if type_name in {"text", "date"}:
        if not isinstance(value, str):
            raise SchemaError(f"expected {type_name} string", path)
        if type_name == "date":
            # The accepted grammar intentionally stays dependency-free.  The
            # lexical ISO date shape is enough for deterministic comparisons.
            import datetime

            try:
                datetime.date.fromisoformat(value)
            except ValueError as exc:
                raise SchemaError("date must be ISO-8601 YYYY-MM-DD", path) from exc
        return value
    raise SchemaError(f"unsupported type {type_name!r}", path)


def coerce_for_compare(value: Any, type_name: str, path: str = "") -> Any:
    if value is None:
        return None
    if type_name in NUMERIC_TYPES:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeMismatchError("value cannot be compared as a number", path)
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise TypeMismatchError("invalid numeric value", path) from exc
    if type_name in {"text", "date"}:
        if not isinstance(value, str):
            raise TypeMismatchError("value cannot be compared as text", path)
        return value
    if type_name == "boolean":
        if not isinstance(value, bool):
            raise TypeMismatchError("value cannot be compared as boolean", path)
        return value
    raise TypeMismatchError(f"unsupported comparison type {type_name!r}", path)


def compare_values(left: Any, right: Any, op: str, left_type: str, right_type: str, path: str = "") -> bool:
    if left is None or right is None:
        return False
    if not type_comparable(left_type, right_type, op):
        raise TypeMismatchError(
            f"types {left_type!r} and {right_type!r} are not comparable with {op!r}",
            path,
        )
    lv = coerce_for_compare(left, left_type, path)
    rv = coerce_for_compare(right, right_type, path)
    if op == "=":
        return lv == rv
    if op in {"<>", "!="}:
        return lv != rv
    if op == "<":
        return lv < rv
    if op == "<=":
        return lv <= rv
    if op == ">":
        return lv > rv
    if op == ">=":
        return lv >= rv
    raise TypeMismatchError(f"unsupported comparison operator {op!r}", path)


def sort_key(value: Any, type_name: str) -> tuple[int, int, Any]:
    """Deterministic cross-plan ordering key; SQL NULLs sort last."""

    if value is None:
        return (1, 0, "")
    if type_name in NUMERIC_TYPES:
        return (0, 0, Decimal(str(value)))
    if type_name == "boolean":
        return (0, 1, 1 if value else 0)
    return (0, 2, str(value))


def canonical_value(value: Any) -> Any:
    if isinstance(value, float):
        if value == 0.0:
            return 0.0
        return value
    return value


def json_dumps_stable(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
