"""Readers and writers for v1, v2 and v3 sampling exchange formats."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import SamplingConfig
from .utils import pretty_json

V1_FIELDS = frozenset({"version", "temperature", "top_k"})
V2_FIELDS = frozenset(
    {
        "version",
        "temperature",
        "top_k",
        "top_p",
        "repetition_penalty",
        "vocab_version",
    }
)
V3_FIELDS = V2_FIELDS
SUPPORTED_VERSIONS = frozenset({"v1", "v2", "v3"})

EXPECTED_UNITS = {
    "temperature": "ratio",
    "top_k": "tokens",
    "top_p": "probability",
    "repetition_penalty": "multiplier",
}

V3_HEADER = "!sampling-config"


def read_config(text: str, *, request_id: str | None = None) -> SamplingConfig:
    """Read a sampling config by header/version and enforce its field set."""

    if not isinstance(text, str) or not text.strip():
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "sampling configuration is empty",
            request_id=request_id,
            field="header",
        )

    leading = text.lstrip()
    if leading.startswith("{"):
        return _read_json_config(text, request_id=request_id)
    if leading.startswith("!"):
        return _read_v3_config(text, request_id=request_id)
    raise BusinessError(
        ErrorCode.FORMAT_CORRUPT,
        "unrecognized configuration header; expected JSON object or !sampling-config v3",
        request_id=request_id,
        field="header",
        details={"first_line": text.splitlines()[0][:120]},
    )


def read_config_file(path: Path, *, request_id: str | None = None) -> SamplingConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"unable to read configuration: {exc}",
            request_id=request_id,
            field="header",
        ) from exc
    return read_config(text, request_id=request_id)


def detect_config_version(text: str) -> str:
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                "configuration header is truncated or not valid JSON",
                field="header",
                details={"line": exc.lineno, "column": exc.colno},
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                "configuration version header is missing",
                field="version",
            )
        return payload["version"]
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    parts = first_line.split()
    if len(parts) >= 2 and parts[0] == V3_HEADER:
        return parts[1]
    raise BusinessError(
        ErrorCode.FORMAT_CORRUPT,
        "configuration header is truncated",
        field="header",
        details={"first_line": first_line[:120]},
    )


def serialize_v1(config: SamplingConfig) -> str:
    top_k = _integer_json_value(config.top_k)
    return pretty_json(
        {
            "version": "v1",
            "temperature": config.temperature_literal,
            "top_k": top_k,
        }
    )


def serialize_v2(config: SamplingConfig) -> str:
    return pretty_json(
        {
            "version": "v2",
            "temperature": {
                "value": config.temperature_literal,
                "unit": "ratio",
            },
            "top_k": {
                "value": _integer_json_value(config.top_k),
                "unit": "tokens",
            },
            "top_p": {
                "value": _literal_value(config, "top_p"),
                "unit": "probability",
            },
            "repetition_penalty": {
                "value": _literal_value(config, "repetition_penalty"),
                "unit": "multiplier",
            },
            "vocab_version": config.vocab_version,
        }
    )


def serialize_v3(config: SamplingConfig) -> str:
    lines = [
        f"{V3_HEADER} v3",
        f"temperature = {config.temperature_literal} ratio",
        f"top_k = {_integer_text(config.top_k)} tokens",
        f"top_p = {_literal_value(config, 'top_p')} probability",
        f"repetition_penalty = {_literal_value(config, 'repetition_penalty')} multiplier",
        f"vocab_version = {config.vocab_version}",
    ]
    return "\n".join(lines) + "\n"


def _read_json_config(text: str, *, request_id: str | None) -> SamplingConfig:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "configuration JSON is truncated or malformed",
            request_id=request_id,
            field="header",
            details={"line": exc.lineno, "column": exc.colno, "reason": exc.msg},
        ) from exc

    if not isinstance(payload, dict):
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "configuration header must be a JSON object",
            request_id=request_id,
            field="header",
        )

    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "required field 'version' is missing",
            request_id=request_id,
            field="version",
        )
    if version not in SUPPORTED_VERSIONS:
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            f"unsupported sampling configuration version '{version}'",
            request_id=request_id,
            field="version",
            version=version,
            original_value=version,
        )
    if version == "v3":
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "v3 uses a line-oriented exchange header, not JSON",
            request_id=request_id,
            field="header",
        )
    if version == "v1":
        return _read_v1_payload(payload, request_id=request_id)
    return _read_v2_payload(payload, request_id=request_id)


def _assert_field_set(
    payload: dict[str, Any],
    allowed: frozenset[str],
    *,
    version: str,
    request_id: str | None,
) -> None:
    for key in sorted(payload):
        if key not in allowed:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"field '{key}' is not part of the {version} field set",
                request_id=request_id,
                field=key,
            )


def _read_v1_payload(
    payload: dict[str, Any], *, request_id: str | None
) -> SamplingConfig:
    _assert_field_set(payload, V1_FIELDS, version="v1", request_id=request_id)
    _require(payload, "temperature", request_id=request_id)
    _require(payload, "top_k", request_id=request_id)

    temperature, temperature_literal = _parse_number(
        payload["temperature"],
        "temperature",
        request_id=request_id,
    )
    top_k, top_k_literal = _parse_number(
        payload["top_k"],
        "top_k",
        request_id=request_id,
    )
    return SamplingConfig(
        version="v1",
        temperature=temperature,
        top_k=top_k,
        temperature_literal=temperature_literal,
        units={
            "temperature": "implicit:ratio",
            "top_k": "implicit:tokens",
        },
        literal_values={
            "temperature": temperature_literal,
            "top_k": top_k_literal,
        },
        source_format="json-flat",
    )


def _read_v2_payload(
    payload: dict[str, Any], *, request_id: str | None
) -> SamplingConfig:
    _assert_field_set(payload, V2_FIELDS, version="v2", request_id=request_id)
    for field_name in (
        "temperature",
        "top_k",
        "top_p",
        "repetition_penalty",
        "vocab_version",
    ):
        _require(payload, field_name, request_id=request_id)

    units: dict[str, str] = {}
    literals: dict[str, str] = {}
    parsed: dict[str, float] = {}
    for field_name in ("temperature", "top_k", "top_p", "repetition_penalty"):
        raw = _extract_v2_value(
            payload,
            field_name,
            request_id=request_id,
            units=units,
        )
        parsed[field_name], literals[field_name] = _parse_number(
            raw,
            field_name,
            request_id=request_id,
        )

    vocab_version = _extract_v2_vocab_version(payload, request_id=request_id)
    units["vocab_version"] = "vocab-version"
    literals["vocab_version"] = vocab_version
    return SamplingConfig(
        version="v2",
        temperature=parsed["temperature"],
        top_k=parsed["top_k"],
        temperature_literal=literals["temperature"],
        top_p=parsed["top_p"],
        repetition_penalty=parsed["repetition_penalty"],
        vocab_version=vocab_version,
        units=units,
        literal_values=literals,
        source_format="json-unit-objects",
    )


def _extract_v2_value(
    payload: dict[str, Any],
    field_name: str,
    *,
    request_id: str | None,
    units: dict[str, str],
) -> Any:
    raw = payload[field_name]
    expected_unit = EXPECTED_UNITS[field_name]
    if isinstance(raw, dict):
        if "value" not in raw:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"required field '{field_name}.value' is missing",
                request_id=request_id,
                field=f"{field_name}.value",
            )
        if "unit" not in raw:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"required field '{field_name}.unit' is missing",
                request_id=request_id,
                field=f"{field_name}.unit",
            )
        if raw["unit"] != expected_unit:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"field '{field_name}' expects unit '{expected_unit}'",
                request_id=request_id,
                field=f"{field_name}.unit",
                original_value=raw["unit"],
                details={"expected": expected_unit},
            )
        units[field_name] = expected_unit
        return raw["value"]
    units[field_name] = f"implicit:{expected_unit}"
    return raw


def _extract_v2_vocab_version(
    payload: dict[str, Any], *, request_id: str | None
) -> str:
    raw = payload["vocab_version"]
    if isinstance(raw, dict):
        if "value" not in raw:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                "required field 'vocab_version.value' is missing",
                request_id=request_id,
                field="vocab_version.value",
            )
        unit = raw.get("unit")
        if unit is not None and unit != "vocab-version":
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                "field 'vocab_version' expects unit 'vocab-version'",
                request_id=request_id,
                field="vocab_version.unit",
                original_value=unit,
            )
        raw = raw["value"]
    if not isinstance(raw, str) or not raw.strip():
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "field 'vocab_version' must be a non-empty string",
            request_id=request_id,
            field="vocab_version",
            original_value=raw,
        )
    return raw.strip()


def _read_v3_config(text: str, *, request_id: str | None) -> SamplingConfig:
    lines = text.splitlines()
    first_line = lines[0].strip() if lines else ""
    header_parts = first_line.split()
    if not header_parts or header_parts[0] != V3_HEADER:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "v3 configuration header is truncated",
            request_id=request_id,
            field="header",
            details={"first_line": first_line[:120]},
        )
    if len(header_parts) < 2:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "required version header is missing after '!sampling-config'",
            request_id=request_id,
            field="version",
        )
    if len(header_parts) > 2:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "v3 header contains unexpected tokens",
            request_id=request_id,
            field="header",
            details={"first_line": first_line[:120]},
        )
    version = header_parts[1]
    if version not in SUPPORTED_VERSIONS:
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            f"unsupported sampling configuration version '{version}'",
            request_id=request_id,
            field="version",
            version=version,
            original_value=version,
        )
    if version != "v3":
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"version '{version}' must use JSON, not the v3 line format",
            request_id=request_id,
            field="header",
        )

    entries: dict[str, tuple[str, str | None]] = {}
    for line_number, original_line in enumerate(lines[1:], start=2):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"line {line_number} is not a key-value entry",
                request_id=request_id,
                field=f"line:{line_number}",
                details={"line": original_line[:160]},
            )
        key, raw_right = line.split("=", 1)
        key = key.strip()
        if not key:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"line {line_number} has an empty field name",
                request_id=request_id,
                field=f"line:{line_number}",
            )
        if key in entries:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"duplicate field '{key}'",
                request_id=request_id,
                field=key,
            )
        parts = raw_right.strip().split()
        if not parts:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"required value for field '{key}' is missing",
                request_id=request_id,
                field=key,
            )
        if key == "vocab_version":
            if len(parts) > 2:
                raise BusinessError(
                    ErrorCode.FORMAT_CORRUPT,
                    "vocab_version accepts at most one optional unit",
                    request_id=request_id,
                    field=key,
                )
            if len(parts) == 2 and parts[1] != "vocab-version":
                raise BusinessError(
                    ErrorCode.FORMAT_CORRUPT,
                    "vocab_version expects unit 'vocab-version' when supplied",
                    request_id=request_id,
                    field=f"{key}.unit",
                    original_value=parts[1],
                )
            entries[key] = (parts[0], parts[1] if len(parts) == 2 else None)
            continue
        if len(parts) != 2:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"field '{key}' must be written as '<value> <unit>'",
                request_id=request_id,
                field=key,
            )
        entries[key] = (parts[0], parts[1])

    required = ("temperature", "top_k", "top_p", "repetition_penalty", "vocab_version")
    for field_name in required:
        if field_name not in entries:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"required field '{field_name}' is missing",
                request_id=request_id,
                field=field_name,
            )
    for key in sorted(entries):
        if key not in V3_FIELDS:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"field '{key}' is not part of the v3 field set",
                request_id=request_id,
                field=key,
            )

    units: dict[str, str] = {}
    literals: dict[str, str] = {}
    parsed: dict[str, float] = {}
    for field_name in ("temperature", "top_k", "top_p", "repetition_penalty"):
        literal, unit = entries[field_name]
        assert unit is not None
        expected_unit = EXPECTED_UNITS[field_name]
        if unit != expected_unit:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"field '{field_name}' expects unit '{expected_unit}'",
                request_id=request_id,
                field=f"{field_name}.unit",
                original_value=unit,
                details={"expected": expected_unit},
            )
        parsed[field_name], literals[field_name] = _parse_number(
            literal,
            field_name,
            request_id=request_id,
        )
        units[field_name] = unit

    vocab_version = entries["vocab_version"][0]
    if not vocab_version.strip():
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "field 'vocab_version' must be a non-empty string",
            request_id=request_id,
            field="vocab_version",
        )
    units["vocab_version"] = entries["vocab_version"][1] or "vocab-version"
    literals["vocab_version"] = vocab_version
    return SamplingConfig(
        version="v3",
        temperature=parsed["temperature"],
        top_k=parsed["top_k"],
        temperature_literal=literals["temperature"],
        top_p=parsed["top_p"],
        repetition_penalty=parsed["repetition_penalty"],
        vocab_version=vocab_version,
        units=units,
        literal_values=literals,
        source_format="line-kv",
    )


def _parse_number(
    raw: Any,
    field_name: str,
    *,
    request_id: str | None,
) -> tuple[float, str]:
    if isinstance(raw, bool) or raw is None:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"field '{field_name}' must contain a numeric value",
            request_id=request_id,
            field=field_name,
            original_value=raw,
        )
    if isinstance(raw, (int, float)):
        literal = str(raw)
        value = float(raw)
    elif isinstance(raw, str):
        literal = raw
        normalized = raw.strip().replace("_", "")
        if not normalized:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"field '{field_name}' contains an empty numeric literal",
                request_id=request_id,
                field=field_name,
                original_value=raw,
                details={"literal": raw},
            )
        try:
            value = float(normalized)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"field '{field_name}' contains an unparseable numeric literal",
                request_id=request_id,
                field=field_name,
                original_value=raw,
                details={"literal": raw, "tolerance_rule": "strip/underscores/scientific"},
            ) from exc
    else:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"field '{field_name}' must contain a numeric value or text literal",
            request_id=request_id,
            field=field_name,
            original_value=raw,
        )

    if not math.isfinite(value):
        raise BusinessError(
            ErrorCode.PARAM_RANGE,
            f"field '{field_name}' must be finite",
            request_id=request_id,
            field=field_name,
            original_value=literal,
        )
    return value, literal


def _require(
    payload: dict[str, Any], field_name: str, *, request_id: str | None
) -> None:
    if field_name not in payload:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"required field '{field_name}' is missing",
            request_id=request_id,
            field=field_name,
        )


def _integer_json_value(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _integer_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _literal_value(config: SamplingConfig, field_name: str) -> str:
    if field_name in config.literal_values:
        return config.literal_values[field_name]
    value = getattr(config, field_name)
    if value is None:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"config version {config.version} has no value for '{field_name}'",
            field=field_name,
        )
    return str(value)
