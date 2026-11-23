"""Offline statistics snapshots, dependency projections and online accumulation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import E_FORMAT_INVALID, fail
from .hashing import canonical_json, read_json, sha256_obj
from .models import FeatureSpec, Sample


@dataclass(frozen=True)
class LoadedStatistics:
    version: int
    payload: dict[str, Any]
    checksum: str
    source_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "source_name": self.source_name,
            "payload": self.payload,
        }

    def series_version(self, path: str) -> int:
        versions = self.payload.get("series_versions", {})
        if not isinstance(versions, dict):
            return self.version
        if path in versions:
            try:
                return int(versions[path])
            except (TypeError, ValueError):
                return self.version
        if "." in path:
            parent = path.rsplit(".", 1)[0]
            if parent in versions:
                try:
                    return int(versions[parent])
                except (TypeError, ValueError):
                    return self.version
        return int(versions.get("global", self.version))

    def global_rate(self, metric: str) -> float:
        global_block = self.payload.get("global", {})
        item = global_block.get(metric, {})
        return _rate(item.get("prior"), field=f"global.{metric}.prior")

    def global_smoothing(self, metric: str) -> float:
        global_block = self.payload.get("global", {})
        item = global_block.get(metric, {})
        return _nonnegative(
            item.get("smoothing", 0.0), field=f"global.{metric}.smoothing"
        )

    def numeric_entry(self, feature: str) -> dict[str, Any]:
        return _mapping(self.payload.get("numeric", {}).get(feature))

    def categorical_entry(self, feature: str, value: str) -> dict[str, Any]:
        block = _mapping(self.payload.get("categorical", {}).get(feature))
        specific = block.get(str(value))
        if isinstance(specific, dict):
            return specific
        aggregate = block.get("__ALL__")
        if isinstance(aggregate, dict):
            return aggregate
        return {}

    def categorical_feature_entry(self, feature: str) -> dict[str, Any]:
        block = _mapping(self.payload.get("categorical", {}).get(feature))
        aggregate = block.get("__ALL__")
        if isinstance(aggregate, dict):
            return aggregate
        merged: dict[str, Any] = {}
        for key, value in block.items():
            if key != "__ALL__" and isinstance(value, dict):
                for leaf, leaf_value in value.items():
                    if isinstance(leaf_value, (int, float)):
                        merged[leaf] = merged.get(leaf, 0.0) + float(leaf_value)
        counts = merged.get("count")
        if counts:
            merged["click_rate"] = merged.get("click_count", 0.0) / counts
            merged["convert_rate"] = merged.get("convert_count", 0.0) / counts
        return merged

    def cross_entry(self, feature: str) -> dict[str, Any]:
        return _mapping(self.payload.get("cross", {}).get(feature))


def load_statistics(path: str | Path) -> LoadedStatistics:
    source = Path(path)
    payload = read_json(source)
    return parse_statistics(payload, source_name=str(source))


def parse_statistics(payload: Any, *, source_name: str = "<memory>") -> LoadedStatistics:
    if not isinstance(payload, dict):
        fail(
            E_FORMAT_INVALID,
            "statistics snapshot must be a JSON object",
            line=1,
            field="$",
            source=source_name,
        )
    if "stats_version" not in payload:
        fail(
            E_FORMAT_INVALID,
            "required field 'stats_version' is missing",
            line=1,
            field="stats_version",
            source=source_name,
        )
    raw_version = payload["stats_version"]
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        fail(
            E_FORMAT_INVALID,
            "stats_version must be an integer",
            line=1,
            field="stats_version",
            source=source_name,
        )
    if raw_version < 1:
        fail(
            E_FORMAT_INVALID,
            "stats_version must be positive",
            line=1,
            field="stats_version",
            source=source_name,
        )
    if not isinstance(payload.get("global"), dict):
        fail(
            E_FORMAT_INVALID,
            "statistics snapshot requires a global object",
            line=1,
            field="global",
            source=source_name,
        )
    for metric in ("ctr", "cvr"):
        block = payload["global"].get(metric)
        if not isinstance(block, dict):
            fail(
                E_FORMAT_INVALID,
                f"global statistics for {metric} are missing",
                line=1,
                field=f"global.{metric}",
                source=source_name,
            )
        _rate(block.get("prior"), field=f"global.{metric}.prior")
        _nonnegative(
            block.get("smoothing", 0.0), field=f"global.{metric}.smoothing"
        )
    for section in ("numeric", "categorical", "cross"):
        if not isinstance(payload.get(section, {}), dict):
            fail(
                E_FORMAT_INVALID,
                f"statistics section {section!r} must be an object",
                line=1,
                field=section,
                source=source_name,
            )
    _validate_rates(payload)
    checksum = sha256_obj(payload)
    return LoadedStatistics(
        version=raw_version,
        payload=payload,
        checksum=checksum,
        source_name=source_name,
    )


def _validate_rates(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if key in {"click_rate", "convert_rate", "prior"}:
                _rate(item, field=child)
            elif key in {"smoothing"}:
                _nonnegative(item, field=child)
            else:
                _validate_rates(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_rates(item, f"{path}[{index}]")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rate(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        fail(E_FORMAT_INVALID, f"{field} must be numeric", line=1, field=field)
    try:
        number = float(value)
    except (TypeError, ValueError):
        fail(E_FORMAT_INVALID, f"{field} is not numeric", line=1, field=field)
    if not math.isfinite(number) or not 0.0 < number < 1.0:
        fail(
            E_FORMAT_INVALID,
            f"{field} must be strictly between zero and one",
            line=1,
            field=field,
        )
    return number


def _nonnegative(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        fail(E_FORMAT_INVALID, f"{field} must be numeric", line=1, field=field)
    try:
        number = float(value)
    except (TypeError, ValueError):
        fail(E_FORMAT_INVALID, f"{field} is not numeric", line=1, field=field)
    if not math.isfinite(number) or number < 0.0:
        fail(
            E_FORMAT_INVALID,
            f"{field} must be finite and non-negative",
            line=1,
            field=field,
        )
    return number


def statistics_dependency_projection(
    stats: LoadedStatistics,
    spec: FeatureSpec,
    sample: Sample,
) -> tuple[dict[str, Any], str]:
    """Return only the statistics slice used by one sample.

    Per-series versions let unchanged samples reuse score products even when a
    new snapshot version changes a different feature series.
    """

    numeric = {
        item.name: stats.numeric_entry(item.name)
        for item in spec.numeric
        if item.name in sample.numeric_feats
    }
    categorical = {
        item.name: stats.categorical_entry(
            item.name, sample.categorical_feats.get(item.name, "")
        )
        for item in spec.categorical
        if item.name in sample.categorical_feats
    }
    cross = {
        item.name: stats.cross_entry(item.name)
        for item in spec.cross
        if item.left in sample.numeric_feats
        or item.left in sample.categorical_feats
        or item.right in sample.numeric_feats
        or item.right in sample.categorical_feats
    }
    projection = {
        "global": stats.payload.get("global", {}),
        "numeric": numeric,
        "categorical": categorical,
        "cross": cross,
    }
    versions = {
        "global": stats.series_version("global"),
        "numeric": {
            name: stats.series_version(f"numeric.{name}") for name in numeric
        },
        "categorical": {
            name: stats.series_version(
                f"categorical.{name}.{sample.categorical_feats.get(name, '')}"
            )
            if name in sample.categorical_feats
            else stats.series_version(f"categorical.{name}")
            for name in categorical
        },
        "cross": {name: stats.series_version(f"cross.{name}") for name in cross},
    }
    fingerprint = sha256_obj({"projection": projection, "versions": versions})
    return versions, fingerprint


def changed_statistics_paths(
    previous: LoadedStatistics, current: LoadedStatistics
) -> list[str]:
    old_flat = _flatten(previous.payload)
    new_flat = _flatten(current.payload)
    ignored = {"stats_version"}
    return sorted(
        path
        for path in set(old_flat) | set(new_flat)
        if path not in ignored
        and not path.startswith("series_versions")
        and old_flat.get(path) != new_flat.get(path)
    )


def affected_requests_for_statistics_diff(
    previous: LoadedStatistics,
    current: LoadedStatistics,
    samples: Iterable[Sample],
) -> tuple[set[str], list[str]]:
    changed = changed_statistics_paths(previous, current)
    if not changed:
        return set(), []
    all_requests = {sample.request_id for sample in samples}
    if any(path.startswith("global") for path in changed):
        return all_requests, changed
    affected: set[str] = set()
    for sample in samples:
        for path in changed:
            if path.startswith("numeric."):
                name = path.split(".")[1]
                if name in sample.numeric_feats:
                    affected.add(sample.request_id)
            elif path.startswith("categorical."):
                name = path.split(".")[1]
                if name in sample.categorical_feats:
                    affected.add(sample.request_id)
            elif path.startswith("cross."):
                name = path.split(".")[1]
                if name in {item for item in sample.numeric_feats} | {
                    item for item in sample.categorical_feats
                }:
                    affected.add(sample.request_id)
    return affected, changed


def _flatten(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            result.update(_flatten(item, child))
        return result
    return {path: value}


def accumulate_statistics(
    assembled_rows: Iterable[Any],
    score_rows: Iterable[dict[str, Any]],
    samples: Iterable[Sample],
) -> dict[str, Any]:
    sample_by_id = {sample.request_id: sample for sample in samples}
    score_by_id = {row["request_id"]: row for row in score_rows}
    numeric: dict[str, dict[str, Any]] = {}
    categorical: dict[str, dict[str, Any]] = {}
    cross: dict[str, dict[str, Any]] = {}
    click_label_sum = 0
    convert_label_sum = 0
    click_probability_sum: list[float] = []
    cvr_probability_sum: list[float] = []
    expected_conversion_sum: list[float] = []
    row_count = 0
    for assembled in assembled_rows:
        request_id = assembled.request_id
        if request_id not in sample_by_id or request_id not in score_by_id:
            fail(
                E_FORMAT_INVALID,
                "accumulation inputs do not contain a matching request",
                field="request_id",
                request_id=request_id,
            )
        sample = sample_by_id[request_id]
        score = score_by_id[request_id]
        row_count += 1
        click_label_sum += sample.label_click
        convert_label_sum += sample.label_convert
        click_probability_sum.append(float(score["click"]["probability"]))
        cvr_probability_sum.append(float(score["convert"]["probability"]))
        expected_conversion_sum.append(float(score["expected_conversion"]))
        for detail in assembled.details:
            feature = detail["feature"]
            entry: dict[str, Any]
            if detail["kind"] == "numeric":
                entry = numeric.setdefault(
                    feature,
                    {"seen": 0, "value_sum": [], "missing_count": 0},
                )
            elif detail["kind"] == "categorical":
                entry = categorical.setdefault(
                    feature,
                    {"seen": 0, "bucket_sum": 0, "missing_count": 0},
                )
            else:
                entry = cross.setdefault(
                    feature,
                    {"seen": 0, "value_sum": [], "missing_count": 0},
                )
            entry["seen"] += 1
            if detail["imputed"]:
                entry["missing_count"] += 1
            if "value_sum" in entry:
                entry["value_sum"].append(float(detail["encoded_value"]))
            if "bucket_sum" in entry:
                entry["bucket_sum"] += int(detail.get("bucket", 0))
    payload = {
        "protocol": "online_statistics_accumulation_v1",
        "row_count": row_count,
        "label_click_sum": click_label_sum,
        "label_convert_sum": convert_label_sum,
        "click_probability_sum": math.fsum(click_probability_sum),
        "cvr_probability_sum": math.fsum(cvr_probability_sum),
        "expected_conversion_sum": math.fsum(expected_conversion_sum),
        "numeric": _finish_aggregates(numeric),
        "categorical": categorical,
        "cross": _finish_aggregates(cross),
    }
    return payload


def _finish_aggregates(values: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, item in sorted(values.items()):
        result[name] = {
            "seen": item["seen"],
            "missing_count": item["missing_count"],
            "value_sum": math.fsum(item["value_sum"]),
        }
    return result


def accumulation_checksum(payload: dict[str, Any]) -> str:
    return sha256_obj(payload)


def statistics_canonical_json(stats: LoadedStatistics) -> str:
    return canonical_json(stats.payload)


__all__ = [
    "LoadedStatistics",
    "load_statistics",
    "parse_statistics",
    "statistics_dependency_projection",
    "changed_statistics_paths",
    "affected_requests_for_statistics_diff",
    "accumulate_statistics",
    "statistics_canonical_json",
]

