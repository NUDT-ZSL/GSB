from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_expected_digest(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if value.startswith("sha256:"):
        value = value[7:]
    return value.lower()


def first_difference(
    left: Any,
    right: Any,
    path: str = "",
) -> dict[str, Any] | None:
    """Return the first deterministic divergence between two JSON values."""

    if type(left) is not type(right):
        return {
            "field_path": path,
            "left_present": True,
            "left_value": left,
            "right_present": True,
            "right_value": right,
        }

    if isinstance(left, dict):
        left_keys = sorted(left)
        right_keys = sorted(right)
        for key in left_keys:
            child_path = f"{path}.{key}" if path else key
            if key not in right:
                return {
                    "field_path": child_path,
                    "left_present": True,
                    "left_value": left[key],
                    "right_present": False,
                    "right_value": None,
                }
            difference = first_difference(left[key], right[key], child_path)
            if difference is not None:
                return difference
        for key in right_keys:
            if key not in left:
                child_path = f"{path}.{key}" if path else key
                return {
                    "field_path": child_path,
                    "left_present": False,
                    "left_value": None,
                    "right_present": True,
                    "right_value": right[key],
                }
        return None

    if isinstance(left, list):
        if len(left) != len(right):
            return {
                "field_path": f"{path}.length" if path else "length",
                "left_present": True,
                "left_value": len(left),
                "right_present": True,
                "right_value": len(right),
            }
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            difference = first_difference(
                left_item,
                right_item,
                f"{path}[{index}]",
            )
            if difference is not None:
                return difference
        return None

    if left != right:
        return {
            "field_path": path,
            "left_present": True,
            "left_value": left,
            "right_present": True,
            "right_value": right,
        }
    return None

