"""Readers for the three coexisting monitoring configuration dialects.

Dispatch is driven either by the textual format header
(``# DRIFT_MONITOR_CONFIG v1`` / ``v2``) or by the structured field set of a
v3 JSON document.  Structural problems always raise ``E_FORMAT_CORRUPT`` with
the offending 1-based line number and field name; an unknown version raises
``E_VERSION_UNSUPPORTED``.  Semantic problems (unknown test method, threshold
out of range) are left to :class:`MonitorConfig.validate` and surface as
``E_CONFIG_INVALID``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .config_model import (
    CATEGORICAL,
    COLUMN_TYPES,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_QUANTILES,
    DEFAULT_WINDOW,
    FeatureSpec,
    MonitorConfig,
)
from .errors import DriftError, ErrorCode

HEADER_PREFIX = "# DRIFT_MONITOR_CONFIG"
HEADER_RE = re.compile(r"^# DRIFT_MONITOR_CONFIG (v\d+)\s*$")

V1_FIELDS: Tuple[str, ...] = ("name", "type", "threshold", "window", "enabled")
V2_REQUIRED_KEYS: Tuple[str, ...] = (
    "name",
    "type",
    "threshold",
    "window",
    "enabled",
)


def _corrupt(message: str, line: int, field: Optional[str] = None) -> DriftError:
    context: Dict[str, Any] = {"line": line}
    if field is not None:
        context["field"] = field
    return DriftError(ErrorCode.FORMAT_CORRUPT, message, context)


def _parse_bool(raw: str, line: int, field: str) -> bool:
    token = raw.strip().lower()
    if token in ("true", "1", "yes", "on"):
        return True
    if token in ("false", "0", "no", "off"):
        return False
    raise _corrupt(f"boolean field {field!r} is not parseable: {raw!r}", line, field)


def _parse_float(raw: str, line: int, field: str) -> float:
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        raise _corrupt(f"numeric field {field!r} is not parseable: {raw!r}", line, field)
    if value != value or value in (float("inf"), float("-inf")):
        raise _corrupt(f"numeric field {field!r} must be finite: {raw!r}", line, field)
    return value


def _parse_int(raw: str, line: int, field: str) -> int:
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        raise _corrupt(f"integer field {field!r} is not parseable: {raw!r}", line, field)


def _parse_type(raw: str, line: int, field: str = "type") -> str:
    value = raw.strip().lower()
    if value not in COLUMN_TYPES:
        raise _corrupt(f"unknown column type {raw!r}", line, field)
    return value


def read_config(text: str, config_id: str = "demo-config", validate: bool = True) -> MonitorConfig:
    """Parse configuration text in any of the three supported dialects."""

    stripped = text.lstrip()
    if stripped.startswith("#"):
        first_line = stripped.splitlines()[0].strip()
        match = HEADER_RE.match(first_line)
        if not match:
            # A header was started but is truncated or carries no version token.
            raise _corrupt(
                "configuration header is truncated or missing a version token",
                1,
                "format_version",
            )
        version = match.group(1)
        if version == "v1":
            config = _parse_v1(text, config_id)
        elif version == "v2":
            config = _parse_v2(text, config_id)
        elif version == "v3":
            config = _parse_v3_json("\n".join(stripped.splitlines()[1:]), config_id)
        else:
            raise DriftError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"configuration format version {version!r} is not supported",
                {"field": "format_version", "value": version},
            )
    elif stripped.startswith("{"):
        config = _parse_v3_json(stripped, config_id)
    else:
        raise DriftError(
            ErrorCode.VERSION_UNSUPPORTED,
            "cannot detect a configuration format header or JSON document",
            {"field": "format_version"},
        )

    if validate:
        config.validate()
    return config


def _data_lines(text: str) -> List[Tuple[int, str]]:
    lines: List[Tuple[int, str]] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append((index, line))
    return lines


def _parse_v1(text: str, config_id: str) -> MonitorConfig:
    features: Dict[str, FeatureSpec] = {}
    for line_no, line in _data_lines(text):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != len(V1_FIELDS):
            bad_field = V1_FIELDS[len(parts)] if len(parts) < len(V1_FIELDS) else f"extra[{len(parts)}]"
            raise _corrupt(
                f"expected {len(V1_FIELDS)} pipe separated fields but found {len(parts)}",
                line_no,
                bad_field,
            )
        record = dict(zip(V1_FIELDS, parts))
        spec = FeatureSpec(
            name=record["name"],
            column_type=_parse_type(record["type"], line_no),
            threshold=_parse_float(record["threshold"], line_no, "threshold"),
            window=_parse_int(record["window"], line_no, "window"),
            methods=[],  # filled in during migration to v2/v3
            quantiles=[],
            min_samples=DEFAULT_MIN_SAMPLES,
            enabled=_parse_bool(record["enabled"], line_no, "enabled"),
        )
        features[spec.name] = spec
    if not features:
        raise _corrupt("v1 configuration contains no feature rows", 1, "features")
    return MonitorConfig(config_id=config_id, source_format="v1", features=features)


def _parse_v2(text: str, config_id: str) -> MonitorConfig:
    features: Dict[str, FeatureSpec] = {}
    for line_no, line in _data_lines(text):
        record: Dict[str, str] = {}
        for chunk in line.split("|"):
            if "=" not in chunk:
                raise _corrupt(f"key=value fragment has no '=': {chunk!r}", line_no, "fragment")
            key, value = chunk.split("=", 1)
            record[key.strip()] = value.strip()
        for required in V2_REQUIRED_KEYS:
            if required not in record:
                raise _corrupt(f"required key {required!r} is missing", line_no, required)
        spec = FeatureSpec(
            name=record["name"],
            column_type=_parse_type(record["type"], line_no),
            threshold=_parse_float(record["threshold"], line_no, "threshold"),
            window=_parse_int(record["window"], line_no, "window"),
            methods=[m.strip() for m in record["methods"].split(",") if m.strip()]
            if "methods" in record
            else [],
            quantiles=[
                _parse_float(q, line_no, "quantiles")
                for q in record["quantiles"].split(",")
                if q.strip()
            ]
            if "quantiles" in record
            else [],
            min_samples=_parse_int(record["min_samples"], line_no, "min_samples")
            if "min_samples" in record
            else DEFAULT_MIN_SAMPLES,
            enabled=_parse_bool(record["enabled"], line_no, "enabled"),
        )
        features[spec.name] = spec
    if not features:
        raise _corrupt("v2 configuration contains no feature rows", 1, "features")
    return MonitorConfig(config_id=config_id, source_format="v2", features=features)


def _parse_v3_json(text: str, config_id: str) -> MonitorConfig:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriftError(
            ErrorCode.FORMAT_CORRUPT,
            f"v3 document is not valid JSON: {exc.msg}",
            {"line": exc.lineno, "field": "document"},
        )
    if not isinstance(document, dict) or "format_version" not in document:
        raise DriftError(
            ErrorCode.VERSION_UNSUPPORTED,
            "v3 document is missing the format_version field",
            {"field": "format_version"},
        )
    version = document["format_version"]
    if version != "v3":
        raise DriftError(
            ErrorCode.VERSION_UNSUPPORTED,
            f"configuration format version {version!r} is not supported",
            {"field": "format_version", "value": version},
        )
    raw_features = document.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise DriftError(
            ErrorCode.FORMAT_CORRUPT,
            "v3 document must contain a non-empty features array",
            {"line": 1, "field": "features"},
        )
    features: Dict[str, FeatureSpec] = {}
    for index, raw in enumerate(raw_features, start=1):
        for required in ("name", "type", "threshold", "window"):
            if required not in raw:
                raise DriftError(
                    ErrorCode.FORMAT_CORRUPT,
                    f"feature entry {index} is missing key {required!r}",
                    {"line": index, "field": required},
                )
        try:
            threshold = float(raw["threshold"])
        except (TypeError, ValueError):
            raise DriftError(
                ErrorCode.FORMAT_CORRUPT,
                f"threshold is not parseable: {raw['threshold']!r}",
                {"line": index, "field": "threshold"},
            )
        try:
            window = int(raw["window"])
        except (TypeError, ValueError):
            raise DriftError(
                ErrorCode.FORMAT_CORRUPT,
                f"window is not parseable: {raw['window']!r}",
                {"line": index, "field": "window"},
            )
        if raw["type"] not in COLUMN_TYPES:
            raise DriftError(
                ErrorCode.FORMAT_CORRUPT,
                f"unknown column type {raw['type']!r}",
                {"line": index, "field": "type"},
            )
        spec = FeatureSpec(
            name=str(raw["name"]),
            column_type=raw["type"],
            threshold=threshold,
            window=window,
            methods=list(raw.get("methods", [])),
            quantiles=[float(q) for q in raw.get("quantiles", [])],
            min_samples=int(raw.get("min_samples", DEFAULT_MIN_SAMPLES)),
            enabled=bool(raw.get("enabled", True)),
        )
        features[spec.name] = spec
    resolved_id = document.get("config_id", config_id)
    return MonitorConfig(config_id=str(resolved_id), source_format="v3", features=features)
