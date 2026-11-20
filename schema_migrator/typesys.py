from __future__ import annotations

import re
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

from .errors import error


_DECIMAL_RE = re.compile(r"^DECIMAL\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)$", re.IGNORECASE)
_SIMPLE_TYPES = {
    "INTEGER",
    "REAL",
    "TEXT",
    "BLOB",
    "NUMERIC",
    "BOOLEAN",
    "DATE",
    "DATETIME",
}


@dataclass(frozen=True)
class TypeInfo:
    canonical: str
    base: str
    precision: Optional[int] = None
    scale: Optional[int] = None

    @property
    def family(self) -> str:
        if self.base == "DECIMAL":
            return "decimal"
        if self.base in ("TEXT", "DATE", "DATETIME"):
            return "text"
        if self.base == "INTEGER":
            return "integer"
        if self.base == "REAL":
            return "real"
        if self.base == "NUMERIC":
            return "numeric"
        if self.base == "BOOLEAN":
            return "boolean"
        return "blob"

    @property
    def integer_digits(self) -> Optional[int]:
        if self.base == "DECIMAL" and self.precision is not None and self.scale is not None:
            return self.precision - self.scale
        if self.base == "INTEGER":
            return 19
        return None


def parse_type(value: Any, path: str = "type") -> TypeInfo:
    if not isinstance(value, str) or not value.strip():
        raise error("E_TYPE", "column type must be a non-empty string", path=path)
    raw = value.strip().upper().replace(" ", "")
    match = _DECIMAL_RE.match(raw)
    if match:
        precision = int(match.group(1))
        scale = int(match.group(2))
        if precision < 1 or precision > 38:
            raise error(
                "E_TYPE",
                "DECIMAL precision must be between 1 and 38",
                path=path,
                value=value,
            )
        if scale < 0 or scale > precision:
            raise error(
                "E_TYPE",
                "DECIMAL scale must satisfy 0 <= scale <= precision",
                path=path,
                value=value,
            )
        canonical = f"DECIMAL({precision},{scale})"
        return TypeInfo(canonical=canonical, base="DECIMAL", precision=precision, scale=scale)
    if raw not in _SIMPLE_TYPES:
        raise error(
            "E_TYPE",
            "unsupported type",
            path=path,
            value=value,
            allowed=sorted(_SIMPLE_TYPES) + ["DECIMAL(p,s)"],
        )
    return TypeInfo(canonical=raw, base=raw)


def default_compatible(type_info: TypeInfo, value: Any) -> Tuple[bool, str]:
    if value is None:
        return True, "NULL is compatible"
    family = type_info.family
    if family == "integer":
        return (isinstance(value, int) and not isinstance(value, bool), "INTEGER expects an integer")
    if family == "real":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(value, float):
            valid = valid and math.isfinite(value)
        return valid, "REAL expects a finite number"
    if family == "numeric":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(value, float):
            valid = valid and math.isfinite(value)
        return valid, "NUMERIC expects a finite number"
    if family == "decimal":
        if isinstance(value, bool):
            return False, "DECIMAL expects a decimal-compatible value"
        try:
            dec = Decimal(str(value))
            if not dec.is_finite():
                return False, "DECIMAL expects a finite value"
        except (InvalidOperation, ValueError):
            return False, "DECIMAL expects a decimal-compatible value"
        return True, "decimal-compatible"
    if family == "boolean":
        return (isinstance(value, bool), "BOOLEAN expects true or false")
    if family == "text":
        if not isinstance(value, str):
            return False, f"{type_info.base} expects a string"
        if type_info.base == "DATE":
            try:
                date.fromisoformat(value)
            except ValueError:
                return False, "DATE expects an ISO date string"
        if type_info.base == "DATETIME":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return False, "DATETIME expects an ISO datetime string"
        return True, "text-compatible"
    if family == "blob":
        return isinstance(value, str), "BLOB expects a string for deterministic local samples"
    return False, "unsupported type family"


def compare_types(old: TypeInfo, new: TypeInfo) -> Dict[str, Any]:
    """Classify whether an old column can be rewritten to a new type.

    SAFE means no value/range is lost for the declared model.
    PRECISION_LOSS means values can round or narrow at the representation level,
    but the column remains semantically usable.
    INCOMPATIBLE means common expressions cannot preserve meaning.
    """

    if old.canonical == new.canonical:
        return {"status": "SAFE", "reason": "type_unchanged"}

    if old.family == "text" and new.family == "text":
        if old.base == "DATETIME" and new.base == "DATE":
            return {"status": "PRECISION_LOSS", "reason": "datetime_to_date"}
        return {"status": "SAFE", "reason": "text_family_compatible"}

    if old.family == "integer" and new.family == "integer":
        return {"status": "SAFE", "reason": "integer_unchanged"}

    if old.family == "integer" and new.family == "real":
        return {"status": "PRECISION_LOSS", "reason": "integer_to_binary_real"}

    if old.family == "integer" and new.family == "numeric":
        return {"status": "PRECISION_LOSS", "reason": "integer_to_numeric_affinity"}

    if old.family == "integer" and new.family == "decimal":
        assert new.integer_digits is not None
        if new.integer_digits >= 19:
            return {"status": "SAFE", "reason": "decimal_holds_integer_range"}
        return {
            "status": "INCOMPATIBLE",
            "reason": "decimal_integer_range_too_small",
            "old_integer_digits": 19,
            "new_integer_digits": new.integer_digits,
        }

    if old.family == "decimal" and new.family == "decimal":
        assert old.integer_digits is not None and new.integer_digits is not None
        if new.integer_digits < old.integer_digits:
            return {
                "status": "INCOMPATIBLE",
                "reason": "decimal_integer_digits_shrink",
                "old_integer_digits": old.integer_digits,
                "new_integer_digits": new.integer_digits,
            }
        if new.scale is not None and old.scale is not None and new.scale < old.scale:
            return {
                "status": "PRECISION_LOSS",
                "reason": "decimal_scale_shrink",
                "old_scale": old.scale,
                "new_scale": new.scale,
            }
        return {"status": "SAFE", "reason": "decimal_widening"}

    if old.family == "decimal" and new.family in ("real", "numeric"):
        return {"status": "PRECISION_LOSS", "reason": "decimal_to_binary_numeric"}

    if old.family in ("real", "numeric") and new.family == "decimal":
        return {"status": "PRECISION_LOSS", "reason": "binary_numeric_to_decimal"}

    if old.family in ("real", "numeric", "decimal") and new.family == "integer":
        return {"status": "INCOMPATIBLE", "reason": "fractional_to_integer"}

    if old.family == "boolean" and new.family in ("integer", "boolean"):
        return {"status": "SAFE", "reason": "boolean_integer_compatible"}

    if old.family == "integer" and new.family == "boolean":
        return {"status": "INCOMPATIBLE", "reason": "integer_range_not_boolean"}

    if old.family == "blob" or new.family == "blob":
        return {"status": "INCOMPATIBLE", "reason": "blob_conversion"}

    if old.family in ("text", "integer", "real", "numeric", "decimal") and new.family in (
        "text",
        "integer",
        "real",
        "numeric",
        "decimal",
    ):
        return {"status": "INCOMPATIBLE", "reason": "cross_family_conversion"}

    return {"status": "INCOMPATIBLE", "reason": "unsupported_type_conversion"}


def sql_default_literal(type_info: TypeInfo, value: Any) -> str:
    if value is None:
        return "NULL"
    if type_info.family == "boolean":
        return "1" if bool(value) else "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def python_sql_value(type_info: TypeInfo, value: Any) -> Any:
    if value is None:
        return None
    if type_info.family == "boolean":
        return 1 if bool(value) else 0
    if type_info.family == "decimal":
        return float(Decimal(str(value)))
    if type_info.family == "blob":
        return str(value).encode("utf-8")
    return value


def type_from_sql(value: Optional[str], fallback: str = "TEXT") -> TypeInfo:
    if not value:
        return parse_type(fallback)
    return parse_type(value)

