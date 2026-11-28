from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .common import read_json


SCHEMA_IDS = {
    "gsb.release/v1": "v1",
    "gsb.release/v2": "v2",
    "gsb.release/v3": "v3",
}
THRESHOLD_ORDER = ("error_rate", "latency_p95_ms", "session_drop_rate")
UPPER_BOUND_RE = re.compile(
    r"^\s*(?:(?:p95)\s*)?(?P<operator><=|≤|<)\s*"
    r"(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>%|ms)?\s*$",
    re.IGNORECASE,
)


class NormalizationError(ValueError):
    pass


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NormalizationError(f"not a decimal value: {value!r}") from exc


def _ratio_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _percent_text_from_ppm(value: int) -> str:
    return _ratio_text(Decimal(value) / Decimal(10000))


def _round_ratio(value: Any) -> float | None:
    if value is None:
        return None
    quantized = _decimal(value).quantize(Decimal("0.000000001"))
    return float(quantized)


def _round_latency(value: Any) -> float | None:
    if value is None:
        return None
    quantized = _decimal(value).quantize(Decimal("0.001"))
    return float(quantized)


def _whole_seconds(value: Any, unit: str) -> int:
    number = _decimal(value)
    if unit == "milliseconds":
        number /= Decimal(1000)
    elif unit == "minutes":
        number *= Decimal(60)
    elif unit != "seconds":
        raise NormalizationError(f"unsupported time unit: {unit}")
    if number != number.to_integral_value():
        raise NormalizationError(f"time does not map to whole seconds: {value!r}")
    return int(number)


def _percent_number(value: Any) -> Decimal:
    return _decimal(value) / Decimal(100)


def _basis_points(value: Any) -> Decimal:
    return _decimal(value) / Decimal(10000)


def _fraction_number(value: Any) -> Decimal:
    return _decimal(value)


def _traffic_ppm_from_percent(value: Any) -> int:
    ppm = _decimal(value) * Decimal(10000)
    if ppm != ppm.to_integral_value():
        raise NormalizationError(f"traffic does not map to whole ppm: {value!r}")
    return int(ppm)


def _traffic_ppm_from_fraction(value: Any) -> int:
    ppm = _decimal(value) * Decimal(1_000_000)
    if ppm != ppm.to_integral_value():
        raise NormalizationError(f"traffic does not map to whole ppm: {value!r}")
    return int(ppm)


def _expression_threshold(expression: str, unit: str) -> Decimal:
    match = UPPER_BOUND_RE.match(expression)
    if not match:
        raise NormalizationError(f"unsupported threshold expression: {expression!r}")
    if match.group("operator") not in ("<=", "≤"):
        raise NormalizationError(
            f"only inclusive upper bounds are supported: {expression!r}"
        )
    raw_unit = (match.group("unit") or "").lower()
    number = _decimal(match.group("number"))
    if unit == "fraction":
        if raw_unit != "%":
            raise NormalizationError(
                f"fraction threshold must use percent expression: {expression!r}"
            )
        return number / Decimal(100)
    if unit == "milliseconds":
        if raw_unit != "ms":
            raise NormalizationError(
                f"latency threshold must use milliseconds: {expression!r}"
            )
        return number
    raise NormalizationError(f"unknown canonical threshold unit: {unit}")


def _provenance(
    source_schema: str,
    source_path: str,
    source_field: str,
    raw_value: Any,
    raw_unit: str,
    normalized_value: Any,
    normalized_unit: str,
    difference_kind: str,
) -> dict[str, Any]:
    return {
        "source_schema": source_schema,
        "source_path": source_path,
        "source_field": source_field,
        "raw_value": raw_value,
        "raw_unit": raw_unit,
        "normalized_value": normalized_value,
        "normalized_unit": normalized_unit,
        "difference_kind": difference_kind,
    }


def _add_step_provenance(
    provenance: dict[str, Any],
    source_schema: str,
    canonical_index: int,
    base_path: str,
    field: str,
    source_field: str,
    raw_value: Any,
    raw_unit: str,
    normalized_value: Any,
    normalized_unit: str,
    difference_kind: str,
) -> None:
    provenance[f"steps[{canonical_index}].{field}"] = _provenance(
        source_schema,
        base_path,
        source_field,
        raw_value,
        raw_unit,
        normalized_value,
        normalized_unit,
        difference_kind,
    )


def _step_record(
    *,
    canonical_index: int,
    step_id: str,
    traffic_ppm: int,
    observation_seconds: int,
    sliding_seconds: int,
    sample_interval_seconds: int,
    missing_ratio: Decimal,
    required_consecutive: int,
    error_threshold: Decimal,
    latency_threshold: Decimal,
    drop_threshold: Decimal,
) -> dict[str, Any]:
    return {
        "step_number": canonical_index,
        "step_id": step_id,
        "traffic_ppm": traffic_ppm,
        "traffic_percent": _percent_text_from_ppm(traffic_ppm),
        "observation_window_seconds": observation_seconds,
        "sliding_window_seconds": sliding_seconds,
        "sample_interval_seconds": sample_interval_seconds,
        "max_missing_ratio": _ratio_text(missing_ratio),
        "required_consecutive_breaches": required_consecutive,
        "thresholds": {
            "operator": "<=",
            "error_rate": _ratio_text(error_threshold),
            "latency_p95_ms": _ratio_text(latency_threshold),
            "session_drop_rate": _ratio_text(drop_threshold),
        },
    }


def _normalize_observations(
    rows: Iterable[dict[str, Any]],
    *,
    source_schema: str,
    time_field: str,
    time_unit: str,
    count_field: str,
    error_field: str,
    latency_field: str,
    drop_field: str,
    error_unit: str,
    drop_unit: str,
    base_path: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_times: set[int] = set()
    for row in rows:
        timestamp = _whole_seconds(row[time_field], time_unit)
        if timestamp in seen_times:
            raise NormalizationError(f"duplicate observation time: {timestamp}")
        seen_times.add(timestamp)

        error_value = row.get(error_field)
        drop_value = row.get(drop_field)
        if error_value is not None and error_unit == "percent":
            error_value = _decimal(error_value) / Decimal(100)
        if drop_value is not None and drop_unit == "percent":
            drop_value = _decimal(drop_value) / Decimal(100)

        canonical = {
            "logical_time_seconds": timestamp,
            "sample_count": int(row[count_field]),
            "error_rate": _round_ratio(error_value),
            "latency_p95_ms": _round_latency(row.get(latency_field)),
            "session_drop_rate": _round_ratio(drop_value),
        }
        if canonical["sample_count"] <= 0:
            raise NormalizationError("observation sample count must be positive")
        result.append(canonical)

    result.sort(key=lambda item: item["logical_time_seconds"])
    provenance = {
        "observations.logical_time_seconds": _provenance(
            source_schema,
            f"{base_path}[].{time_field}",
            time_field,
            f"per-sample {time_unit} value",
            time_unit,
            "non-negative integer seconds",
            "seconds",
            "observation_unit",
        ),
        "observations.sample_count": _provenance(
            source_schema,
            f"{base_path}[].{count_field}",
            count_field,
            "per-sample request count",
            "requests",
            "positive integer",
            "requests",
            "field_name",
        ),
        "observations.error_rate": _provenance(
            source_schema,
            f"{base_path}[].{error_field}",
            error_field,
            "per-sample value",
            error_unit,
            "fraction rounded to 9 decimal places",
            "fraction",
            "observation_unit" if error_unit == "percent" else "field_name",
        ),
        "observations.latency_p95_ms": _provenance(
            source_schema,
            f"{base_path}[].{latency_field}",
            latency_field,
            "per-sample value or null",
            "milliseconds",
            "milliseconds rounded to 3 decimal places",
            "milliseconds",
            "field_name",
        ),
        "observations.session_drop_rate": _provenance(
            source_schema,
            f"{base_path}[].{drop_field}",
            drop_field,
            "per-sample value",
            drop_unit,
            "fraction rounded to 9 decimal places",
            "fraction",
            "observation_unit" if drop_unit == "percent" else "field_name",
        ),
    }
    return result, provenance


def _normalize_v1(raw: dict[str, Any]) -> dict[str, Any]:
    source = "v1"
    provenance: dict[str, Any] = {}
    version = raw["service"]["release"]
    capacity = int(raw["session_capacity"])
    provenance["service_version"] = _provenance(
        source,
        "service.release",
        "release",
        version,
        "version",
        version,
        "version",
        "field_name",
    )
    provenance["session_capacity"] = _provenance(
        source,
        "session_capacity",
        "session_capacity",
        capacity,
        "sessions",
        capacity,
        "sessions",
        "field_name",
    )

    raw_steps = list(raw["rollout"]["steps"])
    raw_steps.sort(key=lambda item: int(item["order"]))
    steps = []
    for canonical_index, raws in enumerate(raw_steps, start=1):
        source_index = next(
            index
            for index, candidate in enumerate(raw["rollout"]["steps"])
            if candidate is raws
        )
        base_path = f"rollout.steps[{source_index}]"
        thresholds = raws["thresholds"]
        traffic_ppm = _traffic_ppm_from_percent(raws["traffic_percent"])
        missing = _fraction_number(raws["max_missing_ratio"])
        error_threshold = _percent_number(thresholds["error_rate_percent"])
        latency_threshold = _decimal(thresholds["latency_p95_ms"])
        drop_threshold = _percent_number(thresholds["session_drop_percent"])
        steps.append(
            _step_record(
                canonical_index=canonical_index,
                step_id=raws["step_id"],
                traffic_ppm=traffic_ppm,
                observation_seconds=int(raws["observe_seconds"]),
                sliding_seconds=int(raws["rolling_seconds"]),
                sample_interval_seconds=int(raws["probe_interval_seconds"]),
                missing_ratio=missing,
                required_consecutive=int(raws["required_consecutive_breaches"]),
                error_threshold=error_threshold,
                latency_threshold=latency_threshold,
                drop_threshold=drop_threshold,
            )
        )
        step = steps[-1]
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "step_id", "step_id",
            raws["step_id"], "identifier", raws["step_id"], "identifier", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "traffic_ppm",
            "traffic_percent", raws["traffic_percent"], "percent", traffic_ppm, "ppm",
            "traffic_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path,
            "observation_window_seconds", "observe_seconds",
            raws["observe_seconds"], "seconds", step["observation_window_seconds"],
            "seconds", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "sliding_window_seconds",
            "rolling_seconds", raws["rolling_seconds"], "seconds",
            step["sliding_window_seconds"], "seconds", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "sample_interval_seconds",
            "probe_interval_seconds", raws["probe_interval_seconds"], "seconds",
            step["sample_interval_seconds"], "seconds", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "max_missing_ratio",
            "max_missing_ratio", raws["max_missing_ratio"], "fraction",
            step["max_missing_ratio"], "fraction", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path,
            "required_consecutive_breaches", "required_consecutive_breaches",
            raws["required_consecutive_breaches"], "observations",
            step["required_consecutive_breaches"], "observations", "field_name",
        )
        for canonical_name, source_name, raw_value, raw_unit in (
            ("error_rate", "error_rate_percent", thresholds["error_rate_percent"], "percent"),
            ("latency_p95_ms", "latency_p95_ms", thresholds["latency_p95_ms"], "milliseconds"),
            ("session_drop_rate", "session_drop_percent", thresholds["session_drop_percent"], "percent"),
        ):
            _add_step_provenance(
                provenance, source, canonical_index, base_path,
                f"thresholds.{canonical_name}", source_name, raw_value, raw_unit,
                step["thresholds"][canonical_name], "fraction" if canonical_name != "latency_p95_ms" else "milliseconds",
                "threshold_unit" if canonical_name != "latency_p95_ms" else "field_name",
            )

    observations, observation_provenance = _normalize_observations(
        raw["telemetry"]["samples"],
        source_schema=source,
        time_field="at_seconds",
        time_unit="seconds",
        count_field="requests",
        error_field="error_rate",
        latency_field="latency_p95_ms",
        drop_field="session_drop_rate",
        error_unit="fraction",
        drop_unit="fraction",
        base_path="telemetry.samples",
    )
    provenance.update(observation_provenance)
    return _project(source, version, capacity, steps, observations, provenance)


def _normalize_v2(raw: dict[str, Any]) -> dict[str, Any]:
    source = "v2"
    provenance: dict[str, Any] = {}
    version = raw["release"]["artifact"]
    capacity = int(raw["capacity"]["sessions"])
    provenance["service_version"] = _provenance(
        source, "release.artifact", "artifact", version, "version",
        version, "version", "field_name",
    )
    provenance["session_capacity"] = _provenance(
        source, "capacity.sessions", "sessions", capacity, "sessions",
        capacity, "sessions", "field_name",
    )

    raw_steps = list(raw["stages"])
    raw_steps.sort(key=lambda item: int(item["sequence"]))
    steps = []
    for canonical_index, raws in enumerate(raw_steps, start=1):
        source_index = next(
            index for index, candidate in enumerate(raw["stages"])
            if candidate is raws
        )
        base_path = f"stages[{source_index}]"
        health = raws["health"]
        traffic_ppm = _traffic_ppm_from_fraction(raws["traffic_fraction"])
        missing = _fraction_number(raws["sampling"]["tolerance"]["missing_fraction"])
        error_threshold = _fraction_number(health["error_rate"])
        latency_threshold = _decimal(health["latency_p95_ms"])
        drop_threshold = _fraction_number(health["disconnect_rate"])
        observation_seconds = _whole_seconds(
            raws["window"]["observe_minutes"], "minutes"
        )
        sliding_seconds = _whole_seconds(
            raws["window"]["slide_minutes"], "minutes"
        )
        steps.append(
            _step_record(
                canonical_index=canonical_index,
                step_id=raws["id"],
                traffic_ppm=traffic_ppm,
                observation_seconds=observation_seconds,
                sliding_seconds=sliding_seconds,
                sample_interval_seconds=int(raws["sampling"]["cadence_s"]),
                missing_ratio=missing,
                required_consecutive=int(raws["sustain"]),
                error_threshold=error_threshold,
                latency_threshold=latency_threshold,
                drop_threshold=drop_threshold,
            )
        )
        step = steps[-1]
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "step_id", "id",
            raws["id"], "identifier", raws["id"], "identifier", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "traffic_ppm",
            "traffic_fraction", raws["traffic_fraction"], "fraction",
            traffic_ppm, "ppm", "traffic_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path,
            "observation_window_seconds", "window.observe_minutes",
            raws["window"]["observe_minutes"], "minutes", observation_seconds,
            "seconds", "duration_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "sliding_window_seconds",
            "window.slide_minutes", raws["window"]["slide_minutes"], "minutes",
            sliding_seconds, "seconds", "duration_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "sample_interval_seconds",
            "sampling.cadence_s", raws["sampling"]["cadence_s"], "seconds",
            step["sample_interval_seconds"], "seconds", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "max_missing_ratio",
            "sampling.tolerance.missing_fraction",
            raws["sampling"]["tolerance"]["missing_fraction"], "fraction",
            step["max_missing_ratio"], "fraction", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path,
            "required_consecutive_breaches", "sustain", raws["sustain"],
            "observations", step["required_consecutive_breaches"],
            "observations", "field_name",
        )
        for canonical_name, source_name, raw_value, raw_unit in (
            ("error_rate", "health.error_rate", health["error_rate"], "fraction"),
            ("latency_p95_ms", "health.latency_p95_ms", health["latency_p95_ms"], "milliseconds"),
            ("session_drop_rate", "health.disconnect_rate", health["disconnect_rate"], "fraction"),
        ):
            _add_step_provenance(
                provenance, source, canonical_index, base_path,
                f"thresholds.{canonical_name}", source_name, raw_value, raw_unit,
                step["thresholds"][canonical_name],
                "milliseconds" if canonical_name == "latency_p95_ms" else "fraction",
                "field_name",
            )

    observations, observation_provenance = _normalize_observations(
        raw["observations"]["samples"],
        source_schema=source,
        time_field="at_ms",
        time_unit="milliseconds",
        count_field="n",
        error_field="err",
        latency_field="lat_p95_ms",
        drop_field="drop",
        error_unit="fraction",
        drop_unit="fraction",
        base_path="observations.samples",
    )
    provenance.update(observation_provenance)
    return _project(source, version, capacity, steps, observations, provenance)


def _normalize_v3(raw: dict[str, Any]) -> dict[str, Any]:
    source = "v3"
    provenance: dict[str, Any] = {}
    version = raw["artifact_version"]
    capacity = int(raw["session_pool_size"])
    provenance["service_version"] = _provenance(
        source, "artifact_version", "artifact_version", version, "version",
        version, "version", "field_name",
    )
    provenance["session_capacity"] = _provenance(
        source, "session_pool_size", "session_pool_size", capacity, "sessions",
        capacity, "sessions", "field_name",
    )

    raw_steps = list(raw["plan"]["traffic_steps"])
    raw_steps.sort(key=lambda item: int(item["phase"]))
    steps = []
    for canonical_index, raws in enumerate(raw_steps, start=1):
        source_index = next(
            index for index, candidate in enumerate(raw["plan"]["traffic_steps"])
            if candidate is raws
        )
        base_path = f"plan.traffic_steps[{source_index}]"
        expressions = raws["threshold_expr"]
        traffic_ppm = int(raws["traffic_ppm"])
        missing = _basis_points(raws["drop_limit_bp"])
        error_threshold = _expression_threshold(expressions["error"], "fraction")
        latency_threshold = _expression_threshold(
            expressions["latency"], "milliseconds"
        )
        drop_threshold = _expression_threshold(
            expressions["session_drop"], "fraction"
        )
        steps.append(
            _step_record(
                canonical_index=canonical_index,
                step_id=raws["label"],
                traffic_ppm=traffic_ppm,
                observation_seconds=_whole_seconds(
                    raws["observe_ms"], "milliseconds"
                ),
                sliding_seconds=_whole_seconds(
                    raws["rolling_ms"], "milliseconds"
                ),
                sample_interval_seconds=_whole_seconds(
                    raws["sample_ms"], "milliseconds"
                ),
                missing_ratio=missing,
                required_consecutive=int(raws["sustain_points"]),
                error_threshold=error_threshold,
                latency_threshold=latency_threshold,
                drop_threshold=drop_threshold,
            )
        )
        step = steps[-1]
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "step_id", "label",
            raws["label"], "identifier", raws["label"], "identifier", "field_name",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "traffic_ppm",
            "traffic_ppm", raws["traffic_ppm"], "ppm", traffic_ppm, "ppm",
            "traffic_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path,
            "observation_window_seconds", "observe_ms", raws["observe_ms"],
            "milliseconds", step["observation_window_seconds"], "seconds",
            "duration_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "sliding_window_seconds",
            "rolling_ms", raws["rolling_ms"], "milliseconds",
            step["sliding_window_seconds"], "seconds", "duration_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "sample_interval_seconds",
            "sample_ms", raws["sample_ms"], "milliseconds",
            step["sample_interval_seconds"], "seconds", "duration_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path, "max_missing_ratio",
            "drop_limit_bp", raws["drop_limit_bp"], "basis_points",
            step["max_missing_ratio"], "fraction", "missing_ratio_unit",
        )
        _add_step_provenance(
            provenance, source, canonical_index, base_path,
            "required_consecutive_breaches", "sustain_points", raws["sustain_points"],
            "observations", step["required_consecutive_breaches"],
            "observations", "field_name",
        )
        for canonical_name, expression_name, expression in (
            ("error_rate", "threshold_expr.error", expressions["error"]),
            ("latency_p95_ms", "threshold_expr.latency", expressions["latency"]),
            ("session_drop_rate", "threshold_expr.session_drop", expressions["session_drop"]),
        ):
            _add_step_provenance(
                provenance, source, canonical_index, base_path,
                f"thresholds.{canonical_name}", expression_name, expression,
                "expression", step["thresholds"][canonical_name],
                "milliseconds" if canonical_name == "latency_p95_ms" else "fraction",
                "threshold_expression",
            )

    observations, observation_provenance = _normalize_observations(
        raw["plan"]["metric_stream"],
        source_schema=source,
        time_field="ts_ms",
        time_unit="milliseconds",
        count_field="samples",
        error_field="error_pct",
        latency_field="latency_p95",
        drop_field="drop_pct",
        error_unit="percent",
        drop_unit="percent",
        base_path="plan.metric_stream",
    )
    provenance.update(observation_provenance)
    return _project(source, version, capacity, steps, observations, provenance)


def _project(
    source_schema: str,
    service_version: str,
    session_capacity: int,
    steps: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    _validate_steps(steps)
    return {
        "canonical_schema": "gsb.release.normalized/v1",
        "source_schema": source_schema,
        "service_version": service_version,
        "session_capacity": session_capacity,
        "traffic_unit": "ppm",
        "threshold_operator": "<=",
        "steps": steps,
        "observations": observations,
        "provenance": provenance,
    }


def _validate_steps(steps: list[dict[str, Any]]) -> None:
    if not steps:
        raise NormalizationError("release plan has no steps")
    previous_ppm = -1
    for expected, step in enumerate(steps, start=1):
        if step["step_number"] != expected:
            raise NormalizationError("step numbering is not contiguous")
        if not 0 <= step["traffic_ppm"] <= 1_000_000:
            raise NormalizationError("traffic ppm outside 0..1,000,000")
        if step["traffic_ppm"] < previous_ppm:
            raise NormalizationError("traffic must not decrease between steps")
        previous_ppm = step["traffic_ppm"]
        if step["observation_window_seconds"] <= 0:
            raise NormalizationError("observation window must be positive")
        if step["sliding_window_seconds"] <= 0:
            raise NormalizationError("sliding window must be positive")
        if step["sliding_window_seconds"] > step["observation_window_seconds"]:
            raise NormalizationError("sliding window exceeds observation window")
        if step["sample_interval_seconds"] <= 0:
            raise NormalizationError("sample interval must be positive")
        if (
            step["observation_window_seconds"]
            % step["sample_interval_seconds"]
            != 0
        ):
            raise NormalizationError(
                "observation window must be a whole number of sample intervals"
            )
        if not 0 <= Decimal(step["max_missing_ratio"]) <= 1:
            raise NormalizationError("missing ratio outside 0..1")
        if step["required_consecutive_breaches"] <= 0:
            raise NormalizationError("consecutive breach count must be positive")


def normalize_release(raw: dict[str, Any]) -> dict[str, Any]:
    schema_name = raw.get("schema")
    if schema_name not in SCHEMA_IDS:
        raise NormalizationError(f"unsupported release schema: {schema_name!r}")
    source = SCHEMA_IDS[schema_name]
    if source == "v1":
        return _normalize_v1(raw)
    if source == "v2":
        return _normalize_v2(raw)
    return _normalize_v3(raw)


def normalize_file(path: Path) -> dict[str, Any]:
    return normalize_release(read_json(path))

