from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

from .errors import E_INPUT_JSON, E_INPUT_SCHEMA, E_STATE_CORRUPT, fail


def _reject_constant(token: str) -> None:
    fail(E_INPUT_JSON, "JSON contains a non-finite numeric constant", token=token)


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(E_INPUT_JSON, "JSON object contains a duplicate key", key=key)
        result[key] = value
    return result


def load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_float=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_pairs_without_duplicates,
            )
    except OSError as exc:
        fail(E_INPUT_IO, "Unable to read JSON file", path=path, detail=str(exc))
    except json.JSONDecodeError as exc:
        fail(
            E_INPUT_JSON,
            "Malformed JSON",
            path=path,
            line=exc.lineno,
            column=exc.colno,
            detail=exc.msg,
        )


def parse_json_text(text: str, source: str = "<memory>") -> Any:
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_pairs_without_duplicates,
        )
    except json.JSONDecodeError as exc:
        fail(
            E_INPUT_JSON,
            "Malformed JSON",
            path=source,
            line=exc.lineno,
            column=exc.colno,
            detail=exc.msg,
        )


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        fail(E_INPUT_JSON, "Non-finite Decimal cannot be canonicalized")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def canonical_json(value: Any) -> str:
    """Canonical JSON preserving Decimal values as exact JSON numbers."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            fail(E_INPUT_JSON, "Non-finite float cannot be canonicalized")
        return repr(value)
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        parts: list[str] = []
        for key in sorted(value):
            if not isinstance(key, str):
                fail(E_INPUT_JSON, "Canonical JSON object keys must be strings")
            parts.append(f"{json.dumps(key, ensure_ascii=False)}:{canonical_json(value[key])}")
        return "{" + ",".join(parts) + "}"
    fail(E_INPUT_JSON, "Unsupported value in canonical JSON", type=type(value).__name__)


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def digest_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        fail(E_INPUT_IO, "Unable to hash input file", path=path, detail=str(exc))
    return "sha256:" + digest.hexdigest()


def decimal_text(value: Decimal | int | str | float) -> str:
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        fail(E_INPUT_SCHEMA, "Boolean is not a numeric value")
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            fail(E_INPUT_SCHEMA, "Invalid decimal value", value=str(value))
    if not parsed.is_finite():
        fail(E_INPUT_SCHEMA, "Numeric value must be finite", value=str(value))
    return _canonical_decimal(parsed)


def decimal_from_text(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        fail(E_STATE_CORRUPT, "Stored decimal is invalid", value=text)
    if not value.is_finite():
        fail(E_STATE_CORRUPT, "Stored decimal is not finite", value=text)
    return value


MEAN_PRECISION = Decimal("0.000000000001")


def quantize_mean(value: Decimal) -> Decimal:
    return value.quantize(MEAN_PRECISION, rounding=ROUND_HALF_EVEN)


def json_safe(value: Any) -> Any:
    """Convert Decimals to exact strings for human-facing JSON."""
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def dump_stable(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, indent=2)

