from __future__ import annotations

import os
import sqlite3
import math
from pathlib import Path
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .canonical import (
    canonical_json,
    decimal_text,
    digest_file,
    digest_json,
    load_json,
)
from .errors import (
    E_DUP_EXPERIMENT_ID,
    E_HYPERPARAM_TYPE,
    E_INPUT_SCHEMA,
    E_LINEAGE_CYCLE,
    E_METRIC_CLOCK,
    E_METRIC_DIGEST,
    E_METRIC_ID_DUP,
    E_METRIC_REFERENCE,
    E_METRIC_SEQ,
    E_METRIC_STEP,
    E_METRIC_VALUE,
    E_PARENT_MISSING,
    E_V1_ALIAS_DUP,
    E_V1_FINGERPRINT,
    E_V1_REFRESH,
    E_V2_GENERATION,
    E_V2_HASH,
    E_V2_METADATA,
    fail,
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(E_INPUT_SCHEMA, f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        fail(E_INPUT_SCHEMA, f"{label} must be an array")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(E_INPUT_SCHEMA, f"{label} must be a non-empty string")
    return value


def _require_clock(value: Any, label: str) -> int:
    if not _is_int(value) or value < 0:
        fail(E_INPUT_SCHEMA, f"{label} must be a non-negative integer logical clock")
    return value


def _require_finite_number(value: Any, label: str, code: str = E_INPUT_SCHEMA) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        fail(code, f"{label} must be a finite JSON number", value=str(value))
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            fail(code, f"{label} must be finite", value=str(value))
        parsed = Decimal(str(value))
    else:
        parsed = Decimal(value)
    if not parsed.is_finite():
        fail(code, f"{label} must be finite", value=str(value))
    return parsed


@dataclass(frozen=True)
class Experiment:
    id: str
    parent_id: str | None
    hyperparameters: dict[str, Any]
    dataset_version: str
    code_commit: str
    baseline_metrics: dict[str, Decimal]
    logical_clock_ms: int
    metric_references: dict[str, tuple[str, str]]
    aliases: tuple[str, ...]
    fingerprint: str

    def declaration(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "hyperparameters": self.hyperparameters,
            "dataset_version": self.dataset_version,
            "code_commit": self.code_commit,
            "baseline_metrics": self.baseline_metrics,
            "logical_clock_ms": self.logical_clock_ms,
            "metric_references": {
                key: {"experiment_id": target[0], "metric": target[1]}
                for key, target in self.metric_references.items()
            },
        }

    def as_json(self) -> dict[str, Any]:
        declaration = self.declaration()
        declaration["aliases"] = list(self.aliases)
        declaration["fingerprint"] = self.fingerprint
        return declaration


@dataclass(frozen=True)
class MetricRecord:
    record_id: str
    seq: int
    experiment_id: str
    metric: str
    step: int
    value_text: str
    logical_clock_ms: int
    record_digest: str
    raw_json: str

    @property
    def value(self) -> Decimal:
        return Decimal(self.value_text)


@dataclass(frozen=True)
class SourceEntry:
    source_format: str
    entry_key: str
    alias: str
    experiment_id: str
    metric: str
    relative_refresh_seconds: int | None
    absolute_refresh_at_ms: int
    source_clock_ms: int
    trace_generation: int
    source_priority: int
    metric_digest: str
    tombstone: bool
    conversion_scale: int
    conversion_remainder_ms: int
    rounding_direction: str
    raw_time_json: dict[str, Any]
    raw_json: str
    source_version: str

    def as_json(self) -> dict[str, Any]:
        return {
            "source_format": self.source_format,
            "entry_key": self.entry_key,
            "alias": self.alias,
            "experiment_id": self.experiment_id,
            "metric": self.metric,
            "relative_refresh_seconds": self.relative_refresh_seconds,
            "absolute_refresh_at_ms": self.absolute_refresh_at_ms,
            "source_clock_ms": self.source_clock_ms,
            "trace_generation": self.trace_generation,
            "source_priority": self.source_priority,
            "metric_digest": self.metric_digest,
            "tombstone": self.tombstone,
            "conversion_scale": self.conversion_scale,
            "conversion_remainder_ms": self.conversion_remainder_ms,
            "rounding_direction": self.rounding_direction,
            "raw_time_json": self.raw_time_json,
        }


@dataclass
class V1Snapshot:
    path: str
    snapshot_version: str
    version_number: int
    logical_clock_ms: int
    trace_generation: int
    entries: list[SourceEntry]
    file_hash: str
    raw: dict[str, Any] = field(repr=False)


@dataclass
class V2Snapshot:
    manifest_path: str
    snapshot_version: str
    version_number: int
    logical_clock_ms: int
    trace_generation: int
    trace_generation_floor: int
    shard_hashes: list[str]
    shard_paths: list[str]
    shard_generations: list[int]
    shard_clocks: list[int]
    entries: list[SourceEntry]
    manifest_hash: str
    metadata_db: str
    raw: dict[str, Any] = field(repr=False)


def experiment_declaration_payload(
    *,
    experiment_id: str,
    parent_id: str | None,
    hyperparameters: dict[str, Any],
    dataset_version: str,
    code_commit: str,
    baseline_metrics: dict[str, Decimal],
    logical_clock_ms: int,
    metric_references: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    return {
        "id": experiment_id,
        "parent_id": parent_id,
        "hyperparameters": hyperparameters,
        "dataset_version": dataset_version,
        "code_commit": code_commit,
        "baseline_metrics": baseline_metrics,
        "logical_clock_ms": logical_clock_ms,
        "metric_references": {
            key: {"experiment_id": target[0], "metric": target[1]}
            for key, target in sorted(metric_references.items())
        },
    }


def experiment_fingerprint(
    *,
    experiment_id: str,
    parent_id: str | None,
    hyperparameters: dict[str, Any],
    dataset_version: str,
    code_commit: str,
    baseline_metrics: dict[str, Decimal],
    logical_clock_ms: int,
    metric_references: dict[str, tuple[str, str]],
) -> str:
    return digest_json(
        experiment_declaration_payload(
            experiment_id=experiment_id,
            parent_id=parent_id,
            hyperparameters=hyperparameters,
            dataset_version=dataset_version,
            code_commit=code_commit,
            baseline_metrics=baseline_metrics,
            logical_clock_ms=logical_clock_ms,
            metric_references=metric_references,
        )
    )


def validate_experiments(document: Any) -> list[Experiment]:
    root = _require_object(document, "experiment lineage")
    raw_items = root.get("experiments")
    if raw_items is None and isinstance(root.get("lineage"), list):
        raw_items = root["lineage"]
    raw_items = _require_list(raw_items, "experiments")
    experiments: list[Experiment] = []
    seen: set[str] = set()

    for index, raw_item in enumerate(raw_items):
        item = _require_object(raw_item, f"experiments[{index}]")
        experiment_id = _require_text(item.get("id"), f"experiments[{index}].id")
        if experiment_id in seen:
            fail(E_DUP_EXPERIMENT_ID, "Experiment IDs must be unique", experiment_id=experiment_id)
        seen.add(experiment_id)
        parent_id = item.get("parent_id", item.get("parent"))
        if parent_id is not None:
            parent_id = _require_text(parent_id, f"experiments[{index}].parent_id")
            if parent_id == "\"\"":
                parent_id = None

        hyperparameters = _require_object(
            item.get("hyperparameters", {}), f"experiments[{index}].hyperparameters"
        )
        for key, value in hyperparameters.items():
            if not isinstance(key, str) or not key.strip():
                fail(E_HYPERPARAM_TYPE, "Hyperparameter keys must be non-empty strings")
            if value is None or isinstance(value, (str, bool, int, float, Decimal)):
                if isinstance(value, float) and not math.isfinite(value):
                    fail(
                        E_HYPERPARAM_TYPE,
                        "Hyperparameter numbers must be finite",
                        experiment_id=experiment_id,
                        key=key,
                    )
                if isinstance(value, Decimal) and not value.is_finite():
                    fail(
                        E_HYPERPARAM_TYPE,
                        "Hyperparameter numbers must be finite",
                        experiment_id=experiment_id,
                        key=key,
                    )
                if isinstance(value, float):
                    hyperparameters[key] = Decimal(str(value))
            else:
                fail(
                    E_HYPERPARAM_TYPE,
                    "Hyperparameter values must be scalar JSON values",
                    experiment_id=experiment_id,
                    key=key,
                    value_type=type(value).__name__,
                )

        dataset_version = _require_text(
            item.get("dataset_version"), f"experiments[{index}].dataset_version"
        )
        code_commit = _require_text(
            item.get("code_commit", ""), f"experiments[{index}].code_commit"
        )
        if not code_commit:
            fail(E_INPUT_SCHEMA, "code_commit must not be empty", experiment_id=experiment_id)

        baseline_raw = _require_object(
            item.get("baseline_metrics", {}), f"experiments[{index}].baseline_metrics"
        )
        baseline_metrics: dict[str, Decimal] = {}
        for metric, value in baseline_raw.items():
            _require_text(metric, "baseline metric name")
            baseline_metrics[metric] = _require_finite_number(
                value, f"baseline metric {metric}", E_METRIC_VALUE
            )

        logical_clock_ms = _require_clock(
            item.get("logical_clock_ms"), f"experiments[{index}].logical_clock_ms"
        )

        refs_raw = item.get("metric_references", {})
        refs_obj = _require_object(refs_raw, f"experiments[{index}].metric_references")
        metric_references: dict[str, tuple[str, str]] = {}
        for metric, target_raw in refs_obj.items():
            _require_text(metric, "metric reference name")
            if isinstance(target_raw, str):
                if "." not in target_raw:
                    fail(
                        E_METRIC_REFERENCE,
                        "String metric reference must be '<experiment>.<metric>'",
                        experiment_id=experiment_id,
                        metric=metric,
                    )
                target_experiment, target_metric = target_raw.rsplit(".", 1)
            else:
                target = _require_object(target_raw, f"metric reference {metric}")
                target_experiment = _require_text(
                    target.get("experiment_id", target.get("experiment")),
                    f"metric reference {metric}.experiment_id",
                )
                target_metric = _require_text(
                    target.get("metric"), f"metric reference {metric}.metric"
                )
            if not target_experiment or not target_metric:
                fail(
                    E_METRIC_REFERENCE,
                    "Metric reference target must be non-empty",
                    experiment_id=experiment_id,
                    metric=metric,
                )
            metric_references[metric] = (target_experiment, target_metric)

        aliases_raw = item.get("aliases", [])
        aliases = tuple(
            _require_text(alias, f"experiments[{index}].aliases item")
            for alias in _require_list(aliases_raw, f"experiments[{index}].aliases")
        )
        if len(set(aliases)) != len(aliases):
            fail(E_INPUT_SCHEMA, "Experiment aliases must be unique", experiment_id=experiment_id)

        fingerprint = experiment_fingerprint(
            experiment_id=experiment_id,
            parent_id=parent_id,
            hyperparameters=hyperparameters,
            dataset_version=dataset_version,
            code_commit=code_commit,
            baseline_metrics=baseline_metrics,
            logical_clock_ms=logical_clock_ms,
            metric_references=metric_references,
        )
        declared_fingerprint = item.get("fingerprint")
        if declared_fingerprint is not None:
            declared_fingerprint = _require_text(
                declared_fingerprint, f"experiments[{index}].fingerprint"
            )
            if declared_fingerprint != fingerprint:
                fail(
                    E_V1_FINGERPRINT,
                    "Experiment declaration fingerprint does not match its contents",
                    experiment_id=experiment_id,
                    declared=declared_fingerprint,
                    computed=fingerprint,
                )

        experiments.append(
            Experiment(
                id=experiment_id,
                parent_id=parent_id,
                hyperparameters=hyperparameters,
                dataset_version=dataset_version,
                code_commit=code_commit,
                baseline_metrics=baseline_metrics,
                logical_clock_ms=logical_clock_ms,
                metric_references=metric_references,
                aliases=aliases,
                fingerprint=fingerprint,
            )
        )

    _validate_lineage(experiments)
    return experiments


def _validate_lineage(experiments: Iterable[Experiment]) -> None:
    by_id = {experiment.id: experiment for experiment in experiments}
    for experiment in experiments:
        if experiment.parent_id is not None and experiment.parent_id not in by_id:
            fail(
                E_PARENT_MISSING,
                "Parent experiment reference does not exist",
                experiment_id=experiment.id,
                parent_id=experiment.parent_id,
            )
        for metric, (target_experiment, target_metric) in experiment.metric_references.items():
            if target_experiment not in by_id:
                fail(
                    E_METRIC_REFERENCE,
                    "Metric reference experiment does not exist",
                    experiment_id=experiment.id,
                    metric=metric,
                    target_experiment=target_experiment,
                    target_metric=target_metric,
                )

    state: dict[str, int] = {}

    def visit(experiment_id: str, trail: list[str]) -> None:
        marker = state.get(experiment_id, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle_start = trail.index(experiment_id) if experiment_id in trail else 0
            fail(
                E_LINEAGE_CYCLE,
                "Experiment lineage contains a cycle",
                cycle=trail[cycle_start:] + [experiment_id],
            )
        state[experiment_id] = 1
        parent_id = by_id[experiment_id].parent_id
        if parent_id is not None:
            visit(parent_id, trail + [experiment_id])
        state[experiment_id] = 2

    for experiment in experiments:
        visit(experiment.id, [])


def validate_metric_reference_targets(
    experiments: Iterable[Experiment], metric_keys: set[tuple[str, str]]
) -> None:
    for experiment in experiments:
        for metric, target in sorted(experiment.metric_references.items()):
            if target not in metric_keys:
                fail(
                    E_METRIC_REFERENCE,
                    "Metric reference target metric does not exist",
                    experiment_id=experiment.id,
                    metric=metric,
                    target_experiment=target[0],
                    target_metric=target[1],
                )


def _record_payload(
    record_id: str,
    seq: int,
    experiment_id: str,
    metric: str,
    step: int,
    value_text: str,
    logical_clock_ms: int,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "seq": seq,
        "experiment_id": experiment_id,
        "metric": metric,
        "step": step,
        "value": Decimal(value_text),
        "logical_clock_ms": logical_clock_ms,
    }


def validate_metrics(document: Any, experiments: Iterable[Experiment]) -> list[MetricRecord]:
    root = document if isinstance(document, list) else _require_object(document, "metric stream")
    raw_records = root if isinstance(root, list) else root.get("records", root.get("metrics"))
    raw_records = _require_list(raw_records, "metric records")
    experiment_ids = {experiment.id for experiment in experiments}
    records: list[MetricRecord] = []
    seen_seq: set[int] = set()
    seen_record_ids: set[str] = set()
    for index, raw_record in enumerate(raw_records):
        record = _require_object(raw_record, f"records[{index}]")
        seq = record.get("seq", record.get("sequence"))
        if not _is_int(seq) or seq < 0:
            fail(E_METRIC_SEQ, "Metric sequence must be a non-negative integer", index=index)
        if seq in seen_seq:
            fail(E_METRIC_SEQ, "Metric sequence numbers must be unique", seq=seq)
        seen_seq.add(seq)
        record_id = record.get("record_id")
        if record_id is None:
            record_id = f"seq:{seq}"
        record_id = _require_text(record_id, f"records[{index}].record_id")
        if record_id in seen_record_ids:
            fail(
                E_METRIC_ID_DUP,
                "Metric record IDs must be unique",
                record_id=record_id,
            )
        seen_record_ids.add(record_id)
        experiment_id = _require_text(
            record.get("experiment_id"), f"records[{index}].experiment_id"
        )
        if experiment_id not in experiment_ids:
            fail(
                E_INPUT_SCHEMA,
                "Metric record references an unknown experiment",
                seq=seq,
                experiment_id=experiment_id,
            )
        metric = _require_text(record.get("metric"), f"records[{index}].metric")
        step = record.get("step")
        if not _is_int(step) or step < 0:
            fail(E_METRIC_STEP, "Metric step must be a non-negative integer", seq=seq)
        if "value" not in record:
            fail(E_METRIC_VALUE, "Metric record has no value", seq=seq)
        value = _require_finite_number(record["value"], "metric value", E_METRIC_VALUE)
        value_text = decimal_text(value)
        logical_clock_ms = _require_clock(
            record.get("logical_clock_ms"), f"records[{index}].logical_clock_ms"
        )
        digest = digest_json(
            _record_payload(
                record_id=record_id,
                seq=seq,
                experiment_id=experiment_id,
                metric=metric,
                step=step,
                value_text=value_text,
                logical_clock_ms=logical_clock_ms,
            )
        )
        provided_digest = record.get("record_digest", record.get("digest"))
        if provided_digest is not None:
            provided_digest = _require_text(provided_digest, f"records[{index}].record_digest")
            if provided_digest != digest:
                fail(
                    E_METRIC_DIGEST,
                    "Metric record digest does not match its contents",
                    seq=seq,
                    declared=provided_digest,
                    computed=digest,
                )
        records.append(
            MetricRecord(
                record_id=record_id,
                seq=seq,
                experiment_id=experiment_id,
                metric=metric,
                step=step,
                value_text=value_text,
                logical_clock_ms=logical_clock_ms,
                record_digest=digest,
                raw_json=canonical_json(record),
            )
        )

    records.sort(key=lambda record: record.seq)
    last_step: dict[tuple[str, str], int] = {}
    last_clock: dict[tuple[str, str], int] = {}
    for record in records:
        key = (record.experiment_id, record.metric)
        if key in last_step and record.step <= last_step[key]:
            fail(
                E_METRIC_STEP,
                "Metric steps must be strictly increasing in sequence order",
                experiment_id=record.experiment_id,
                metric=record.metric,
                previous_step=last_step[key],
                step=record.step,
            )
        if key in last_clock and record.logical_clock_ms < last_clock[key]:
            fail(
                E_METRIC_CLOCK,
                "Metric logical clocks must be non-decreasing in sequence order",
                experiment_id=record.experiment_id,
                metric=record.metric,
                previous_clock_ms=last_clock[key],
                logical_clock_ms=record.logical_clock_ms,
            )
        last_step[key] = record.step
        last_clock[key] = record.logical_clock_ms
    return records


def _require_fingerprint(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if not text.startswith("sha256:"):
        text = "sha256:" + text
    return text


def _snapshot_clock(root: dict[str, Any], path: str) -> int:
    for key in ("logical_clock_ms", "logical_clock", "refresh_clock_ms", "clock_ms"):
        if key in root:
            return _require_clock(root[key], f"{path}.{key}")
    fail(E_INPUT_SCHEMA, "Snapshot must declare a logical clock", path=path)


def _snapshot_generation(root: dict[str, Any], path: str, default: int) -> int:
    for key in ("tracking_generation", "trace_generation", "generation"):
        if key in root:
            value = root[key]
            if not _is_int(value) or value < 0:
                fail(E_V2_GENERATION, "Tracking generation must be a non-negative integer", path=path)
            return value
    return default


def _snapshot_version(root: dict[str, Any], path: str, fallback: str | None) -> str:
    for key in ("snapshot_version", "snapshot_id", "version"):
        if key in root:
            return _require_text(root[key], f"{path}.{key}")
    if fallback:
        return fallback
    fail(E_INPUT_SCHEMA, "Snapshot must declare a version", path=path)


def _snapshot_version_number(root: dict[str, Any], default: int) -> int:
    for key in ("version_number", "manifest_version"):
        if key in root:
            value = root[key]
            if not _is_int(value) or value < 0:
                fail(E_INPUT_SCHEMA, "Snapshot version_number must be a non-negative integer")
            return value
    return default


def validate_v1_snapshot(
    path: str,
    experiments: Iterable[Experiment],
    fingerprint_history: dict[str, set[str]] | None = None,
) -> V1Snapshot:
    root = _require_object(load_json(path), "v1 snapshot")
    snapshot_version = _snapshot_version(root, path, os.path.basename(path))
    logical_clock_ms = _snapshot_clock(root, path)
    trace_generation = _snapshot_generation(root, path, 1)
    version_number = _snapshot_version_number(root, 1)
    entries_raw = root.get("entries", root.get("refresh_entries", root.get("metrics")))
    entries_raw = _require_list(entries_raw, "v1 snapshot entries")
    by_id = {experiment.id: experiment for experiment in experiments}
    aliases: set[str] = set()
    entries: list[SourceEntry] = []
    for index, raw_entry in enumerate(entries_raw):
        entry = _require_object(raw_entry, f"v1.entries[{index}]")
        alias = _require_text(entry.get("alias"), f"v1.entries[{index}].alias")
        if alias in aliases:
            fail(E_V1_ALIAS_DUP, "v1 aliases must be unique", alias=alias)
        aliases.add(alias)
        experiment_id = _require_text(
            entry.get("experiment_id", entry.get("experiment")),
            f"v1.entries[{index}].experiment_id",
        )
        if experiment_id not in by_id:
            fail(
                E_INPUT_SCHEMA,
                "v1 entry references an unknown experiment",
                alias=alias,
                experiment_id=experiment_id,
            )
        metric = _require_text(entry.get("metric"), f"v1.entries[{index}].metric")
        relative = entry.get(
            "relative_refresh_seconds", entry.get("refresh_after_seconds", entry.get("refresh_s"))
        )
        if not _is_int(relative) or relative < 0:
            fail(
                E_V1_REFRESH,
                "v1 relative refresh seconds must be a non-negative integer",
                alias=alias,
            )
        fingerprint = _require_fingerprint(
            entry.get("fingerprint"), f"v1.entries[{index}].fingerprint"
        )
        allowed_fingerprints = (
            set(fingerprint_history.get(experiment_id, set()))
            if fingerprint_history is not None
            else set()
        )
        allowed_fingerprints.add(by_id[experiment_id].fingerprint)
        if fingerprint not in allowed_fingerprints:
            fail(
                E_V1_FINGERPRINT,
                "v1 alias fingerprint does not match a known experiment declaration",
                alias=alias,
                experiment_id=experiment_id,
                declared=fingerprint,
                computed=by_id[experiment_id].fingerprint,
                known_fingerprints=sorted(allowed_fingerprints),
            )
        absolute_refresh = logical_clock_ms + relative * 1000
        metric_digest = entry.get("metric_digest", entry.get("digest"))
        if metric_digest is None:
            metric_digest = digest_json(
                {
                    "alias": alias,
                    "experiment_id": experiment_id,
                    "metric": metric,
                    "relative_refresh_seconds": relative,
                    "fingerprint": fingerprint,
                }
            )
        else:
            metric_digest = _require_text(metric_digest, "v1 metric digest")
        entry_key = f"v1:{alias}:{metric}"
        raw_time_json = {
            "representation": "relative_seconds",
            "relative_refresh_seconds": relative,
            "base_logical_clock_ms": logical_clock_ms,
        }
        entries.append(
            SourceEntry(
                source_format="v1",
                entry_key=entry_key,
                alias=alias,
                experiment_id=experiment_id,
                metric=metric,
                relative_refresh_seconds=relative,
                absolute_refresh_at_ms=absolute_refresh,
                source_clock_ms=logical_clock_ms,
                trace_generation=trace_generation,
                source_priority=1,
                metric_digest=metric_digest,
                tombstone=False,
                conversion_scale=1000,
                conversion_remainder_ms=0,
                rounding_direction="floor",
                raw_time_json=raw_time_json,
                raw_json=canonical_json(entry),
                source_version=snapshot_version,
            )
        )
    return V1Snapshot(
        path=path,
        snapshot_version=snapshot_version,
        version_number=version_number,
        logical_clock_ms=logical_clock_ms,
        trace_generation=trace_generation,
        entries=entries,
        file_hash=digest_file(path),
        raw=root,
    )


def _safe_child(base_dir: str, relative_path: str, label: str) -> str:
    if not isinstance(relative_path, str) or not relative_path:
        fail(E_INPUT_SCHEMA, f"{label} path must be a non-empty string")
    path = os.path.realpath(os.path.join(base_dir, relative_path))
    base = os.path.realpath(base_dir)
    try:
        inside = os.path.commonpath([base, path]) == base
    except ValueError:
        inside = False
    if not inside:
        fail(E_INPUT_SCHEMA, f"{label} path escapes the snapshot package", path=relative_path)
    return path


def _read_v2_metadata(path: str, manifest: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not os.path.exists(path):
        fail(E_V2_METADATA, "v2 SQLite metadata database is missing", path=path)
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        metadata_entries: list[dict[str, Any]] | None = None
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "tracking_metadata" in tables:
                metadata = {
                    str(row["key"]): str(row["value"])
                    for row in connection.execute("SELECT key, value FROM tracking_metadata")
                }
                manifest_version = (
                    manifest.get("snapshot_version")
                    or manifest.get("snapshot_id")
                    or manifest.get("version")
                )
                manifest_generation = (
                    manifest.get("tracking_generation")
                    if "tracking_generation" in manifest
                    else manifest.get("trace_generation", manifest.get("generation"))
                )
                manifest_clock = next(
                    (
                        manifest[key]
                        for key in (
                            "logical_clock_ms",
                            "logical_clock",
                            "refresh_clock_ms",
                            "clock_ms",
                        )
                        if key in manifest
                    ),
                    None,
                )
                expected = {
                    "snapshot_version": None
                    if manifest_version is None
                    else str(manifest_version),
                    "tracking_generation": None
                    if manifest_generation is None
                    else str(manifest_generation),
                    "logical_clock_ms": None
                    if manifest_clock is None
                    else str(manifest_clock),
                }
                for key, expected_value in expected.items():
                    if expected_value is None:
                        continue
                    if key in metadata and metadata[key] != expected_value:
                        fail(
                            E_V2_METADATA,
                            "v2 metadata disagrees with the manifest",
                            key=key,
                            metadata_value=metadata[key],
                            manifest_value=expected_value,
                        )
            if "tracking_shards" in tables:
                hashes = [
                    str(row["sha256"])
                    for row in connection.execute(
                        "SELECT sha256 FROM tracking_shards ORDER BY shard_index"
                    )
                ]
                manifest_hashes = [
                    str(shard.get("sha256", shard.get("hash")))
                    for shard in manifest.get("shards", [])
                ]
                if hashes and hashes != manifest_hashes:
                    fail(
                        E_V2_METADATA,
                        "v2 metadata shard hashes disagree with the manifest",
                        metadata_hashes=hashes,
                        manifest_hashes=manifest_hashes,
                    )
            if "tracking_entries" in tables:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(tracking_entries)")
                }
                if not {"experiment_id", "metric", "metric_digest"}.issubset(columns):
                    fail(
                        E_V2_METADATA,
                        "v2 tracking_entries metadata table is missing required columns",
                        columns=sorted(columns),
                    )
                metadata_entries = [
                    {key: row[key] for key in row.keys()}
                    for row in connection.execute("SELECT * FROM tracking_entries")
                ]
        finally:
            connection.close()
        return metadata_entries
    except sqlite3.Error as exc:
        fail(E_V2_METADATA, "Unable to read v2 SQLite metadata", path=path, detail=str(exc))


def validate_v2_snapshot(manifest_path: str, experiments: Iterable[Experiment]) -> V2Snapshot:
    root = _require_object(load_json(manifest_path), "v2 manifest")
    base_dir = os.path.dirname(os.path.realpath(manifest_path))
    snapshot_version = _snapshot_version(root, manifest_path, None)
    logical_clock_ms = _snapshot_clock(root, manifest_path)
    trace_generation = _snapshot_generation(root, manifest_path, 0)
    floor_raw = root.get("tracking_generation_floor", root.get("generation_floor", trace_generation))
    if not _is_int(floor_raw) or floor_raw < 0:
        fail(E_V2_GENERATION, "tracking_generation_floor must be a non-negative integer")
    trace_generation_floor = floor_raw
    if trace_generation_floor > trace_generation:
        fail(
            E_V2_GENERATION,
            "tracking_generation_floor cannot exceed tracking_generation",
            floor=trace_generation_floor,
            generation=trace_generation,
        )
    version_number = _snapshot_version_number(root, 2)
    shards_raw = _require_list(root.get("shards"), "v2 shards")
    if not shards_raw:
        fail(E_INPUT_SCHEMA, "v2 manifest must contain at least one shard")

    metadata_path_raw = root.get("metadata_db", root.get("metadata_db_path", root.get("sqlite")))
    if metadata_path_raw is None:
        metadata_path_raw = "metadata.sqlite3"
    metadata_path = _safe_child(base_dir, metadata_path_raw, "metadata database")
    metadata_entries = _read_v2_metadata(metadata_path, root)

    by_id = {experiment.id: experiment for experiment in experiments}
    entries: list[SourceEntry] = []
    shard_hashes: list[str] = []
    shard_paths: list[str] = []
    shard_generations: list[int] = []
    shard_clocks: list[int] = []
    generation_sequence: list[int] = []
    entry_keys: set[str] = set()
    for shard_index, raw_shard_ref in enumerate(shards_raw):
        shard_ref = _require_object(raw_shard_ref, f"v2.shards[{shard_index}]")
        shard_rel = shard_ref.get("path", shard_ref.get("file"))
        shard_path = _safe_child(base_dir, shard_rel, f"v2 shard {shard_index}")
        expected_hash = _require_text(
            shard_ref.get("sha256", shard_ref.get("hash")),
            f"v2.shards[{shard_index}].sha256",
        )
        if not expected_hash.startswith("sha256:"):
            expected_hash = "sha256:" + expected_hash
        actual_hash = digest_file(shard_path)
        if actual_hash != expected_hash:
            fail(
                E_V2_HASH,
                "v2 shard hash does not match the manifest",
                shard=shard_rel,
                declared=expected_hash,
                actual=actual_hash,
            )
        shard_hashes.append(actual_hash)
        shard_paths.append(shard_path)
        shard_doc = _require_object(load_json(shard_path), f"v2 shard {shard_rel}")
        shard_generation = _snapshot_generation(
            {**shard_doc, "trace_generation": shard_doc.get("trace_generation", shard_ref.get("trace_generation", trace_generation))},
            shard_path,
            trace_generation,
        )
        generation_sequence.append(shard_generation)
        shard_generations.append(shard_generation)
        shard_clock = _snapshot_clock(
            shard_doc if any(k in shard_doc for k in ("logical_clock_ms", "logical_clock", "refresh_clock_ms", "clock_ms")) else root,
            shard_path,
        )
        shard_clocks.append(shard_clock)
        raw_entries = shard_doc.get("entries")
        raw_entries = _require_list(raw_entries, f"v2 shard {shard_rel}.entries")
        for entry_index, raw_entry in enumerate(raw_entries):
            entry = _require_object(raw_entry, f"{shard_rel}.entries[{entry_index}]")
            experiment_id = _require_text(
                entry.get("experiment_id", entry.get("experiment")),
                f"{shard_rel}.entries[{entry_index}].experiment_id",
            )
            if experiment_id not in by_id:
                fail(
                    E_INPUT_SCHEMA,
                    "v2 entry references an unknown experiment",
                    experiment_id=experiment_id,
                )
            metric = _require_text(entry.get("metric"), f"{shard_rel}.entries[{entry_index}].metric")
            alias = entry.get("alias", experiment_id)
            alias = _require_text(alias, f"{shard_rel}.entries[{entry_index}].alias")
            absolute = entry.get(
                "absolute_refresh_at_ms",
                entry.get("absolute_refresh_ms", entry.get("refresh_at_ms")),
            )
            if not _is_int(absolute) or absolute < 0:
                fail(
                    E_INPUT_SCHEMA,
                    "v2 absolute refresh time must be a non-negative integer millisecond value",
                    experiment_id=experiment_id,
                    metric=metric,
                )
            entry_generation = entry.get(
                "trace_generation", entry.get("tracking_generation", shard_generation)
            )
            if not _is_int(entry_generation) or entry_generation < 0:
                fail(E_V2_GENERATION, "v2 entry trace_generation must be a non-negative integer")
            if entry_generation < trace_generation_floor or entry_generation > trace_generation:
                fail(
                    E_V2_GENERATION,
                    "v2 entry generation is outside the declared generation bounds",
                    experiment_id=experiment_id,
                    metric=metric,
                    trace_generation=entry_generation,
                    floor=trace_generation_floor,
                    ceiling=trace_generation,
                )
            tombstone = entry.get("tombstone", entry.get("deleted", False))
            if not isinstance(tombstone, bool):
                fail(E_INPUT_SCHEMA, "v2 tombstone must be a boolean")
            metric_digest = entry.get("metric_digest", entry.get("digest"))
            if metric_digest is not None:
                metric_digest = _require_text(metric_digest, "v2 metric digest")
            else:
                metric_digest = digest_json(
                    {
                        "experiment_id": experiment_id,
                        "metric": metric,
                        "absolute_refresh_at_ms": absolute,
                        "tombstone": tombstone,
                    }
                )
            entry_key = f"v2:{shard_index}:{entry_index}:{experiment_id}:{metric}"
            if entry_key in entry_keys:
                fail(E_INPUT_SCHEMA, "v2 entry key collision", entry_key=entry_key)
            entry_keys.add(entry_key)
            raw_time_json = {
                "representation": "absolute_milliseconds",
                "absolute_refresh_at_ms": absolute,
            }
            entries.append(
                SourceEntry(
                    source_format="v2",
                    entry_key=entry_key,
                    alias=alias,
                    experiment_id=experiment_id,
                    metric=metric,
                    relative_refresh_seconds=None,
                    absolute_refresh_at_ms=absolute,
                    source_clock_ms=shard_clock,
                    trace_generation=entry_generation,
                    source_priority=2,
                    metric_digest=metric_digest,
                    tombstone=tombstone,
                    conversion_scale=1,
                    conversion_remainder_ms=0,
                    rounding_direction="none",
                    raw_time_json=raw_time_json,
                    raw_json=canonical_json(entry),
                    source_version=snapshot_version,
                )
            )

    if generation_sequence != sorted(generation_sequence):
        fail(
            E_V2_GENERATION,
            "v2 shard tracking generations must be monotonically non-decreasing",
            generations=generation_sequence,
        )
    if any(generation < trace_generation_floor for generation in generation_sequence):
        fail(
            E_V2_GENERATION,
            "v2 shard generation is below the declared generation floor",
            generations=generation_sequence,
            floor=trace_generation_floor,
        )
    if metadata_entries is not None:
        expected_rows = set()
        for entry in entries:
            expected_rows.add(
                (
                    entry.experiment_id,
                    entry.metric,
                    entry.absolute_refresh_at_ms,
                    entry.trace_generation,
                    entry.metric_digest,
                    1 if entry.tombstone else 0,
                )
            )
        actual_rows = set()
        for row in metadata_entries:
            absolute = row.get(
                "absolute_refresh_at_ms", row.get("absolute_refresh_ms", row.get("refresh_at_ms"))
            )
            generation = row.get(
                "trace_generation", row.get("tracking_generation", row.get("generation"))
            )
            tombstone = row.get("tombstone", row.get("deleted", False))
            if isinstance(tombstone, str):
                tombstone = tombstone.strip().lower() in {"1", "true", "yes"}
            actual_rows.add(
                (
                    str(row.get("experiment_id")),
                    str(row.get("metric")),
                    int(absolute) if absolute is not None else None,
                    int(generation) if generation is not None else None,
                    str(row.get("metric_digest")),
                    1 if tombstone else 0,
                )
            )
        if actual_rows != expected_rows:
            fail(
                E_V2_METADATA,
                "v2 SQLite metadata entries disagree with the shard entries",
                metadata_entry_count=len(actual_rows),
                shard_entry_count=len(expected_rows),
                missing=sorted(expected_rows - actual_rows, key=str),
                unexpected=sorted(actual_rows - expected_rows, key=str),
            )
    return V2Snapshot(
        manifest_path=manifest_path,
        snapshot_version=snapshot_version,
        version_number=version_number,
        logical_clock_ms=logical_clock_ms,
        trace_generation=trace_generation,
        trace_generation_floor=trace_generation_floor,
        shard_hashes=shard_hashes,
        shard_paths=shard_paths,
        shard_generations=shard_generations,
        shard_clocks=shard_clocks,
        entries=entries,
        manifest_hash=digest_file(manifest_path),
        metadata_db=metadata_path,
        raw=root,
    )

