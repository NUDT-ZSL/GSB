"""Observation-value normalization and order-independent comparability.

Supported aligned types: int, bool, enum, interval, mask.
A normalized value is always a dict with a ``type`` field and primitive members,
so it is JSON-serializable and hashable through :func:`value_key`.
"""

from __future__ import annotations

from . import codes

INT = "int"
BOOL = "bool"
ENUM = "enum"
INTERVAL = "interval"
MASK = "mask"
TYPES = (INT, BOOL, ENUM, INTERVAL, MASK)


class ValueError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _check_labels(labels: list[str], allowed: list[str] | None) -> None:
    if not labels or any(not isinstance(label, str) or not label for label in labels):
        raise ValueError(codes.E_VALUE_PARSE, "mask labels must be non-empty strings")
    if allowed is not None:
        for label in labels:
            if label not in allowed:
                raise ValueError(codes.E_ENUM_NOT_ALLOWED, f"label {label!r} outside whitelist")


def _validate_int(raw) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(codes.E_VALUE_PARSE, "int value must be a JSON integer")
    return raw


def normalize_v2(form: dict, spec_type: str, allowed: list[str] | None = None) -> dict:
    """Validate a v2 normalized form against the spec-declared type."""
    if not isinstance(form, dict) or form.get("type") != spec_type:
        raise ValueError(codes.E_TYPE_MISMATCH, f"expected type {spec_type!r}")
    if spec_type == INT:
        return {"type": INT, "value": _validate_int(form.get("value"))}
    if spec_type == BOOL:
        if not isinstance(form.get("value"), bool):
            raise ValueError(codes.E_VALUE_PARSE, "bool value must be true/false")
        return {"type": BOOL, "value": form["value"]}
    if spec_type == ENUM:
        value = form.get("value")
        if not isinstance(value, str):
            raise ValueError(codes.E_VALUE_PARSE, "enum value must be a string")
        _check_labels([value], allowed)
        return {"type": ENUM, "value": value}
    if spec_type == INTERVAL:
        low, high = _validate_int(form.get("low")), _validate_int(form.get("high"))
        if low > high:
            raise ValueError(codes.E_INTERVAL_REVERSED, f"interval {low} > {high}")
        return {"type": INTERVAL, "low": low, "high": high}
    if spec_type == MASK:
        labels = form.get("labels")
        if not isinstance(labels, list):
            raise ValueError(codes.E_VALUE_PARSE, "mask labels must be a list")
        labels = sorted(set(labels))
        _check_labels(labels, allowed)
        return {"type": MASK, "labels": labels}
    raise ValueError(codes.E_TYPE_MISMATCH, f"unknown spec type {spec_type!r}")


def parse_v1_string(text: str, spec_type: str, allowed: list[str] | None = None) -> dict:
    """Parse a v1 raw observation string into the same normalized shape as v2."""
    if not isinstance(text, str):
        raise ValueError(codes.E_VALUE_PARSE, "v1 observation must be a string")
    raw = text.strip()
    if spec_type == INT:
        if not raw or ("." in raw):
            raise ValueError(codes.E_VALUE_PARSE, f"not an integer: {text!r}")
        try:
            value = int(raw, 10)
        except ValueError as exc:
            raise ValueError(codes.E_VALUE_PARSE, f"not an integer: {text!r}") from exc
        return {"type": INT, "value": value}
    if spec_type == BOOL:
        low = raw.lower()
        if low in ("true", "1", "yes", "on"):
            return {"type": BOOL, "value": True}
        if low in ("false", "0", "no", "off"):
            return {"type": BOOL, "value": False}
        raise ValueError(codes.E_VALUE_PARSE, f"not a boolean: {text!r}")
    if spec_type == ENUM:
        _check_labels([raw], allowed)
        return {"type": ENUM, "value": raw}
    if spec_type == INTERVAL:
        body = raw
        if body.startswith("[") and body.endswith("]"):
            body = body[1:-1]
        parts = body.replace("..", ",").split(",")
        if len(parts) != 2:
            raise ValueError(codes.E_VALUE_PARSE, f"bad interval grammar: {text!r}")
        try:
            low, high = int(parts[0].strip()), int(parts[1].strip())
        except ValueError as exc:
            raise ValueError(codes.E_VALUE_PARSE, f"bad interval endpoints: {text!r}") from exc
        if low > high:
            raise ValueError(codes.E_INTERVAL_REVERSED, f"interval {low} > {high}")
        return {"type": INTERVAL, "low": low, "high": high}
    if spec_type == MASK:
        labels = sorted(set(part.strip() for part in raw.split("|") if part.strip()))
        _check_labels(labels, allowed)
        return {"type": MASK, "labels": labels}
    raise ValueError(codes.E_TYPE_MISMATCH, f"unknown spec type {spec_type!r}")


def value_key(normalized: dict):
    """Hashable canonical key for exact-equality checks."""
    if normalized["type"] == MASK:
        return (MASK, tuple(sorted(normalized["labels"])))
    if normalized["type"] == INTERVAL:
        return (INTERVAL, normalized["low"], normalized["high"])
    return (normalized["type"], normalized["value"])


def compare_values(left: dict, right: dict) -> dict:
    """Order-independent three-way relation between two normalized values.

    Returns relation code plus, for containment, which side is the container.
    The result does not depend on argument order: swapping inputs only swaps the
    ``container_side`` label, never the relation code.
    """
    if left["type"] != right["type"]:
        raise ValueError(codes.E_TYPE_MISMATCH, "cannot compare values of different types")
    if value_key(left) == value_key(right):
        return {"relation": codes.R_EQUAL, "container_side": None, "partial_overlap": False}

    if left["type"] == INTERVAL:
        l_contains_r = left["low"] <= right["low"] and right["high"] <= left["high"]
        r_contains_l = right["low"] <= left["low"] and left["high"] <= right["high"]
        if l_contains_r:
            return {"relation": codes.R_CONTAINS, "container_side": "left", "partial_overlap": False}
        if r_contains_l:
            return {"relation": codes.R_CONTAINS, "container_side": "right", "partial_overlap": False}
        disjoint = left["high"] < right["low"] or right["high"] < left["low"]
        return {
            "relation": codes.R_EXCLUSIVE,
            "container_side": None,
            "partial_overlap": not disjoint,
        }

    if left["type"] == MASK:
        lset, rset = set(left["labels"]), set(right["labels"])
        if lset >= rset:
            return {"relation": codes.R_CONTAINS, "container_side": "left", "partial_overlap": False}
        if rset >= lset:
            return {"relation": codes.R_CONTAINS, "container_side": "right", "partial_overlap": False}
        return {
            "relation": codes.R_EXCLUSIVE,
            "container_side": None,
            "partial_overlap": bool(lset & rset),
        }

    return {"relation": codes.R_EXCLUSIVE, "container_side": None, "partial_overlap": False}


def baseline_upper_crossed(normalized: dict, upper) -> bool:
    """Determine whether a normalized value crosses the baseline upper bound."""
    if upper is None:
        return False
    kind = normalized["type"]
    if kind == INT:
        return normalized["value"] > int(upper)
    if kind == BOOL:
        expected = upper if isinstance(upper, bool) else str(upper).strip().lower() in ("true", "1", "yes", "on")
        return normalized["value"] != expected
    if kind == ENUM:
        return normalized["value"] != upper
    if kind == INTERVAL:
        return normalized["high"] > int(upper)
    if kind == MASK:
        return normalized["labels"] != sorted(set(upper if isinstance(upper, list) else [upper]))
    return False
