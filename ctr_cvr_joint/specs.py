"""Readers, normalizers and serializers for feature specification v1/v2/v3."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    E_FORMAT_INVALID,
    E_VERSION_UNSUPPORTED,
    fail,
)
from .hashing import canonical_json, sha256_bytes, sha256_text
from .models import (
    CategoricalFeature,
    CrossFeature,
    FeatureSpec,
    LineSummary,
    NumericFeature,
    StatSpec,
)


_MISSING_STRATEGIES_NUMERIC = {"zero", "mean", "error"}
_MISSING_STRATEGIES_CATEGORICAL = {"default", "error"}
_MISSING_STRATEGIES_CROSS = {"zero", "error"}
_CROSS_SLOTS = {"code", "affinity", "product"}


@dataclass(frozen=True)
class LoadedSpec:
    version: int
    spec: FeatureSpec
    checksum: str
    raw_text: str
    source_name: str
    line_summaries: tuple[LineSummary, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_name": self.source_name,
            "checksum": self.checksum,
            "line_summaries": [item.to_dict() for item in self.line_summaries],
            "internal": self.spec.to_dict(),
        }


def _summarize_lines(text: str) -> tuple[LineSummary, ...]:
    summaries: list[LineSummary] = []
    for number, line in enumerate(text.splitlines(), start=1):
        preview = line if len(line) <= 120 else line[:117] + "..."
        summaries.append(
            LineSummary(
                line=number,
                checksum=sha256_text(line),
                preview=preview,
            )
        )
    return tuple(summaries)


def load_spec(path: str | Path) -> LoadedSpec:
    source = Path(path)
    return load_spec_bytes(source.read_bytes(), source_name=str(source))


def load_spec_bytes(data: bytes, *, source_name: str = "<memory>") -> LoadedSpec:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        fail(
            E_FORMAT_INVALID,
            "feature specification is not UTF-8",
            field="encoding",
            details=str(exc),
        )
    stripped = text.lstrip()
    if not stripped:
        fail(E_FORMAT_INVALID, "feature specification is empty", field="content")
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            fail(
                E_FORMAT_INVALID,
                "invalid JSON feature specification",
                line=exc.lineno,
                field="json",
                column=exc.colno,
            )
        version = _detect_json_version(payload)
        if version == 2:
            spec = _parse_v2(payload, text)
        elif version == 3:
            spec = _parse_v3(payload, text)
        else:
            fail(
                E_VERSION_UNSUPPORTED,
                f"feature specification version {version!r} is unsupported",
                field="format_version",
                version=version,
            )
    else:
        version = _detect_text_version(text)
        if version != 1:
            fail(
                E_VERSION_UNSUPPORTED,
                f"feature specification version {version!r} is unsupported",
                field="format_version",
                version=version,
            )
        spec = _parse_v1(text)
    return LoadedSpec(
        version=spec.source_version,
        spec=spec,
        checksum=sha256_bytes(data),
        raw_text=text,
        source_name=source_name,
        line_summaries=_summarize_lines(text),
    )


def _detect_text_version(text: str) -> Any:
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"(?:version|format_version|spec_version)\s*=\s*([^\s]+)", line, re.I
        )
        if match:
            token = match.group(1).strip().lower().lstrip("v")
            try:
                return int(token)
            except ValueError:
                return match.group(1)
        if line.startswith(("N|", "C|", "X|", "S|")):
            # Field-set dispatch: a recognizable pipe row identifies v1 even if
            # the optional header is absent.
            return 1
        return line.split("=", 1)[-1].strip()
    fail(E_FORMAT_INVALID, "missing specification version header", field="format_version")


def _detect_json_version(payload: Any) -> Any:
    if not isinstance(payload, dict):
        fail(
            E_FORMAT_INVALID,
            "JSON feature specification must be an object",
            line=1,
            field="$",
        )
    if "format_version" not in payload:
        # Field-set dispatch for JSON formats when the explicit header is
        # omitted.  The inferred version is still returned by LoadedSpec.
        if {
            "numeric_features",
            "categorical_features",
            "stats_semantics",
        } <= set(payload):
            return 3
        if "features" in payload and "statistics" in payload:
            return 2
        fail(
            E_FORMAT_INVALID,
            "required field 'format_version' is missing and the field set is ambiguous",
            line=1,
            field="format_version",
        )
    raw = payload["format_version"]
    if isinstance(raw, str):
        token = raw.strip().lower().lstrip("v")
        try:
            return int(token)
        except ValueError:
            return raw
    if isinstance(raw, bool):
        return raw
    return raw


def _json_field_line(text: str, field: str, default: int = 1) -> int:
    leaf = field.rsplit(".", 1)[-1]
    candidates = (f'"{leaf}"', field)
    for number, line in enumerate(text.splitlines(), start=1):
        if any(candidate in line for candidate in candidates):
            return number
    return default


def _required(
    mapping: Any,
    field: str,
    *,
    line: int,
    path: str,
) -> Any:
    if not isinstance(mapping, dict):
        fail(
            E_FORMAT_INVALID,
            f"field {path!r} must be an object",
            line=line,
            field=path,
        )
    if field not in mapping:
        fail(
            E_FORMAT_INVALID,
            f"required field {path!r} is missing",
            line=line,
            field=path,
        )
    return mapping[field]


def _float(
    value: Any,
    *,
    field: str,
    line: int,
    minimum: float | None = None,
    strict_minimum: bool = False,
) -> float:
    if isinstance(value, bool):
        fail(
            E_FORMAT_INVALID,
            f"field {field!r} must be numeric",
            line=line,
            field=field,
        )
    try:
        number = float(value)
    except (TypeError, ValueError):
        fail(
            E_FORMAT_INVALID,
            f"field {field!r} is not numeric",
            line=line,
            field=field,
            value=value,
        )
    if not math.isfinite(number):
        fail(
            E_FORMAT_INVALID,
            f"field {field!r} must be finite",
            line=line,
            field=field,
            value=value,
        )
    if minimum is not None:
        invalid = number <= minimum if strict_minimum else number < minimum
        if invalid:
            comparator = ">" if strict_minimum else ">="
            fail(
                E_FORMAT_INVALID,
                f"field {field!r} must be {comparator} {minimum}",
                line=line,
                field=field,
                value=value,
            )
    return 0.0 if number == 0.0 else number


def _string(value: Any, *, field: str, line: int) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(
            E_FORMAT_INVALID,
            f"required field {field!r} is missing or empty",
            line=line,
            field=field,
        )
    return value.strip()


def _positive_int(value: Any, *, field: str, line: int) -> int:
    number = _float(value, field=field, line=line, minimum=1)
    if number != int(number):
        fail(
            E_FORMAT_INVALID,
            f"field {field!r} must be an integer",
            line=line,
            field=field,
            value=value,
        )
    return int(number)


def _strategy(
    value: Any,
    *,
    field: str,
    line: int,
    allowed: set[str],
) -> str:
    strategy = _string(value, field=field, line=line)
    if strategy not in allowed:
        fail(
            E_FORMAT_INVALID,
            f"unsupported missing strategy {strategy!r}",
            line=line,
            field=field,
            allowed=sorted(allowed),
        )
    return strategy


def _parse_v1(text: str) -> FeatureSpec:
    numeric: list[NumericFeature] = []
    categorical: list[CategoricalFeature] = []
    cross: list[CrossFeature] = []
    statistics: list[StatSpec] = []
    seen_names: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"(?:version|format_version|spec_version)\s*=\s*1", line, re.I):
            continue
        fields = [item.strip() for item in line.split("|")]
        kind = fields[0]
        if kind == "N":
            if len(fields) != 5:
                fail(
                    E_FORMAT_INVALID,
                    "v1 numeric row must contain exactly 5 pipe-delimited fields",
                    line=line_number,
                    field="columns",
                    expected=5,
                    actual=len(fields),
                )
            name = _string(fields[1], field="name", line=line_number)
            _check_duplicate(seen_names, name, line_number)
            mean = _float(fields[2], field=f"numeric.{name}.mean", line=line_number)
            std = _float(
                fields[3],
                field=f"numeric.{name}.std",
                line=line_number,
                minimum=0.0,
                strict_minimum=True,
            )
            weight = _float(fields[4], field=f"numeric.{name}.weight", line=line_number)
            numeric.append(
                NumericFeature(
                    name=name,
                    mean=mean,
                    std=std,
                    weight=weight,
                    missing_strategy=None,
                    default=None,
                )
            )
        elif kind == "C":
            if len(fields) != 4:
                fail(
                    E_FORMAT_INVALID,
                    "v1 categorical row must contain exactly 4 pipe-delimited fields",
                    line=line_number,
                    field="columns",
                    expected=4,
                    actual=len(fields),
                )
            name = _string(fields[1], field="name", line=line_number)
            _check_duplicate(seen_names, name, line_number)
            weight = _float(fields[2], field=f"categorical.{name}.weight", line=line_number)
            buckets = _positive_int(
                fields[3],
                field=f"categorical.{name}.buckets",
                line=line_number,
            )
            categorical.append(
                CategoricalFeature(
                    name=name,
                    weight=weight,
                    buckets=buckets,
                    missing_strategy=None,
                    default=None,
                )
            )
        elif kind == "X":
            if len(fields) != 6:
                fail(
                    E_FORMAT_INVALID,
                    "v1 cross row must contain exactly 6 pipe-delimited fields",
                    line=line_number,
                    field="columns",
                    expected=6,
                    actual=len(fields),
                )
            name = _string(fields[1], field="name", line=line_number)
            _check_duplicate(seen_names, name, line_number)
            left = _string(fields[2], field=f"cross.{name}.left", line=line_number)
            right = _string(fields[3], field=f"cross.{name}.right", line=line_number)
            slots = tuple(
                sorted(
                    item.strip() for item in fields[4].split(",") if item.strip()
                )
            )
            if not slots:
                fail(
                    E_FORMAT_INVALID,
                    "cross slots cannot be empty",
                    line=line_number,
                    field=f"cross.{name}.slots",
                )
            _validate_slots(slots, field=f"cross.{name}.slots", line=line_number)
            weight = _float(fields[5], field=f"cross.{name}.weight", line=line_number)
            cross.append(
                CrossFeature(
                    name=name,
                    left=left,
                    right=right,
                    weight=weight,
                    slots=slots,
                    missing_strategy=None,
                )
            )
        elif kind == "S":
            if len(fields) != 4:
                fail(
                    E_FORMAT_INVALID,
                    "v1 statistics row must contain exactly 4 pipe-delimited fields",
                    line=line_number,
                    field="columns",
                    expected=4,
                    actual=len(fields),
                )
            metric = _string(fields[1], field="metric", line=line_number)
            if metric not in {"ctr", "cvr"}:
                fail(
                    E_FORMAT_INVALID,
                    "statistics metric must be ctr or cvr",
                    line=line_number,
                    field=f"statistics.{metric}.metric",
                )
            if metric in {item.metric for item in statistics}:
                fail(
                    E_FORMAT_INVALID,
                    f"duplicate statistics metric {metric!r}",
                    line=line_number,
                    field=f"statistics.{metric}",
                )
            prior = _float(
                fields[2],
                field=f"statistics.{metric}.prior",
                line=line_number,
                minimum=0.0,
                strict_minimum=True,
            )
            smoothing = _float(
                fields[3],
                field=f"statistics.{metric}.smoothing",
                line=line_number,
                minimum=0.0,
            )
            statistics.append(
                StatSpec(
                    metric=metric,
                    prior=prior,
                    smoothing=smoothing,
                    semantics_name=None,
                    semantics_version=None,
                )
            )
        else:
            fail(
                E_FORMAT_INVALID,
                f"unknown v1 row kind {kind!r}",
                line=line_number,
                field="kind",
            )
    _validate_cross_references(numeric, categorical, cross)
    _validate_required_content(numeric, categorical, statistics)
    return FeatureSpec(
        source_version=1,
        numeric=tuple(sorted(numeric, key=lambda item: item.name)),
        categorical=tuple(sorted(categorical, key=lambda item: item.name)),
        cross=tuple(sorted(cross, key=lambda item: item.name)),
        statistics=tuple(sorted(statistics, key=lambda item: item.metric)),
        metadata={"format": "v1-pipe"},
    )


def _check_duplicate(seen: set[str], name: str, line: int) -> None:
    if name in seen:
        fail(
            E_FORMAT_INVALID,
            f"duplicate feature name {name!r}",
            line=line,
            field="name",
            value=name,
        )
    seen.add(name)


def _validate_slots(slots: Iterable[str], *, field: str, line: int) -> None:
    for slot in slots:
        if slot not in _CROSS_SLOTS:
            fail(
                E_FORMAT_INVALID,
                f"unsupported cross slot {slot!r}",
                line=line,
                field=field,
                allowed=sorted(_CROSS_SLOTS),
            )


def _validate_cross_references(
    numeric: list[NumericFeature],
    categorical: list[CategoricalFeature],
    cross: list[CrossFeature],
) -> None:
    names = {item.name for item in numeric} | {item.name for item in categorical}
    for item in cross:
        for side, name in (("left", item.left), ("right", item.right)):
            if name not in names:
                fail(
                    E_FORMAT_INVALID,
                    f"cross feature {item.name!r} references unknown {side} feature {name!r}",
                    line=1,
                    field=f"cross.{item.name}.{side}",
                )


def _validate_unique_names(
    numeric: list[NumericFeature],
    categorical: list[CategoricalFeature],
    cross: list[CrossFeature],
) -> None:
    seen: set[str] = set()
    for item in [*numeric, *categorical, *cross]:
        if item.name in seen:
            fail(
                E_FORMAT_INVALID,
                f"duplicate feature name {item.name!r}",
                line=1,
                field="name",
                value=item.name,
            )
        seen.add(item.name)


def _validate_required_content(
    numeric: list[NumericFeature],
    categorical: list[CategoricalFeature],
    statistics: list[StatSpec],
) -> None:
    if not numeric and not categorical:
        fail(
            E_FORMAT_INVALID,
            "feature specification must declare at least one feature",
            line=1,
            field="features",
        )
    metrics = {item.metric for item in statistics}
    if metrics != {"ctr", "cvr"}:
        missing = sorted({"ctr", "cvr"} - metrics)
        fail(
            E_FORMAT_INVALID,
            f"missing required statistics declarations: {', '.join(missing)}",
            line=1,
            field=f"statistics.{missing[0]}",
        )


def _parse_v2(payload: dict[str, Any], text: str) -> FeatureSpec:
    line = 1
    features = _required(payload, "features", line=line, path="features")
    raw_numeric = _required(features, "numeric", line=line, path="features.numeric")
    raw_categorical = _required(
        features, "categorical", line=line, path="features.categorical"
    )
    raw_cross = features.get("cross", [])
    raw_statistics = _required(
        payload, "statistics", line=line, path="statistics"
    )
    if not isinstance(raw_numeric, list) or not isinstance(raw_categorical, list):
        fail(
            E_FORMAT_INVALID,
            "v2 numeric and categorical feature collections must be arrays",
            line=line,
            field="features",
        )
    numeric = [
        _parse_v2_numeric(item, index, text) for index, item in enumerate(raw_numeric)
    ]
    categorical = [
        _parse_v2_categorical(item, index, text)
        for index, item in enumerate(raw_categorical)
    ]
    cross = [
        _parse_v2_cross(item, index, text) for index, item in enumerate(raw_cross or [])
    ]
    statistics = _parse_statistics_plain(raw_statistics, text)
    _validate_cross_references(numeric, categorical, cross)
    _validate_unique_names(numeric, categorical, cross)
    _validate_required_content(numeric, categorical, statistics)
    return FeatureSpec(
        source_version=2,
        numeric=tuple(sorted(numeric, key=lambda item: item.name)),
        categorical=tuple(sorted(categorical, key=lambda item: item.name)),
        cross=tuple(sorted(cross, key=lambda item: item.name)),
        statistics=tuple(sorted(statistics, key=lambda item: item.metric)),
        metadata={"format": "v2-keyed-json"},
    )


def _parse_v2_numeric(item: Any, index: int, text: str) -> NumericFeature:
    base = f"features.numeric[{index}]"
    line = _json_field_line(text, base)
    name = _string(_required(item, "name", line=line, path=f"{base}.name"), field=f"{base}.name", line=line)
    mean = _float(_required(item, "mean", line=line, path=f"{base}.mean"), field=f"{base}.mean", line=line)
    std = _float(
        _required(item, "std", line=line, path=f"{base}.std"),
        field=f"{base}.std",
        line=line,
        minimum=0.0,
        strict_minimum=True,
    )
    weight = _float(
        _required(item, "weight", line=line, path=f"{base}.weight"),
        field=f"{base}.weight",
        line=line,
    )
    strategy = _strategy(
        _required(
            item,
            "missing_strategy",
            line=line,
            path=f"{base}.missing_strategy",
        ),
        field=f"{base}.missing_strategy",
        line=line,
        allowed=_MISSING_STRATEGIES_NUMERIC,
    )
    return NumericFeature(name, mean, std, weight, strategy, None)


def _parse_v2_categorical(item: Any, index: int, text: str) -> CategoricalFeature:
    base = f"features.categorical[{index}]"
    line = _json_field_line(text, base)
    name = _string(_required(item, "name", line=line, path=f"{base}.name"), field=f"{base}.name", line=line)
    weight = _float(
        _required(item, "weight", line=line, path=f"{base}.weight"),
        field=f"{base}.weight",
        line=line,
    )
    buckets = _positive_int(
        _required(item, "buckets", line=line, path=f"{base}.buckets"),
        field=f"{base}.buckets",
        line=line,
    )
    strategy = _strategy(
        _required(
            item,
            "missing_strategy",
            line=line,
            path=f"{base}.missing_strategy",
        ),
        field=f"{base}.missing_strategy",
        line=line,
        allowed=_MISSING_STRATEGIES_CATEGORICAL,
    )
    return CategoricalFeature(name, weight, buckets, strategy, None)


def _parse_v2_cross(item: Any, index: int, text: str) -> CrossFeature:
    base = f"features.cross[{index}]"
    line = _json_field_line(text, base)
    name = _string(_required(item, "name", line=line, path=f"{base}.name"), field=f"{base}.name", line=line)
    left = _string(_required(item, "left", line=line, path=f"{base}.left"), field=f"{base}.left", line=line)
    right = _string(_required(item, "right", line=line, path=f"{base}.right"), field=f"{base}.right", line=line)
    weight = _float(
        _required(item, "weight", line=line, path=f"{base}.weight"),
        field=f"{base}.weight",
        line=line,
    )
    raw_slots = _required(item, "slots", line=line, path=f"{base}.slots")
    if not isinstance(raw_slots, list) or not raw_slots:
        fail(
            E_FORMAT_INVALID,
            "cross slots must be a non-empty array",
            line=line,
            field=f"{base}.slots",
        )
    slots = tuple(
        sorted(_string(value, field=f"{base}.slots", line=line) for value in raw_slots)
    )
    _validate_slots(slots, field=f"{base}.slots", line=line)
    strategy = _strategy(
        _required(
            item,
            "missing_strategy",
            line=line,
            path=f"{base}.missing_strategy",
        ),
        field=f"{base}.missing_strategy",
        line=line,
        allowed=_MISSING_STRATEGIES_CROSS,
    )
    return CrossFeature(name, left, right, weight, slots, strategy)


def _parse_statistics_plain(raw: Any, text: str) -> list[StatSpec]:
    line = _json_field_line(text, "statistics")
    if not isinstance(raw, dict):
        fail(
            E_FORMAT_INVALID,
            "statistics must be an object",
            line=line,
            field="statistics",
        )
    result: list[StatSpec] = []
    for metric in ("ctr", "cvr"):
        base = f"statistics.{metric}"
        item = _required(raw, metric, line=line, path=base)
        prior = _float(
            _required(item, "prior", line=line, path=f"{base}.prior"),
            field=f"{base}.prior",
            line=line,
            minimum=0.0,
            strict_minimum=True,
        )
        smoothing = _float(
            _required(item, "smoothing", line=line, path=f"{base}.smoothing"),
            field=f"{base}.smoothing",
            line=line,
            minimum=0.0,
        )
        result.append(StatSpec(metric, prior, smoothing, None, None))
    return result


def _parse_v3(payload: dict[str, Any], text: str) -> FeatureSpec:
    line = 1
    numeric_map = _required(
        payload, "numeric_features", line=line, path="numeric_features"
    )
    categorical_map = _required(
        payload, "categorical_features", line=line, path="categorical_features"
    )
    cross_map = payload.get("cross_features", {})
    statistics_raw = _required(payload, "statistics", line=line, path="statistics")
    semantics = _required(
        payload, "stats_semantics", line=line, path="stats_semantics"
    )
    if not isinstance(numeric_map, dict) or not isinstance(categorical_map, dict):
        fail(
            E_FORMAT_INVALID,
            "v3 feature collections must be objects keyed by feature name",
            line=line,
            field="features",
        )
    if not isinstance(cross_map, dict):
        fail(
            E_FORMAT_INVALID,
            "v3 cross_features must be an object",
            line=line,
            field="cross_features",
        )
    semantics_name = _string(
        _required(semantics, "name", line=line, path="stats_semantics.name"),
        field="stats_semantics.name",
        line=line,
    )
    semantics_version = _positive_int(
        _required(
            semantics, "version", line=line, path="stats_semantics.version"
        ),
        field="stats_semantics.version",
        line=line,
    )
    numeric = [
        _parse_v3_numeric(name, definition, text)
        for name, definition in sorted(numeric_map.items())
    ]
    categorical = [
        _parse_v3_categorical(name, definition, text)
        for name, definition in sorted(categorical_map.items())
    ]
    cross = [
        _parse_v3_cross(name, definition, text)
        for name, definition in sorted(cross_map.items())
    ]
    statistics = _parse_statistics_v3(
        statistics_raw, semantics_name, semantics_version, text
    )
    _validate_cross_references(numeric, categorical, cross)
    _validate_unique_names(numeric, categorical, cross)
    _validate_required_content(numeric, categorical, statistics)
    return FeatureSpec(
        source_version=3,
        numeric=tuple(numeric),
        categorical=tuple(categorical),
        cross=tuple(cross),
        statistics=tuple(sorted(statistics, key=lambda item: item.metric)),
        metadata={
            "format": "v3-nested-json",
            "stats_semantics": {
                "name": semantics_name,
                "version": semantics_version,
            },
        },
    )


def _parse_missing_v3(
    definition: dict[str, Any],
    *,
    base: str,
    line: int,
    allowed: set[str],
) -> tuple[str, float | str | None]:
    missing = _required(definition, "missing", line=line, path=f"{base}.missing")
    if isinstance(missing, str):
        return _strategy(missing, field=f"{base}.missing.strategy", line=line, allowed=allowed), None
    strategy = _strategy(
        _required(missing, "strategy", line=line, path=f"{base}.missing.strategy"),
        field=f"{base}.missing.strategy",
        line=line,
        allowed=allowed,
    )
    return strategy, missing.get("value")


def _parse_v3_numeric(name: str, definition: Any, text: str) -> NumericFeature:
    base = f"numeric_features.{name}"
    line = _json_field_line(text, name)
    stats = _required(
        definition, "stats", line=line, path=f"{base}.stats"
    )
    mean = _float(
        _required(stats, "mean", line=line, path=f"{base}.stats.mean"),
        field=f"{base}.stats.mean",
        line=line,
    )
    std = _float(
        _required(stats, "std", line=line, path=f"{base}.stats.std"),
        field=f"{base}.stats.std",
        line=line,
        minimum=0.0,
        strict_minimum=True,
    )
    weight = _float(
        _required(definition, "weight", line=line, path=f"{base}.weight"),
        field=f"{base}.weight",
        line=line,
    )
    default = _float(
        _required(
            definition, "default_value", line=line, path=f"{base}.default_value"
        ),
        field=f"{base}.default_value",
        line=line,
    )
    strategy, missing_value = _parse_missing_v3(
        definition,
        base=base,
        line=line,
        allowed=_MISSING_STRATEGIES_NUMERIC | {"default"},
    )
    if missing_value is not None:
        default = _float(
            missing_value, field=f"{base}.missing.value", line=line
        )
    return NumericFeature(name, mean, std, weight, strategy, default)


def _parse_v3_categorical(
    name: str, definition: Any, text: str
) -> CategoricalFeature:
    base = f"categorical_features.{name}"
    line = _json_field_line(text, name)
    encoding = _required(definition, "encoding", line=line, path=f"{base}.encoding")
    if _required(encoding, "kind", line=line, path=f"{base}.encoding.kind") != "hash_mod":
        fail(
            E_FORMAT_INVALID,
            "only hash_mod categorical encoding is supported",
            line=line,
            field=f"{base}.encoding.kind",
        )
    buckets = _positive_int(
        _required(
            encoding, "buckets", line=line, path=f"{base}.encoding.buckets"
        ),
        field=f"{base}.encoding.buckets",
        line=line,
    )
    weight = _float(
        _required(definition, "weight", line=line, path=f"{base}.weight"),
        field=f"{base}.weight",
        line=line,
    )
    default = _string(
        _required(
            definition, "default_value", line=line, path=f"{base}.default_value"
        ),
        field=f"{base}.default_value",
        line=line,
    )
    strategy, missing_value = _parse_missing_v3(
        definition,
        base=base,
        line=line,
        allowed=_MISSING_STRATEGIES_CATEGORICAL,
    )
    if missing_value is not None:
        default = _string(
            missing_value, field=f"{base}.missing.value", line=line
        )
    return CategoricalFeature(name, weight, buckets, strategy, default)


def _parse_v3_cross(name: str, definition: Any, text: str) -> CrossFeature:
    base = f"cross_features.{name}"
    line = _json_field_line(text, name)
    left = _string(
        _required(definition, "left", line=line, path=f"{base}.left"),
        field=f"{base}.left",
        line=line,
    )
    right = _string(
        _required(definition, "right", line=line, path=f"{base}.right"),
        field=f"{base}.right",
        line=line,
    )
    weight = _float(
        _required(definition, "weight", line=line, path=f"{base}.weight"),
        field=f"{base}.weight",
        line=line,
    )
    raw_slots = _required(definition, "slots", line=line, path=f"{base}.slots")
    if isinstance(raw_slots, dict):
        slots = tuple(sorted(raw_slots))
    elif isinstance(raw_slots, list):
        slots = tuple(
            _string(value, field=f"{base}.slots", line=line) for value in raw_slots
        )
    else:
        fail(
            E_FORMAT_INVALID,
            "v3 cross slots must be an object or array",
            line=line,
            field=f"{base}.slots",
        )
    if not slots:
        fail(
            E_FORMAT_INVALID,
            "cross slots cannot be empty",
            line=line,
            field=f"{base}.slots",
        )
    _validate_slots(slots, field=f"{base}.slots", line=line)
    strategy, _ = _parse_missing_v3(
        definition,
        base=base,
        line=line,
        allowed=_MISSING_STRATEGIES_CROSS,
    )
    return CrossFeature(name, left, right, weight, slots, strategy)


def _parse_statistics_v3(
    raw: Any,
    semantics_name: str,
    semantics_version: int,
    text: str,
) -> list[StatSpec]:
    line = _json_field_line(text, "statistics")
    if not isinstance(raw, dict):
        fail(
            E_FORMAT_INVALID,
            "statistics must be an object",
            line=line,
            field="statistics",
        )
    result: list[StatSpec] = []
    for metric in ("ctr", "cvr"):
        base = f"statistics.{metric}"
        item = _required(raw, metric, line=line, path=base)
        prior = _float(
            _required(item, "prior", line=line, path=f"{base}.prior"),
            field=f"{base}.prior",
            line=line,
            minimum=0.0,
            strict_minimum=True,
        )
        smoothing = _float(
            _required(item, "smoothing", line=line, path=f"{base}.smoothing"),
            field=f"{base}.smoothing",
            line=line,
            minimum=0.0,
        )
        result.append(
            StatSpec(
                metric,
                prior,
                smoothing,
                semantics_name,
                semantics_version,
            )
        )
    return result


def normalize_spec(spec: FeatureSpec) -> FeatureSpec:
    """Fill format-specific implicit values into the canonical runtime view."""

    default_semantics = {
        1: ("legacy_prior_v1", 1),
        2: ("snapshot_bayes_v2", 2),
        3: ("nested_snapshot_bayes_v3", 3),
    }[spec.source_version]
    numeric: list[NumericFeature] = []
    for item in spec.numeric:
        strategy = item.missing_strategy or "zero"
        default = item.default
        if default is None:
            default = item.mean if strategy == "mean" else 0.0
        numeric.append(
            NumericFeature(
                item.name,
                item.mean,
                item.std,
                item.weight,
                strategy,
                default,
            )
        )
    categorical: list[CategoricalFeature] = []
    for item in spec.categorical:
        strategy = item.missing_strategy or "default"
        default = item.default if item.default is not None else "__MISSING__"
        categorical.append(
            CategoricalFeature(
                item.name,
                item.weight,
                item.buckets or 16,
                strategy,
                default,
            )
        )
    cross: list[CrossFeature] = []
    for item in spec.cross:
        cross.append(
            CrossFeature(
                item.name,
                item.left,
                item.right,
                item.weight,
                tuple(sorted(item.slots or ("code", "affinity"))),
                item.missing_strategy or "zero",
            )
        )
    statistics: list[StatSpec] = []
    for item in spec.statistics:
        sem_name = item.semantics_name or default_semantics[0]
        sem_version = item.semantics_version or default_semantics[1]
        statistics.append(
            StatSpec(
                item.metric,
                item.prior,
                item.smoothing,
                sem_name,
                sem_version,
            )
        )
    return FeatureSpec(
        source_version=spec.source_version,
        numeric=tuple(numeric),
        categorical=tuple(categorical),
        cross=tuple(cross),
        statistics=tuple(statistics),
        metadata=dict(spec.metadata),
    )


def serialize_spec(spec: FeatureSpec, version: int) -> str:
    """Serialize a specification to v1/v2/v3.

    Serialization is lossy in exactly the ways documented by the downgrade
    migration rules.  Callers that need an audit trail should use
    :func:`ctr_cvr_joint.migration.migrate_spec`.
    """

    normalized = normalize_spec(spec)
    if version == 1:
        return _serialize_v1(normalized)
    if version == 2:
        return _serialize_v2(normalized)
    if version == 3:
        return _serialize_v3(normalized)
    fail(
        E_VERSION_UNSUPPORTED,
        f"feature specification version {version!r} is unsupported",
        field="target_version",
        version=version,
    )


def _serialize_v1(spec: FeatureSpec) -> str:
    lines = ["version=1"]
    for item in sorted(spec.numeric, key=lambda value: value.name):
        lines.append(
            f"N|{item.name}|{_number(item.mean)}|{_number(item.std)}|{_number(item.weight)}"
        )
    for item in sorted(spec.categorical, key=lambda value: value.name):
        lines.append(
            f"C|{item.name}|{_number(item.weight)}|{int(item.buckets or 16)}"
        )
    for item in sorted(spec.cross, key=lambda value: value.name):
        lines.append(
            "X|"
            + "|".join(
                [
                    item.name,
                    item.left,
                    item.right,
                    ",".join(item.slots),
                    _number(item.weight),
                ]
            )
        )
    for item in sorted(spec.statistics, key=lambda value: value.metric):
        lines.append(
            f"S|{item.metric}|{_number(item.prior)}|{_number(item.smoothing)}"
        )
    return "\n".join(lines) + "\n"


def _serialize_v2(spec: FeatureSpec) -> str:
    payload = {
        "format_version": 2,
        "features": {
            "numeric": [
                {
                    "name": item.name,
                    "mean": item.mean,
                    "std": item.std,
                    "weight": item.weight,
                    "missing_strategy": item.missing_strategy,
                }
                for item in sorted(spec.numeric, key=lambda value: value.name)
            ],
            "categorical": [
                {
                    "name": item.name,
                    "weight": item.weight,
                    "buckets": int(item.buckets or 16),
                    "missing_strategy": item.missing_strategy,
                }
                for item in sorted(spec.categorical, key=lambda value: value.name)
            ],
            "cross": [
                {
                    "name": item.name,
                    "left": item.left,
                    "right": item.right,
                    "weight": item.weight,
                    "slots": list(item.slots),
                    "missing_strategy": item.missing_strategy,
                }
                for item in sorted(spec.cross, key=lambda value: value.name)
            ],
        },
        "statistics": {
            item.metric: {
                "prior": item.prior,
                "smoothing": item.smoothing,
            }
            for item in sorted(spec.statistics, key=lambda value: value.metric)
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _serialize_v3(spec: FeatureSpec) -> str:
    semantics: tuple[str, int] = ("nested_snapshot_bayes_v3", 3)
    for item in spec.statistics:
        if item.semantics_name and item.semantics_version:
            semantics = (item.semantics_name, item.semantics_version)
            break
    payload = {
        "format_version": 3,
        "numeric_features": {
            item.name: {
                "stats": {"mean": item.mean, "std": item.std},
                "weight": item.weight,
                "default_value": item.default,
                "missing": {"strategy": item.missing_strategy},
            }
            for item in sorted(spec.numeric, key=lambda value: value.name)
        },
        "categorical_features": {
            item.name: {
                "encoding": {
                    "kind": "hash_mod",
                    "buckets": int(item.buckets or 16),
                },
                "weight": item.weight,
                "default_value": item.default,
                "missing": {"strategy": item.missing_strategy},
            }
            for item in sorted(spec.categorical, key=lambda value: value.name)
        },
        "cross_features": {
            item.name: {
                "left": item.left,
                "right": item.right,
                "weight": item.weight,
                "slots": {
                    slot: {
                        "kind": "numeric"
                        if slot in {"affinity", "product"}
                        else "hash_mod"
                    }
                    for slot in item.slots
                },
                "missing": {"strategy": item.missing_strategy},
            }
            for item in sorted(spec.cross, key=lambda value: value.name)
        },
        "statistics": {
            item.metric: {
                "prior": item.prior,
                "smoothing": item.smoothing,
            }
            for item in sorted(spec.statistics, key=lambda value: value.metric)
        },
        "stats_semantics": {
            "name": semantics[0],
            "version": semantics[1],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(float(value))


def spec_text_checksum(text: str) -> str:
    return sha256_text(text)


def parse_spec_text(text: str, version: int | None = None) -> FeatureSpec:
    """Parse a target serialization without changing its provenance version."""

    loaded = load_spec_bytes(text.encode("utf-8"))
    if version is not None and loaded.version != version:
        fail(
            E_FORMAT_INVALID,
            f"expected specification version {version}, got {loaded.version}",
            field="format_version",
        )
    return loaded.spec


def spec_to_canonical_json(spec: FeatureSpec) -> str:
    return canonical_json(normalize_spec(spec).to_dict())


__all__ = [
    "LoadedSpec",
    "load_spec",
    "load_spec_bytes",
    "normalize_spec",
    "serialize_spec",
    "parse_spec_text",
    "spec_to_canonical_json",
    "spec_text_checksum",
    "BusinessError",
]

