"""Version-dispatched loaders for v1, v2 and v3 position configs."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .errors import E_FORMAT_CORRUPT, E_VERSION_UNSUPPORTED, RopeError
from .models import (
    CONFIG_VERSION_V1,
    CONFIG_VERSION_V2,
    CONFIG_VERSION_V3,
    FREQUENCY_METHODS,
    FrequencyAdjustment,
    ParsedConfig,
    SUPPORTED_CONFIG_VERSIONS,
    V2_VARIANTS,
    V3_VARIANTS,
)
from .util import sha256_bytes


_VERSION_HEADER = re.compile(r"^v(\d+)$", re.IGNORECASE)
_V2_REQUIRED = ("base_theta", "pos_scale", "block_size")
_V2_ALLOWED = set(_V2_REQUIRED) | {"variant"}
_V3_REQUIRED = (
    "base_theta",
    "pos_scale",
    "block_size",
    "variant",
    "frequency_adjustment",
    "per_head_scale",
)


def _corrupt(
    message: str,
    *,
    source_line: int | None = None,
    field_position: str | None = None,
    field: str | None = None,
    **extra: Any,
) -> RopeError:
    details: dict[str, Any] = dict(extra)
    if source_line is not None:
        details["source_line"] = source_line
    if field_position is not None:
        details["field_position"] = field_position
    if field is not None:
        details["field"] = field
    return RopeError(E_FORMAT_CORRUPT, message, details=details)


def _check_finite(value: float, *, field: str, location: str) -> float:
    if not math.isfinite(value):
        raise _corrupt(
            "numeric field must be finite",
            field=field,
            field_position=location,
        )
    return float(value)


def _parse_float(text: str, *, field: str, location: str, source_line: int) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise _corrupt(
            "numeric field is not parseable",
            source_line=source_line,
            field=field,
            field_position=location,
        ) from exc
    return _check_finite(value, field=field, location=location)


def _parse_positive_float(
    value: Any, *, field: str, location: str, source_line: int
) -> float:
    if isinstance(value, bool):
        raise _corrupt(
            "numeric field cannot be boolean",
            source_line=source_line,
            field=field,
            field_position=location,
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise _corrupt(
            "numeric field is not parseable",
            source_line=source_line,
            field=field,
            field_position=location,
        ) from exc
    _check_finite(parsed, field=field, location=location)
    if parsed <= 0:
        raise _corrupt(
            "numeric field must be greater than zero",
            source_line=source_line,
            field=field,
            field_position=location,
        )
    return parsed


def _parse_positive_int(
    value: Any, *, field: str, location: str, source_line: int
) -> int:
    if isinstance(value, bool):
        raise _corrupt(
            "integer field cannot be boolean",
            source_line=source_line,
            field=field,
            field_position=location,
        )
    try:
        if isinstance(value, str):
            if not re.fullmatch(r"[+-]?\d+", value.strip()):
                raise ValueError
            parsed = int(value.strip())
        else:
            numeric = float(value)
            if not numeric.is_integer():
                raise ValueError
            parsed = int(numeric)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _corrupt(
            "integer field is not parseable",
            source_line=source_line,
            field=field,
            field_position=location,
        ) from exc
    if parsed <= 0:
        raise _corrupt(
            "integer field must be greater than zero",
            source_line=source_line,
            field=field,
            field_position=location,
        )
    return parsed


def _split_nonblank_lines(text: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            result.append((line_number, line))
    return result


def _detect_version(
    lines: list[tuple[int, str]],
) -> tuple[str | None, int, int | None]:
    first_line_number, first_text = lines[0]
    header = _VERSION_HEADER.fullmatch(first_text.strip())
    if header:
        version = f"v{int(header.group(1))}"
        if version not in SUPPORTED_CONFIG_VERSIONS:
            raise RopeError(
                E_VERSION_UNSUPPORTED,
                "configuration version is not supported",
                details={
                    "source_line": first_line_number,
                    "source_version": version,
                    "supported_versions": list(SUPPORTED_CONFIG_VERSIONS),
                },
            )
        return version, first_line_number, 1

    stripped = first_text.strip()
    if stripped.startswith("{"):
        try:
            document = json.loads("\n".join(text for _, text in lines))
        except json.JSONDecodeError:
            document = None
        if isinstance(document, dict):
            if "frequency_adjustment" in document or "per_head_scale" in document:
                return CONFIG_VERSION_V3, first_line_number, 0
            if "base_theta" in document:
                raise RopeError(
                    E_VERSION_UNSUPPORTED,
                    "JSON config lacks v3 field set and no version header was supplied",
                    details={"source_line": first_line_number},
                )
    if "=" in stripped:
        return CONFIG_VERSION_V2, first_line_number, 0
    if "|" in stripped:
        return CONFIG_VERSION_V1, first_line_number, 0
    raise RopeError(
        E_VERSION_UNSUPPORTED,
        "cannot dispatch configuration version from header or field set",
        details={"source_line": first_line_number},
    )


def _parse_v1(text: str, source_sha: str, header_found: bool) -> ParsedConfig:
    lines = _split_nonblank_lines(text)
    header_offset = 1 if header_found else 0
    if len(lines) != 1 + header_offset:
        line = lines[1 + header_offset][0] if len(lines) > 1 + header_offset else None
        raise _corrupt(
            "v1 requires exactly one data line",
            source_line=line,
            field_position="line:v1_data",
            expected_data_lines=1,
            actual_data_lines=max(0, len(lines) - header_offset),
        )
    source_line, data = lines[header_offset]
    fields = data.split("|")
    if len(fields) != 3:
        raise _corrupt(
            "v1 field count must be exactly 3",
            source_line=source_line,
            field_position=f"line:{source_line},field_count",
            expected=3,
            actual=len(fields),
        )
    locations = {
        "base_theta": f"line:{source_line},field:1",
        "pos_scale": f"line:{source_line},field:2",
        "block_size": f"line:{source_line},field:3",
    }
    base_theta = _parse_positive_float(
        fields[0].strip(),
        field="base_theta",
        location=locations["base_theta"],
        source_line=source_line,
    )
    pos_scale = _parse_positive_float(
        fields[1].strip(),
        field="pos_scale",
        location=locations["pos_scale"],
        source_line=source_line,
    )
    block_size = _parse_positive_int(
        fields[2].strip(),
        field="block_size",
        location=locations["block_size"],
        source_line=source_line,
    )
    return ParsedConfig(
        source_version=CONFIG_VERSION_V1,
        source_line=source_line,
        source_span=f"line:{lines[0][0]}-line:{source_line}",
        source_text_sha256=source_sha,
        base_theta=base_theta,
        pos_scale=pos_scale,
        block_size=block_size,
        variant=None,
        frequency_adjustment=None,
        per_head_scale=None,
        explicit_fields=("base_theta", "pos_scale", "block_size"),
        field_locations=locations,
    )


def _parse_v2(text: str, source_sha: str, header_found: bool) -> ParsedConfig:
    lines = _split_nonblank_lines(text)
    header_offset = 1 if header_found else 0
    if len(lines) != 1 + header_offset:
        line = lines[1 + header_offset][0] if len(lines) > 1 + header_offset else None
        raise _corrupt(
            "v2 requires exactly one data line",
            source_line=line,
            field_position="line:v2_data",
            expected_data_lines=1,
            actual_data_lines=max(0, len(lines) - header_offset),
        )
    source_line, data = lines[header_offset]
    segments = data.split("|")
    if not 3 <= len(segments) <= 4:
        raise _corrupt(
            "v2 field count must be 3 or 4",
            source_line=source_line,
            field_position=f"line:{source_line},field_count",
            expected="3 or 4",
            actual=len(segments),
        )
    values: dict[str, str] = {}
    locations: dict[str, str] = {}
    for index, segment in enumerate(segments, start=1):
        if segment.count("=") != 1:
            raise _corrupt(
                "v2 field must contain exactly one '='",
                source_line=source_line,
                field_position=f"line:{source_line},field:{index}",
            )
        name, raw = segment.split("=", 1)
        name = name.strip()
        raw = raw.strip()
        if not name:
            raise _corrupt(
                "v2 field name is empty",
                source_line=source_line,
                field_position=f"line:{source_line},field:{index}",
            )
        if name not in _V2_ALLOWED:
            raise _corrupt(
                "v2 field name is unknown",
                source_line=source_line,
                field_position=f"line:{source_line},field:{index}",
                field=name,
            )
        if name in values:
            raise _corrupt(
                "v2 field is duplicated",
                source_line=source_line,
                field_position=f"line:{source_line},field:{index}",
                field=name,
            )
        values[name] = raw
        locations[name] = f"line:{source_line},field:{index}"
    for name in _V2_REQUIRED:
        if name not in values:
            raise _corrupt(
                "v2 required field is missing",
                source_line=source_line,
                field_position=f"line:{source_line},field:{name}",
                field=name,
            )
    base_theta = _parse_positive_float(
        values["base_theta"],
        field="base_theta",
        location=locations["base_theta"],
        source_line=source_line,
    )
    pos_scale = _parse_positive_float(
        values["pos_scale"],
        field="pos_scale",
        location=locations["pos_scale"],
        source_line=source_line,
    )
    block_size = _parse_positive_int(
        values["block_size"],
        field="block_size",
        location=locations["block_size"],
        source_line=source_line,
    )
    variant: str | None = None
    if "variant" in values:
        variant = values["variant"].strip().lower()
        if variant not in V2_VARIANTS:
            raise _corrupt(
                "v2 variant is not supported",
                source_line=source_line,
                field="variant",
                field_position=locations["variant"],
                supported=list(V2_VARIANTS),
            )
    explicit = tuple(name for name in ("base_theta", "pos_scale", "block_size", "variant") if name in values)
    return ParsedConfig(
        source_version=CONFIG_VERSION_V2,
        source_line=source_line,
        source_span=f"line:{lines[0][0]}-line:{source_line}",
        source_text_sha256=source_sha,
        base_theta=base_theta,
        pos_scale=pos_scale,
        block_size=block_size,
        variant=variant,
        frequency_adjustment=None,
        per_head_scale=None,
        explicit_fields=explicit,
        field_locations=locations,
    )


def _json_location(source_line: int, field: str) -> str:
    return f"line:{source_line},json:{field}"


def _parse_frequency_adjustment(
    value: Any, *, source_line: int
) -> FrequencyAdjustment:
    location = _json_location(source_line, "frequency_adjustment")
    if not isinstance(value, dict):
        raise _corrupt(
            "frequency_adjustment must be an object",
            source_line=source_line,
            field="frequency_adjustment",
            field_position=location,
        )
    allowed = {"method", "factor", "block_tokens"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _corrupt(
            "frequency_adjustment has unknown fields",
            source_line=source_line,
            field=unknown[0],
            field_position=_json_location(source_line, f"frequency_adjustment.{unknown[0]}"),
        )
    if "method" not in value:
        raise _corrupt(
            "frequency_adjustment.method is required",
            source_line=source_line,
            field="frequency_adjustment.method",
            field_position=_json_location(source_line, "frequency_adjustment.method"),
        )
    method = value["method"]
    if not isinstance(method, str) or method not in FREQUENCY_METHODS:
        raise _corrupt(
            "frequency_adjustment.method is not supported",
            source_line=source_line,
            field="frequency_adjustment.method",
            field_position=_json_location(source_line, "frequency_adjustment.method"),
            supported=list(FREQUENCY_METHODS),
        )
    factor = _parse_positive_float(
        value.get("factor", 1.0),
        field="frequency_adjustment.factor",
        location=_json_location(source_line, "frequency_adjustment.factor"),
        source_line=source_line,
    )
    block_tokens: int | None = None
    if method == "blockwise":
        if "block_tokens" not in value:
            raise _corrupt(
                "blockwise frequency adjustment requires block_tokens",
                source_line=source_line,
                field="frequency_adjustment.block_tokens",
                field_position=_json_location(
                    source_line, "frequency_adjustment.block_tokens"
                ),
            )
        block_tokens = _parse_positive_int(
            value["block_tokens"],
            field="frequency_adjustment.block_tokens",
            location=_json_location(source_line, "frequency_adjustment.block_tokens"),
            source_line=source_line,
        )
    elif "block_tokens" in value:
        block_tokens = _parse_positive_int(
            value["block_tokens"],
            field="frequency_adjustment.block_tokens",
            location=_json_location(source_line, "frequency_adjustment.block_tokens"),
            source_line=source_line,
        )
    return FrequencyAdjustment(
        method=method,
        factor=factor,
        block_tokens=block_tokens,
    )


def _parse_v3(text: str, source_sha: str, header_found: bool) -> ParsedConfig:
    lines = _split_nonblank_lines(text)
    header_offset = 1 if header_found else 0
    if len(lines) <= header_offset:
        raise _corrupt(
            "v3 requires a JSON object",
            field_position="line:v3_json",
        )
    source_line = lines[header_offset][0]
    json_text = "\n".join(line for _, line in lines[header_offset:])
    try:
        document = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise _corrupt(
            "v3 JSON is not parseable",
            source_line=source_line + max(0, exc.lineno - 1),
            field_position=f"line:{source_line + max(0, exc.lineno - 1)},column:{exc.colno}",
            reason=exc.msg,
        ) from exc
    if not isinstance(document, dict):
        raise _corrupt(
            "v3 root must be a JSON object",
            source_line=source_line,
            field_position="line:v3_json",
        )
    unknown = sorted(set(document) - set(_V3_REQUIRED))
    if unknown:
        raise _corrupt(
            "v3 config has unknown fields",
            source_line=source_line,
            field=unknown[0],
            field_position=_json_location(source_line, unknown[0]),
        )
    for name in _V3_REQUIRED:
        if name not in document:
            raise _corrupt(
                "v3 required field is missing",
                source_line=source_line,
                field=name,
                field_position=_json_location(source_line, name),
            )

    locations = {
        name: _json_location(source_line, name) for name in _V3_REQUIRED
    }
    base_theta = _parse_positive_float(
        document["base_theta"],
        field="base_theta",
        location=locations["base_theta"],
        source_line=source_line,
    )
    pos_scale = _parse_positive_float(
        document["pos_scale"],
        field="pos_scale",
        location=locations["pos_scale"],
        source_line=source_line,
    )
    block_size = _parse_positive_int(
        document["block_size"],
        field="block_size",
        location=locations["block_size"],
        source_line=source_line,
    )
    variant = document["variant"]
    if not isinstance(variant, str) or variant not in V3_VARIANTS:
        raise _corrupt(
            "v3 variant is not supported",
            source_line=source_line,
            field="variant",
            field_position=locations["variant"],
            supported=list(V3_VARIANTS),
        )
    frequency = _parse_frequency_adjustment(
        document["frequency_adjustment"], source_line=source_line
    )
    if variant == "block_freq" and frequency.method != "blockwise":
        raise _corrupt(
            "variant block_freq requires blockwise frequency_adjustment",
            source_line=source_line,
            field="frequency_adjustment.method",
            field_position=locations["frequency_adjustment"],
        )

    raw_scale = document["per_head_scale"]
    if isinstance(raw_scale, (int, float)) and not isinstance(raw_scale, bool):
        raw_scales = [raw_scale]
    elif isinstance(raw_scale, list) and raw_scale:
        raw_scales = raw_scale
    else:
        raise _corrupt(
            "per_head_scale must be a positive number or a non-empty list",
            source_line=source_line,
            field="per_head_scale",
            field_position=locations["per_head_scale"],
        )
    per_head_scale = tuple(
        _parse_positive_float(
            item,
            field=f"per_head_scale[{index}]",
            location=_json_location(source_line, f"per_head_scale[{index}]"),
            source_line=source_line,
        )
        for index, item in enumerate(raw_scales)
    )
    return ParsedConfig(
        source_version=CONFIG_VERSION_V3,
        source_line=source_line,
        source_span=f"line:{lines[0][0]}-line:{lines[-1][0]}",
        source_text_sha256=source_sha,
        base_theta=base_theta,
        pos_scale=pos_scale,
        block_size=block_size,
        variant=variant,
        frequency_adjustment=frequency,
        per_head_scale=per_head_scale,
        explicit_fields=_V3_REQUIRED,
        field_locations=locations,
    )


def parse_config(text: str | bytes) -> ParsedConfig:
    """Parse one config by explicit version header or detectable field set."""

    if isinstance(text, bytes):
        try:
            decoded = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _corrupt(
                "config is not valid UTF-8",
                field_position="bytes",
                reason=str(exc),
            ) from exc
        source_bytes = text
    else:
        decoded = text
        source_bytes = text.encode("utf-8")
    lines = _split_nonblank_lines(decoded)
    if not lines:
        raise _corrupt(
            "config is empty",
            field_position="line:1",
        )
    version, first_line, header_found = _detect_version(lines)
    source_sha = sha256_bytes(source_bytes)
    if version == CONFIG_VERSION_V1:
        return _parse_v1(decoded, source_sha, bool(header_found))
    if version == CONFIG_VERSION_V2:
        return _parse_v2(decoded, source_sha, bool(header_found))
    if version == CONFIG_VERSION_V3:
        return _parse_v3(decoded, source_sha, bool(header_found))
    raise RopeError(
        E_VERSION_UNSUPPORTED,
        "configuration version is not supported",
        details={
            "source_line": first_line,
            "source_version": version,
            "supported_versions": list(SUPPORTED_CONFIG_VERSIONS),
        },
    )

