from __future__ import annotations

import math
from typing import Any, Iterable

from .errors import LedgerError


def require_dict(value: Any, *, stage: str, what: str = "document") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError(
            "E_FORMAT",
            f"{what} must be a JSON object",
            stage=stage,
            details={"actual_type": type(value).__name__},
        )
    return value


def require_fields(
    obj: dict[str, Any],
    fields: Iterable[str],
    *,
    stage: str,
    where: str = "document",
) -> None:
    for field in fields:
        if field not in obj:
            raise LedgerError(
                "E_FIELD_MISSING",
                f"missing required field {field!r} in {where}",
                stage=stage,
                details={"field": field, "where": where},
            )


def parse_int(value: Any, *, field: str, stage: str, where: str = "document") -> int:
    if isinstance(value, bool):
        raise LedgerError(
            "E_PARSE",
            f"field {field!r} must be an integer, got boolean",
            stage=stage,
            details={"field": field, "where": where, "value": str(value)},
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError as exc:
                raise LedgerError(
                    "E_PARSE",
                    f"field {field!r} is not an integer",
                    stage=stage,
                    details={"field": field, "where": where, "value": value},
                ) from exc
            if math.isfinite(number) and number.is_integer():
                return int(number)
    raise LedgerError(
        "E_PARSE",
        f"field {field!r} is not an integer",
        stage=stage,
        details={"field": field, "where": where, "value": repr(value)},
    )


def parse_float(value: Any, *, field: str, stage: str, where: str = "document") -> float:
    if isinstance(value, bool):
        raise LedgerError(
            "E_PARSE",
            f"field {field!r} must be numeric, got boolean",
            stage=stage,
            details={"field": field, "where": where, "value": str(value)},
        )
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError as exc:
            raise LedgerError(
                "E_PARSE",
                f"field {field!r} is not a float",
                stage=stage,
                details={"field": field, "where": where, "value": value},
            ) from exc
    else:
        raise LedgerError(
            "E_PARSE",
            f"field {field!r} is not numeric",
            stage=stage,
            details={"field": field, "where": where, "value": repr(value)},
        )
    if not math.isfinite(result):
        raise LedgerError(
            "E_PARSE",
            f"field {field!r} must be finite",
            stage=stage,
            details={"field": field, "where": where, "value": repr(value)},
        )
    return result


def parse_int_array(
    value: Any,
    *,
    field: str,
    stage: str,
    where: str = "document",
) -> list[int]:
    if not isinstance(value, list):
        raise LedgerError(
            "E_PARSE",
            f"field {field!r} must be an array",
            stage=stage,
            details={"field": field, "where": where},
        )
    return [parse_int(item, field=field, stage=stage, where=where) for item in value]


def parse_float_array(
    value: Any,
    *,
    field: str,
    stage: str,
    where: str = "document",
) -> list[float]:
    if not isinstance(value, list):
        raise LedgerError(
            "E_PARSE",
            f"field {field!r} must be an array",
            stage=stage,
            details={"field": field, "where": where},
        )
    return [parse_float(item, field=field, stage=stage, where=where) for item in value]


def read_version(obj: dict[str, Any] | Any, *, stage: str) -> int:
    obj = require_dict(obj, stage=stage, what="ledger document")
    if "version" not in obj:
        raise LedgerError(
            "E_FIELD_MISSING",
            "missing required field 'version'",
            stage=stage,
            details={"field": "version", "where": "top-level"},
        )
    try:
        version = parse_int(obj["version"], field="version", stage=stage)
    except LedgerError as exc:
        raise LedgerError(
            "E_VERSION_UNSUPPORTED",
            "ledger version is not a supported integer",
            stage=stage,
            details={"version": obj.get("version")},
        ) from exc
    if version not in (1, 2, 3):
        raise LedgerError(
            "E_VERSION_UNSUPPORTED",
            f"unsupported ledger version {version}",
            stage=stage,
            details={"version": version, "supported": [1, 2, 3]},
        )
    return version
