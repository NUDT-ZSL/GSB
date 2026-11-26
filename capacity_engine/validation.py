from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .errors import CapacityError, ErrorCode
from .hashing import canonical_dumps, sha256_object


METRIC_NAMES = (
    "cpu_utilization",
    "memory_utilization",
    "latency_p95",
    "throughput_attainment",
)
UPPER_BETTER_METRICS = {"throughput_attainment"}
LOWER_IS_BETTER_METRICS = {
    "cpu_utilization",
    "memory_utilization",
    "latency_p95",
}


def is_real_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal)):
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False
    return False


def parse_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or not is_real_number(value) and not isinstance(value, str):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal number") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def require_nonnegative_number(value: Any, field_name: str, issues: list[dict[str, Any]]) -> Decimal | None:
    if isinstance(value, str):
        try:
            parsed = parse_decimal(value, field_name)
        except ValueError as exc:
            issues.append({"field": field_name, "value": value, "reason": str(exc)})
            return None
    elif is_real_number(value):
        try:
            parsed = parse_decimal(value, field_name)
        except ValueError as exc:
            issues.append({"field": field_name, "value": value, "reason": str(exc)})
            return None
    else:
        issues.append({"field": field_name, "value": value, "reason": "must be a non-negative finite number"})
        return None
    if parsed < 0:
        issues.append({"field": field_name, "value": value, "reason": "must be non-negative"})
        return None
    return parsed


def require_positive_int(value: Any, field_name: str, issues: list[dict[str, Any]]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        issues.append({"field": field_name, "value": value, "reason": "must be a positive integer"})
        return None
    return value


def require_nonnegative_int(value: Any, field_name: str, issues: list[dict[str, Any]]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        issues.append({"field": field_name, "value": value, "reason": "must be a non-negative integer"})
        return None
    return value


def parse_bool_marker(value: Any, field_name: str, issues: list[dict[str, Any]]) -> bool | None:
    if isinstance(value, bool):
        return value
    allowed_true = {"SUCCESS", "success", "OK", "true", "TRUE", "1", 1}
    allowed_false = {"FAILURE", "failure", "FAILED", "failed", "ERROR", "error", "false", "FALSE", "0", 0}
    if value in allowed_true:
        return True
    if value in allowed_false:
        return False
    issues.append(
        {
            "field": field_name,
            "value": value,
            "reason": "must be one of SUCCESS/FAILURE or a boolean",
        }
    )
    return None


def parse_drop_marker(value: Any, field_name: str, issues: list[dict[str, Any]]) -> bool | None:
    if isinstance(value, bool):
        return value
    dropped = {"DROPPED", "dropped", "true", "TRUE", "1", 1}
    not_dropped = {"NOT_DROPPED", "not_dropped", "false", "FALSE", "0", 0}
    if value in dropped:
        return True
    if value in not_dropped:
        return False
    issues.append(
        {
            "field": field_name,
            "value": value,
            "reason": "must be one of DROPPED/NOT_DROPPED or a boolean",
        }
    )
    return None


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_float=Decimal)
    except FileNotFoundError as exc:
        raise CapacityError(ErrorCode.INPUT_NOT_FOUND, f"input file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CapacityError(ErrorCode.VALIDATION_SCENARIO, f"invalid JSON in {path}: {exc}") from exc


def read_traces(path: Path) -> list[dict[str, Any]]:
    if path.with_suffix(".jsonl").exists():
        path = path.with_suffix(".jsonl")
    if not path.exists():
        alt = path.parent / "traces.json"
        if alt.exists():
            payload = read_json(alt)
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise CapacityError(ErrorCode.VALIDATION_TRACE, "traces.json must contain records list")
            return payload["records"]
        raise CapacityError(ErrorCode.INPUT_NOT_FOUND, f"trace file not found: {path}")
    records: list[dict[str, Any]] = []
    try:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line, parse_float=Decimal)
            except json.JSONDecodeError as exc:
                raise CapacityError(ErrorCode.VALIDATION_TRACE, f"invalid JSONL at line {line_no}: {exc}") from exc
            if not isinstance(item, dict):
                raise CapacityError(ErrorCode.VALIDATION_TRACE, f"trace line {line_no} must be an object")
            item["_line_no"] = line_no
            records.append(item)
    except OSError as exc:
        raise CapacityError(ErrorCode.INPUT_NOT_FOUND, f"cannot read traces: {exc}") from exc
    return records


def first_present(record: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


@dataclass
class CapacityInput:
    root: Path
    scenario: dict[str, Any]
    traces: list[dict[str, Any]]
    metric_definitions: dict[str, Any]
    v1: dict[str, Any]
    v2_shards: list[dict[str, Any]]
    v2_metadata: dict[str, list[dict[str, Any]]]
    patches: list[dict[str, Any]]
    anomalies: dict[str, Any]

    @property
    def origin_ms(self) -> int:
        return int(self.scenario.get("logical_clock_origin_ms", 0))


def _raise_issues(code: str, message: str, issues: list[dict[str, Any]]) -> None:
    if issues:
        raise CapacityError(code, message, {"issues": issues})


def validate_scenario(payload: Any) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        raise CapacityError(ErrorCode.VALIDATION_SCENARIO, "scenario must be a JSON object")
    require_nonnegative_int(
        payload.get("logical_clock_origin_ms", 0),
        "logical_clock_origin_ms",
        issues,
    )
    services = payload.get("services")
    if not isinstance(services, list) or not services:
        issues.append({"field": "services", "reason": "must be a non-empty list"})
        _raise_issues(ErrorCode.VALIDATION_SCENARIO, "invalid capacity scenario", issues)

    seen_names: set[str] = set()
    seen_instances: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for index, service in enumerate(services):
        prefix = f"services[{index}]"
        if not isinstance(service, dict):
            issues.append({"field": prefix, "reason": "must be an object"})
            continue
        name = service.get("service_name", service.get("name"))
        instance_id = service.get("instance_id")
        level = service.get("service_level")
        expected_rate = service.get("expected_request_rate_per_second", service.get("expected_rate_rps"))
        if not isinstance(name, str) or not name:
            issues.append({"field": f"{prefix}.service_name", "value": name, "reason": "must be a non-empty string"})
        elif name in seen_names:
            issues.append({"field": f"{prefix}.service_name", "value": name, "reason": "service_name must be unique"})
        else:
            seen_names.add(name)
        if not isinstance(instance_id, str) or not instance_id:
            issues.append({"field": f"{prefix}.instance_id", "value": instance_id, "reason": "must be a non-empty string"})
        elif instance_id in seen_instances:
            issues.append({"field": f"{prefix}.instance_id", "value": instance_id, "reason": "instance_id must be unique"})
        else:
            seen_instances.add(instance_id)
        if not isinstance(level, str) or not level:
            issues.append({"field": f"{prefix}.service_level", "value": level, "reason": "must be a non-empty string"})
        rate = require_nonnegative_number(expected_rate, f"{prefix}.expected_request_rate_per_second", issues)
        if rate is not None and rate == 0:
            issues.append(
                {"field": f"{prefix}.expected_request_rate_per_second", "value": expected_rate, "reason": "must be positive for throughput attainment"}
            )
        if name and instance_id:
            pair = (name, instance_id)
            if pair in seen_pairs:
                issues.append({"field": f"{prefix}", "reason": "service_name/instance_id pair must be unique"})
            seen_pairs.add(pair)
        normalized.append(
            {
                "service_name": name,
                "instance_id": instance_id,
                "service_level": level,
                "expected_rate_rps": rate,
            }
        )
    _raise_issues(ErrorCode.VALIDATION_SCENARIO, "invalid capacity scenario", issues)
    payload["services"] = normalized
    return payload


def validate_traces(records: list[dict[str, Any]], scenario: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    service_index = {
        (item["service_name"], item["instance_id"]): item for item in scenario["services"]
    }
    if not records:
        issues.append({"field": "traces", "reason": "must contain at least one record"})
    seen_seq: set[int] = set()
    normalized: list[dict[str, Any]] = []
    for raw in records:
        line = raw.get("_line_no", "?")
        seq = first_present(raw, ("request_seq", "seq", "request_sequence"))
        name = first_present(raw, ("service_name", "service"))
        instance_id = first_present(raw, ("instance_id", "instance"))
        clock = first_present(raw, ("logical_clock_ms", "ts_ms", "timestamp_ms", "clock_ms"))
        duration = first_present(raw, ("duration_ms", "duration", "latency_ms"))
        success_raw = first_present(raw, ("success", "success_flag", "is_success"))
        dropped_raw = first_present(raw, ("dropped", "drop_flag", "is_dropped"))

        seq_int = require_nonnegative_int(seq, f"traces[{line}].request_seq", issues)
        if seq_int is not None and seq_int in seen_seq:
            issues.append({"field": f"traces[{line}].request_seq", "value": seq, "reason": "request_seq must be unique"})
        elif seq_int is not None:
            seen_seq.add(seq_int)
        if not isinstance(name, str) or not name:
            issues.append({"field": f"traces[{line}].service_name", "value": name, "reason": "must be a non-empty string"})
        if not isinstance(instance_id, str) or not instance_id:
            issues.append({"field": f"traces[{line}].instance_id", "value": instance_id, "reason": "must be a non-empty string"})
        if name and instance_id and (name, instance_id) not in service_index:
            issues.append({"field": f"traces[{line}]", "reason": f"unknown service/instance {name}/{instance_id}"})
        clock_int = require_nonnegative_int(clock, f"traces[{line}].logical_clock_ms", issues)
        duration_dec = require_nonnegative_number(duration, f"traces[{line}].duration_ms", issues)
        success = parse_bool_marker(success_raw, f"traces[{line}].success", issues)
        dropped = parse_drop_marker(dropped_raw, f"traces[{line}].dropped", issues)
        if success is not None and dropped is not None and dropped and success:
            issues.append({"field": f"traces[{line}]", "reason": "a dropped request cannot simultaneously be marked successful"})
        if not issues or seq_int is not None:
            normalized.append(
                {
                    "request_seq": seq_int,
                    "service_name": name,
                    "instance_id": instance_id,
                    "logical_clock_ms": clock_int,
                    "duration_ms": duration_dec,
                    "success": bool(success) if success is not None else False,
                    "dropped": bool(dropped) if dropped is not None else False,
                }
            )
    _raise_issues(ErrorCode.VALIDATION_TRACE, "invalid request trace stream", issues)
    normalized.sort(key=lambda item: (item["logical_clock_ms"], item["request_seq"]))
    return normalized


def validate_metric_definitions(payload: Any) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        raise CapacityError(ErrorCode.VALIDATION_METRIC_DEFINITION, "metric definitions must be an object")
    require_positive_int(payload.get("window_length_ms"), "window_length_ms", issues)
    require_positive_int(payload.get("slide_ms"), "slide_ms", issues)
    if payload.get("window_length_ms") and payload.get("slide_ms"):
        if payload["slide_ms"] > payload["window_length_ms"]:
            issues.append({"field": "slide_ms", "reason": "slide_ms must not exceed window_length_ms in exclusive ownership mode"})
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        issues.append({"field": "metrics", "reason": "must be an object keyed by metric name"})
        metrics = {}
    for metric_name in METRIC_NAMES:
        spec = metrics.get(metric_name)
        if not isinstance(spec, dict):
            issues.append({"field": f"metrics.{metric_name}", "reason": "metric conversion definition is required"})
            continue
        require_positive_int(spec.get("scale"), f"metrics.{metric_name}.scale", issues)
        rounding = spec.get("rounding", "half_up")
        if rounding not in {"half_up", "half_even", "floor", "down", "ceil", "up"}:
            issues.append({"field": f"metrics.{metric_name}.rounding", "value": rounding, "reason": "unsupported rounding direction"})
        for key in ("pair_tolerance", "threshold_margin_tolerance"):
            if key in spec:
                require_nonnegative_number(spec[key], f"metrics.{metric_name}.{key}", issues)
        for key, value in spec.items():
            if key.endswith(("_coefficient", "_percent", "_per_request", "_ms")) and isinstance(value, (int, float, Decimal)):
                require_nonnegative_number(value, f"metrics.{metric_name}.{key}", issues)
        if "higher_scale_multiplier" in spec:
            require_positive_int(spec["higher_scale_multiplier"], f"metrics.{metric_name}.higher_scale_multiplier", issues)
    _raise_issues(ErrorCode.VALIDATION_METRIC_DEFINITION, "invalid metric definitions", issues)
    return payload


def _validate_threshold_map(thresholds: Any, prefix: str, issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(thresholds, dict) or not thresholds:
        issues.append({"field": f"{prefix}.thresholds", "reason": "must be a non-empty flat object"})
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, raw in thresholds.items():
        if not isinstance(key, str) or "." not in key:
            issues.append({"field": f"{prefix}.thresholds.{key}", "reason": "threshold key must be <service_level>.<metric>"})
            continue
        level, metric = key.split(".", 1)
        if not level or metric not in METRIC_NAMES:
            issues.append({"field": f"{prefix}.thresholds.{key}", "reason": f"metric must be one of {METRIC_NAMES}"})
        value = raw.get("value") if isinstance(raw, dict) else raw
        decimal_value = require_nonnegative_number(value, f"{prefix}.thresholds.{key}", issues)
        operator = "max" if metric not in UPPER_BETTER_METRICS else "min"
        if isinstance(raw, dict) and raw.get("operator") is not None:
            operator = raw["operator"]
            if operator not in {"max", "min", "lt", "lte", "gt", "gte"}:
                issues.append({"field": f"{prefix}.thresholds.{key}.operator", "value": operator, "reason": "invalid operator"})
        normalized[key] = {"service_level": level, "metric": metric, "value": decimal_value, "operator": operator}
    return normalized


def validate_v1(payload: Any) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        raise CapacityError(ErrorCode.VALIDATION_V1, "v1 threshold snapshot must be an object")
    require_nonnegative_int(payload.get("effective_after_seconds"), "effective_after_seconds", issues)
    if "logical_clock_ms" in payload:
        require_nonnegative_int(payload["logical_clock_ms"], "logical_clock_ms", issues)
    if "generation" in payload:
        require_positive_int(payload["generation"], "generation", issues)
    normalized_thresholds = _validate_threshold_map(payload.get("thresholds"), "v1", issues)
    _raise_issues(ErrorCode.VALIDATION_V1, "invalid v1 threshold snapshot", issues)
    payload["normalized_thresholds"] = normalized_thresholds
    return payload


def shard_content_hash(shard: dict[str, Any]) -> str:
    clone = dict(shard)
    clone.pop("sha256", None)
    return sha256_object(clone)


def validate_v2(shards: list[dict[str, Any]], metadata: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not shards:
        raise CapacityError(ErrorCode.VALIDATION_V2_METADATA, "at least one v2 shard is required")
    ordered = sorted(shards, key=lambda item: (item.get("shard_index", 0), item.get("shard_id", "")))
    previous_generation = 0
    previous_effective = -1
    for index, shard in enumerate(ordered):
        prefix = f"v2_shards[{index}]"
        if not isinstance(shard, dict):
            issues.append({"field": prefix, "reason": "shard must be an object"})
            continue
        declared_hash = shard.get("sha256")
        actual_hash = shard_content_hash(shard)
        if declared_hash != actual_hash:
            raise CapacityError(
                ErrorCode.VALIDATION_V2_SHARD_HASH,
                "v2 shard canonical hash mismatch",
                {"shard_id": shard.get("shard_id"), "declared": declared_hash, "actual": actual_hash},
            )
        generation = require_positive_int(shard.get("generation"), f"{prefix}.generation", issues)
        effective = require_nonnegative_int(shard.get("effective_at_ms"), f"{prefix}.effective_at_ms", issues)
        require_nonnegative_int(shard.get("logical_clock_ms", effective), f"{prefix}.logical_clock_ms", issues)
        shard["normalized_thresholds"] = _validate_threshold_map(shard.get("thresholds", {}), prefix, issues)
        if generation is not None:
            if generation < previous_generation:
                raise CapacityError(
                    ErrorCode.VALIDATION_V2_GENERATION,
                    "v2 shard generations must be non-decreasing",
                    {"shard_id": shard.get("shard_id"), "previous_generation": previous_generation, "generation": generation},
                )
            previous_generation = generation
        if effective is not None and effective < previous_effective:
            issues.append({"field": f"{prefix}.effective_at_ms", "reason": "effective_at_ms must be non-decreasing"})
            previous_effective = effective
        elif effective is not None:
            previous_effective = effective
        for dep_index, dep in enumerate(shard.get("dependencies", [])):
            if not isinstance(dep, dict) or not dep.get("service_level") or not dep.get("depends_on_service_level"):
                issues.append({"field": f"{prefix}.dependencies[{dep_index}]", "reason": "dependency requires service_level and depends_on_service_level"})
        for tomb_index, tomb in enumerate(shard.get("tombstones", [])):
            if not isinstance(tomb, dict) or not tomb.get("service_level") or tomb.get("metric") not in METRIC_NAMES:
                issues.append({"field": f"{prefix}.tombstones[{tomb_index}]", "reason": "tombstone requires service_level and known metric"})
    meta_shards = metadata.get("shards", [])
    if len(meta_shards) != len(ordered):
        issues.append({"field": "v2_metadata.shards", "reason": "metadata shard count does not match JSON shard count"})
    meta_by_id = {row.get("shard_id"): row for row in meta_shards if isinstance(row, dict)}
    for shard in ordered:
        row = meta_by_id.get(shard.get("shard_id"))
        if row is None:
            issues.append({"field": "v2_metadata.shards", "shard_id": shard.get("shard_id"), "reason": "missing metadata row"})
            continue
        if row.get("sha256") != shard.get("sha256"):
            issues.append({"field": "v2_metadata.sha256", "shard_id": shard.get("shard_id"), "reason": "metadata hash does not match shard"})
        for field_name in ("generation", "effective_at_ms", "logical_clock_ms"):
            if row.get(field_name) != shard.get(field_name, shard.get("effective_at_ms") if field_name == "logical_clock_ms" else None):
                if field_name == "logical_clock_ms" and row.get(field_name) == shard.get("logical_clock_ms", shard.get("effective_at_ms")):
                    continue
                issues.append({"field": f"v2_metadata.{field_name}", "shard_id": shard.get("shard_id"), "reason": "metadata value does not match shard"})
    _raise_issues(ErrorCode.VALIDATION_V2_METADATA, "invalid v2 metadata", issues)
    return ordered


def read_v2_metadata(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    if not db_path.exists():
        raise CapacityError(ErrorCode.INPUT_NOT_FOUND, f"v2 metadata database not found: {db_path}")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            result: dict[str, list[dict[str, Any]]] = {}
            for table in ("shards", "dependencies", "tombstones"):
                if table not in tables:
                    raise CapacityError(ErrorCode.VALIDATION_V2_METADATA, f"v2 metadata missing table {table}")
                result[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            return result
    except sqlite3.Error as exc:
        raise CapacityError(ErrorCode.VALIDATION_V2_METADATA, f"cannot read v2 metadata SQLite: {exc}") from exc


def validate_patches(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    issues: list[dict[str, Any]] = []
    if not isinstance(payload, dict) or not isinstance(payload.get("patches"), list):
        raise CapacityError(ErrorCode.VALIDATION_PATCH, "patches must contain a patches list")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, patch in enumerate(payload["patches"]):
        prefix = f"patches[{index}]"
        if not isinstance(patch, dict):
            issues.append({"field": prefix, "reason": "must be an object"})
            continue
        patch_id = patch.get("patch_id")
        if not isinstance(patch_id, str) or not patch_id:
            issues.append({"field": f"{prefix}.patch_id", "value": patch_id, "reason": "must be non-empty string"})
        elif patch_id in seen:
            issues.append({"field": f"{prefix}.patch_id", "value": patch_id, "reason": "patch_id must be unique"})
        else:
            seen.add(patch_id)
        require_nonnegative_int(patch.get("logical_clock_ms"), f"{prefix}.logical_clock_ms", issues)
        kind = patch.get("kind")
        if kind not in {"threshold_update", "dependency_change", "tombstone", "metric_parameter_update"}:
            issues.append({"field": f"{prefix}.kind", "value": kind, "reason": "unsupported patch kind"})
        target = patch.get("target", {})
        if kind in {"threshold_update", "tombstone"}:
            if not isinstance(target, dict) or not target.get("service_level") or target.get("metric") not in METRIC_NAMES:
                issues.append({"field": f"{prefix}.target", "reason": "target requires service_level and known metric"})
        if kind == "threshold_update":
            value = None
            if isinstance(patch.get("threshold"), dict):
                value = patch["threshold"].get("value")
            else:
                value = patch.get("value")
            require_nonnegative_number(value, f"{prefix}.threshold.value", issues)
        if kind == "dependency_change" and not isinstance(patch.get("dependency"), dict):
            issues.append({"field": f"{prefix}.dependency", "reason": "dependency_change requires dependency object"})
        if kind == "metric_parameter_update":
            metric = patch.get("metric")
            if metric not in METRIC_NAMES:
                issues.append({"field": f"{prefix}.metric", "reason": "known metric is required"})
            params = patch.get("parameters", {})
            if not isinstance(params, dict) or "scale" not in params:
                issues.append({"field": f"{prefix}.parameters.scale", "reason": "scale update requires parameters.scale"})
            else:
                require_positive_int(params["scale"], f"{prefix}.parameters.scale", issues)
        normalized.append(patch)
    _raise_issues(ErrorCode.VALIDATION_PATCH, "invalid patch list", issues)
    return normalized


def load_input(root: str | Path) -> CapacityInput:
    root = Path(root)
    if not root.exists():
        raise CapacityError(ErrorCode.INPUT_NOT_FOUND, f"input directory not found: {root}")
    scenario = validate_scenario(read_json(root / "scenario.json"))
    traces = validate_traces(read_traces(root / "traces.jsonl"), scenario)
    metrics = validate_metric_definitions(read_json(root / "metric_definitions.json"))
    v1 = validate_v1(read_json(root / "thresholds" / "v1.json"))
    v2_dir = root / "thresholds" / "v2"
    shard_paths = sorted(v2_dir.glob("shard-*.json"))
    shards = [read_json(path) for path in shard_paths]
    metadata = read_v2_metadata(v2_dir / "metadata.sqlite")
    v2_shards = validate_v2(shards, metadata)
    patches_path = root / "patches.json"
    patches = validate_patches(read_json(patches_path)) if patches_path.exists() else []
    anomalies_path = root / "anomalies.json"
    anomalies = read_json(anomalies_path) if anomalies_path.exists() else {}
    return CapacityInput(root, scenario, traces, metrics, v1, v2_shards, metadata, patches, anomalies)
