"""Validation and merging of v1/v2 measurement criteria snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .errors import PipelineError
from .models import CriterionRecord, SnapshotConfig, TaskSpec, UnifiedView
from .util import (
    as_int,
    as_number,
    canonical_json_bytes,
    ensure_inside,
    finite_number,
    load_json,
    safe_resolve,
    sha256_bytes,
    sha256_file,
)


_META_FIELDS = {
    "record_id",
    "id",
    "input_id",
    "canonical_id",
    "canonical_input_id",
    "criterion",
    "name",
    "generation",
    "algebra",
    "effective_ms",
    "effective_at_ms",
    "effective_seconds",
    "effective_s",
    "tombstone",
    "deleted",
    "suggestion_rule",
}


def _issue(error: PipelineError) -> dict[str, Any]:
    result = error.as_dict()
    result["recoverable"] = error.recoverable
    return result


def _classify_thresholds(data: dict[str, Any], source: dict[str, Any],
                         criterion: str) -> None:
    if "window_ms" in data:
        as_number(data["window_ms"], "window_ms", exclusive_minimum=True,
                  code="E_WINDOW")
    if "hop_ms" in data:
        as_number(data["hop_ms"], "hop_ms", exclusive_minimum=True,
                  code="E_WINDOW")
    if "policy" in data and data["policy"] not in ("zero_pad", "drop", "shorten"):
        raise PipelineError("E_WINDOW", "invalid partial-window policy",
                            source=source)
    for key in ("threshold", "threshold_db", "relative_gate_lu", "relative_gate_db"):
        if key in data:
            as_number(data[key], key, maximum=0.0, code="E_GATE")
    for key in ("threshold_lufs", "absolute_gate_lufs", "target_lufs"):
        if key in data:
            as_number(data[key], key, minimum=-120.0, maximum=20.0, code="E_GATE")
    for key in ("min_db", "dynamic_range_min_db"):
        if key in data:
            as_number(data[key], key, minimum=0.0, code="E_DRANGE")
    for key in ("max_db", "dynamic_range_max_db"):
        if key in data:
            as_number(data[key], key, minimum=0.0, code="E_DRANGE")
    if ("min_db" in data or "dynamic_range_min_db" in data) and (
        "max_db" in data or "dynamic_range_max_db" in data
    ):
        minimum = data.get("min_db", data.get("dynamic_range_min_db"))
        maximum = data.get("max_db", data.get("dynamic_range_max_db"))
        if float(minimum) > float(maximum):
            raise PipelineError("E_DRANGE", "dynamic-range minimum exceeds maximum",
                                source=source)
    if "ratio" in data:
        as_number(data["ratio"], "ratio", minimum=1.0, code="E_GATE")
    if criterion == "compression" and "enabled" in data and not isinstance(
        data["enabled"], bool
    ):
        raise PipelineError("E_SNAPSHOT", "compression.enabled must be boolean",
                            source=source)


def _record_from_entry(
    entry: Any,
    *,
    generation_default: int,
    source: dict[str, Any],
    sequence: int,
    origin_ms: int = 0,
    db_metadata: dict[str, Any] | None = None,
) -> tuple[CriterionRecord | None, PipelineError | None]:
    if not isinstance(entry, dict):
        return None, PipelineError("E_SNAPSHOT", "snapshot entry must be an object",
                                   source=source)
    merged = dict(entry)
    if db_metadata:
        for key, value in db_metadata.items():
            if key in merged and merged[key] != value and key not in ("id", "record_id"):
                return None, PipelineError(
                    "E_SNAPSHOT",
                    f"SQLite metadata disagrees with shard field {key}",
                    source=source,
                    details={"shard": merged[key], "sqlite": value},
                )
            merged.setdefault(key, value)
    canonical = merged.get("canonical_id", merged.get("canonical_input_id",
                                                       merged.get("input_id")))
    if not isinstance(canonical, str) or not canonical.strip():
        return None, PipelineError("E_SNAPSHOT", "entry has no canonical input id",
                                   source=source)
    criterion = merged.get("criterion", merged.get("name"))
    if not isinstance(criterion, str) or not criterion.strip():
        return None, PipelineError("E_SNAPSHOT", "entry has no criterion name",
                                   source=source, input_id=canonical)
    try:
        generation = as_int(
            merged.get("generation", merged.get("algebra", generation_default)),
            "generation", minimum=1, code="E_SNAPSHOT",
        )
        tombstone_value = merged.get("tombstone", merged.get("deleted", False))
        if not isinstance(tombstone_value, (bool, int)) or int(tombstone_value) not in (0, 1):
            raise PipelineError("E_SNAPSHOT", "tombstone must be boolean",
                                input_id=canonical, source=source)
        tombstone = bool(tombstone_value)
        if merged.get("effective_ms", merged.get("effective_at_ms")) is not None:
            effective_ms = as_int(
                merged.get("effective_ms", merged.get("effective_at_ms")),
                "effective_ms", minimum=0, code="E_SNAPSHOT",
            )
        elif merged.get("effective_seconds", merged.get("effective_s")) is not None:
            seconds = as_number(
                merged.get("effective_seconds", merged.get("effective_s")),
                "effective_seconds", minimum=0.0, code="E_SNAPSHOT",
            )
            effective_ms = origin_ms + int(round(seconds * 1000.0))
        else:
            effective_ms = origin_ms
    except PipelineError as exc:
        exc.input_id = canonical
        if exc.source is None:
            exc.source = source
        return None, exc

    data = {key: value for key, value in merged.items() if key not in _META_FIELDS}
    if "suggestion_rule" in merged:
        if not isinstance(merged["suggestion_rule"], dict):
            return None, PipelineError(
                "E_SNAPSHOT", "suggestion_rule must be a JSON object",
                input_id=canonical, source=source,
            )
        data.update(merged["suggestion_rule"])
    try:
        _classify_thresholds(data, source, criterion)
    except PipelineError as exc:
        exc.input_id = canonical
        if exc.source is None:
            exc.source = source
        return None, exc
    return CriterionRecord(
        canonical_id=canonical.strip(),
        criterion=criterion.strip(),
        generation=generation,
        effective_ms=effective_ms,
        tombstone=tombstone,
        data=data,
        source=source,
        sequence=sequence,
    ), None


def _entries_from_document(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if not isinstance(document, dict):
        raise PipelineError("E_SNAPSHOT", "snapshot JSON must be an object or array")
    for name in ("entries", "records", "criteria"):
        if name in document:
            entries = document[name]
            if not isinstance(entries, list):
                raise PipelineError("E_SNAPSHOT", f"snapshot {name} must be an array")
            return entries
    raise PipelineError("E_SNAPSHOT", "snapshot has no entries/records/criteria array")


def _validate_declared_count(document: Any, actual: int,
                             declared: int | None, source: dict[str, Any]) -> None:
    file_declared: int | None = None
    if isinstance(document, dict):
        for name in ("expected_windows", "expected_entries", "window_count"):
            if name in document:
                file_declared = as_int(document[name], name, minimum=0,
                                       code="E_DIGEST")
                break
    for value in (declared, file_declared):
        if value is not None and value != actual:
            raise PipelineError(
                "E_DIGEST",
                f"declared entry/window count {value} != actual {actual}",
                source=source,
            )


def _load_v1(task: TaskSpec, issues: list[dict[str, Any]]) -> tuple[
    list[CriterionRecord], dict[str, Any]
]:
    cfg = task.snapshots
    source_base = {
        "kind": "v1",
        "path": str(cfg.v1_path),
        "sha256": cfg.v1_sha256,
    }
    summary: dict[str, Any] = {
        "kind": "v1",
        "path": str(cfg.v1_path),
        "valid": False,
        "sha256": None,
        "declared_sha256": cfg.v1_sha256,
        "entries": 0,
        "skipped_entries": 0,
    }
    try:
        actual_digest = sha256_file(cfg.v1_path)
        summary["sha256"] = actual_digest
        if actual_digest.lower() != cfg.v1_sha256.lower():
            raise PipelineError(
                "E_DIGEST",
                "v1 snapshot digest mismatch",
                source=source_base,
                details={"expected": cfg.v1_sha256, "actual": actual_digest},
            )
        document = load_json(cfg.v1_path, code="E_SNAPSHOT")
        entries = _entries_from_document(document)
        _validate_declared_count(document, len(entries),
                                 cfg.v1_expected_entries, source_base)
        file_origin = 0
        if isinstance(document, dict):
            file_origin = as_int(
                document.get("logical_origin_ms", document.get("origin_ms", 0)),
                "logical_origin_ms", minimum=0, code="E_SNAPSHOT",
            )
        origin = cfg.v1_logical_origin_ms + file_origin
        summary["logical_origin_ms"] = origin
        records: list[CriterionRecord] = []
        for index, entry in enumerate(entries):
            source = {
                **source_base,
                "entry_index": index,
                "record_id": entry.get("record_id", entry.get("id"))
                if isinstance(entry, dict) else None,
            }
            record, error = _record_from_entry(
                entry,
                generation_default=1,
                source=source,
                sequence=index,
                origin_ms=origin,
            )
            if error is not None:
                issues.append(_issue(error))
                summary["skipped_entries"] += 1
                continue
            assert record is not None
            records.append(record)
        summary["valid"] = True
        summary["entries"] = len(records)
        return records, summary
    except PipelineError as exc:
        if exc.code == "E_IO":
            exc = PipelineError(
                "E_DIGEST", f"v1 snapshot content is missing: {exc.message}",
                source=source_base,
            )
        if exc.source is None:
            exc.source = source_base
        issues.append(_issue(exc))
        return [], summary


def _connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as exc:
        raise PipelineError("E_SQLITE", f"cannot open criteria database {path}: {exc}") from exc


def _load_v2(task: TaskSpec, issues: list[dict[str, Any]]) -> tuple[
    list[CriterionRecord], dict[str, Any]
]:
    cfg = task.snapshots
    db_source = {
        "kind": "v2-db",
        "path": str(cfg.v2_db_path),
        "sha256": cfg.v2_db_sha256,
    }
    summary: dict[str, Any] = {
        "kind": "v2",
        "database": str(cfg.v2_db_path),
        "valid": False,
        "sha256": None,
        "declared_sha256": cfg.v2_db_sha256,
        "shards": [],
        "entries": 0,
    }
    try:
        actual_digest = sha256_file(cfg.v2_db_path)
        summary["sha256"] = actual_digest
        if actual_digest.lower() != cfg.v2_db_sha256.lower():
            raise PipelineError(
                "E_DIGEST",
                "v2 SQLite metadata digest mismatch",
                source=db_source,
                details={"expected": cfg.v2_db_sha256, "actual": actual_digest},
            )
        connection = _connect_readonly(cfg.v2_db_path)
    except PipelineError as exc:
        if exc.code == "E_IO":
            exc = PipelineError(
                "E_DIGEST", f"v2 snapshot content is missing: {exc.message}",
                source=db_source,
            )
        if exc.source is None:
            exc.source = db_source
        issues.append(_issue(exc))
        return [], summary
    records: list[CriterionRecord] = []
    try:
        with connection:
            shard_rows = connection.execute(
                "SELECT * FROM snapshot_shards ORDER BY shard_id"
            ).fetchall()
            criteria_rows = connection.execute(
                "SELECT * FROM criteria ORDER BY shard_id, rowid"
            ).fetchall()
        by_id = {str(row["record_id"]): dict(row) for row in criteria_rows}
        used_ids: set[str] = set()
        invalid_shards: set[str] = set()
        for shard_row in shard_rows:
            shard = dict(shard_row)
            shard_source = {
                "kind": "v2-shard",
                "shard_id": str(shard["shard_id"]),
                "path": str(cfg.v2_shard_root / str(shard["path"])),
                "sha256": str(shard.get("sha256", "")).lower(),
                "database": str(cfg.v2_db_path),
            }
            shard_summary = {
                "shard_id": str(shard["shard_id"]),
                "path": str(cfg.v2_shard_root / str(shard["path"])),
                "valid": False,
                "sha256": None,
                "declared_sha256": shard.get("sha256"),
                "entries": 0,
            }
            summary["shards"].append(shard_summary)
            try:
                shard_path = safe_resolve(cfg.v2_shard_root, str(shard["path"]))
                ensure_inside(cfg.v2_shard_root, shard_path)
                try:
                    actual = sha256_file(shard_path)
                except PipelineError as exc:
                    if exc.code == "E_IO":
                        raise PipelineError(
                            "E_DIGEST", f"shard content is missing: {exc.message}",
                            source=shard_source,
                        ) from exc
                    raise
                shard_summary["sha256"] = actual
                declared = str(shard.get("sha256", "")).lower()
                if not declared:
                    raise PipelineError("E_DIGEST", "shard digest is missing",
                                        source=shard_source)
                if actual.lower() != declared:
                    raise PipelineError(
                        "E_DIGEST",
                        "v2 shard digest mismatch",
                        source=shard_source,
                        details={"expected": declared, "actual": actual},
                    )
                document = load_json(shard_path, code="E_SNAPSHOT")
                entries = _entries_from_document(document)
                expected = shard.get("expected_windows")
                if expected is None:
                    expected = shard.get("expected_entries")
                try:
                    declared_count = (
                        as_int(expected, "expected_windows", minimum=0,
                               code="E_DIGEST")
                        if expected is not None else None
                    )
                    generation_default = as_int(
                        shard.get("generation", 2), "generation",
                        minimum=1, code="E_SNAPSHOT",
                    )
                    shard_origin = as_int(
                        shard.get("logical_origin_ms", 0), "logical_origin_ms",
                        minimum=0, code="E_SNAPSHOT",
                    )
                except PipelineError as exc:
                    exc.source = shard_source
                    raise
                _validate_declared_count(document, len(entries), declared_count,
                                         shard_source)
                for index, entry in enumerate(entries):
                    record_id = (
                        entry.get("record_id", entry.get("id"))
                        if isinstance(entry, dict) else None
                    )
                    source = {
                        **shard_source,
                        "entry_index": index,
                        "record_id": record_id,
                    }
                    db_metadata = None
                    if record_id is not None:
                        db_row = by_id.get(str(record_id))
                        if db_row is None:
                            raise PipelineError(
                                "E_SNAPSHOT",
                                f"record {record_id} is absent from SQLite metadata",
                                source=source,
                            )
                        suggestion = None
                        if db_row.get("suggestion_rule"):
                            try:
                                suggestion = json.loads(str(db_row["suggestion_rule"]))
                            except json.JSONDecodeError as exc:
                                raise PipelineError(
                                    "E_SNAPSHOT",
                                    f"invalid suggestion_rule JSON for record {record_id}",
                                    source=source,
                                ) from exc
                            if not isinstance(suggestion, dict):
                                raise PipelineError(
                                    "E_SNAPSHOT",
                                    f"suggestion_rule for {record_id} must be an object",
                                    source=source,
                                )
                        shard_canonical = entry.get(
                            "canonical_id",
                            entry.get("canonical_input_id", entry.get("input_id")),
                        )
                        if (
                            shard_canonical is not None
                            and str(db_row.get("canonical_input_id"))
                            != str(shard_canonical)
                        ):
                            raise PipelineError(
                                "E_SNAPSHOT",
                                "SQLite canonical input id disagrees with shard",
                                source=source,
                            )
                        shard_criterion = entry.get(
                            "criterion", entry.get("name")
                        )
                        if (
                            shard_criterion is not None
                            and str(db_row.get("criterion")) != str(shard_criterion)
                        ):
                            raise PipelineError(
                                "E_SNAPSHOT",
                                "SQLite criterion disagrees with shard",
                                source=source,
                            )
                        if str(db_row.get("shard_id")) != str(shard["shard_id"]):
                            raise PipelineError(
                                "E_SNAPSHOT",
                                "SQLite record belongs to a different shard",
                                source=source,
                            )
                        try:
                            if (
                                "generation" in entry
                                or "algebra" in entry
                            ):
                                shard_generation = as_int(
                                    entry.get(
                                        "generation",
                                        entry.get("algebra", generation_default),
                                    ),
                                    "generation", minimum=1, code="E_SNAPSHOT",
                                )
                                if int(db_row.get("generation")) != shard_generation:
                                    raise PipelineError(
                                        "E_SNAPSHOT",
                                        "SQLite generation disagrees with shard",
                                        source=source,
                                    )
                            if (
                                "effective_ms" in entry
                                or "effective_at_ms" in entry
                                or "effective_seconds" in entry
                                or "effective_s" in entry
                            ):
                                shard_effective = entry.get(
                                    "effective_ms", entry.get("effective_at_ms")
                                )
                                if shard_effective is None:
                                    seconds = as_number(
                                        entry.get(
                                            "effective_seconds",
                                            entry.get("effective_s"),
                                        ),
                                        "effective_seconds", minimum=0.0,
                                        code="E_SNAPSHOT",
                                    )
                                    shard_effective = (
                                        shard_origin + int(round(seconds * 1000.0))
                                    )
                                if int(db_row.get("effective_ms")) != int(shard_effective):
                                    raise PipelineError(
                                        "E_SNAPSHOT",
                                        "SQLite effective_ms disagrees with shard",
                                        source=source,
                                    )
                            if "tombstone" in entry or "deleted" in entry:
                                shard_tombstone = entry.get(
                                    "tombstone", entry.get("deleted", False)
                                )
                                if bool(db_row.get("tombstone")) != bool(shard_tombstone):
                                    raise PipelineError(
                                        "E_SNAPSHOT",
                                        "SQLite tombstone disagrees with shard",
                                        source=source,
                                    )
                        except (PipelineError, TypeError, ValueError) as exc:
                            if isinstance(exc, PipelineError):
                                raise
                            raise PipelineError(
                                "E_SNAPSHOT",
                                f"invalid SQLite/shard numeric metadata: {exc}",
                                source=source,
                            ) from exc
                        db_metadata = {
                            "canonical_id": db_row.get("canonical_input_id"),
                            "criterion": db_row.get("criterion"),
                            "generation": db_row.get("generation"),
                            "effective_ms": db_row.get("effective_ms"),
                            "tombstone": bool(db_row.get("tombstone")),
                        }
                        if suggestion is not None:
                            db_metadata["suggestion_rule"] = suggestion
                        used_ids.add(str(record_id))
                    record, error = _record_from_entry(
                        entry,
                        generation_default=generation_default,
                        source=source,
                        sequence=len(records) + index,
                        origin_ms=shard_origin,
                        db_metadata=db_metadata,
                    )
                    if error is not None:
                        issues.append(_issue(error))
                        continue
                    assert record is not None
                    records.append(record)
                shard_summary["valid"] = True
                shard_summary["entries"] = len(entries)
            except PipelineError as exc:
                if exc.source is None:
                    exc.source = shard_source
                issues.append(_issue(exc))
                invalid_shards.add(str(shard["shard_id"]))
        unused = sorted(
            record_id for record_id, row in by_id.items()
            if record_id not in used_ids
            and str(row.get("shard_id")) not in invalid_shards
        )
        if unused:
            issues.append(_issue(PipelineError(
                "E_SNAPSHOT",
                "SQLite criteria rows are not represented by a valid shard",
                source={"kind": "v2-db", "path": str(cfg.v2_db_path)},
                details={"record_ids": unused[:20], "count": len(unused)},
            )))
        summary["valid"] = any(shard["valid"] for shard in summary["shards"])
        summary["entries"] = len(records)
        return records, summary
    except sqlite3.Error as exc:
        issues.append(_issue(PipelineError(
            "E_SQLITE", f"cannot query v2 metadata: {exc}", source=db_source
        )))
        return [], summary
    except (KeyError, TypeError, ValueError) as exc:
        issues.append(_issue(PipelineError(
            "E_SQLITE", f"v2 metadata schema is inconsistent: {exc}",
            source=db_source,
        )))
        return [], summary
    finally:
        connection.close()


def _measurement_fields(record: CriterionRecord) -> dict[str, Any]:
    if record.criterion not in ("window", "relative_gate", "absolute_gate"):
        return {}
    if record.criterion == "window":
        wanted = {"window_ms", "hop_ms", "policy", "partial_window_policy"}
    elif record.criterion == "relative_gate":
        wanted = {
            "threshold", "threshold_db", "threshold_lufs", "relative_gate_lu",
            "relative_gate_db",
        }
    else:
        wanted = {"threshold_lufs", "absolute_gate_lufs", "threshold_db"}
    return {key: value for key, value in record.data.items() if key in wanted}


def _decision_fields(record: CriterionRecord) -> dict[str, Any]:
    return {
        key: value for key, value in record.data.items()
        if key not in ("window_ms", "hop_ms", "policy", "partial_window_policy")
    }


def merge_snapshots(task: TaskSpec) -> UnifiedView:
    issues: list[dict[str, Any]] = []
    v1_records, v1_summary = _load_v1(task, issues)
    v2_records, v2_summary = _load_v2(task, issues)
    all_records = v1_records + v2_records

    grouped: dict[tuple[str, str], list[CriterionRecord]] = {}
    for record in all_records:
        grouped.setdefault((record.canonical_id, record.criterion), []).append(record)

    conflicts: list[dict[str, Any]] = []
    unified: dict[str, dict[str, CriterionRecord]] = {}
    for (canonical_id, criterion), group in sorted(grouped.items()):
        group.sort(key=lambda item: item.rank, reverse=True)
        winner = group[0]
        active = [item for item in group if not item.tombstone]
        v1_active = [item for item in active if item.source.get("kind") == "v1"]
        v2_active = [item for item in active if item.source.get("kind") == "v2-shard"]
        if v1_active and v2_active:
            older = max(v1_active, key=lambda item: item.rank)
            newer = max(v2_active, key=lambda item: item.rank)
            if older.threshold_signature != newer.threshold_signature:
                source_records = [older, newer]
                adjudicated = max(source_records, key=lambda item: item.rank)
                conflicts.append({
                    "canonical_id": canonical_id,
                    "criterion": criterion,
                    "resolution": "tombstone" if winner.tombstone else "selected",
                    "winner_generation": adjudicated.generation,
                    "winner_effective_ms": adjudicated.effective_ms,
                    "winner_source": adjudicated.source,
                    "sources": [
                        {
                            "generation": item.generation,
                            "effective_ms": item.effective_ms,
                            "data": item.data,
                            "source": item.source,
                        }
                        for item in sorted(source_records, key=lambda value: value.rank)
                    ],
                })
        if winner.tombstone:
            unified.setdefault(canonical_id, {}).pop(criterion, None)
            continue
        unified.setdefault(canonical_id, {})[criterion] = winner

    measurement_descriptor: list[dict[str, Any]] = []
    decision_descriptor: list[dict[str, Any]] = []
    for canonical_id, criteria in sorted(unified.items()):
        for criterion, record in sorted(criteria.items()):
            measurement_data = _measurement_fields(record)
            base = {
                "canonical_id": canonical_id,
                "criterion": criterion,
                "generation": record.generation,
                "effective_ms": record.effective_ms,
            }
            if measurement_data:
                measurement_descriptor.append({
                    **base,
                    "data": measurement_data,
                })
            decision_descriptor.append({
                **base,
                "data": _decision_fields(record),
            })
    measurement_descriptor.append({
        "task_defaults": {
            "window_ms": task.measurement.window_ms,
            "hop_ms": task.measurement.hop_ms,
            "relative_gate_lu": task.measurement.relative_gate_lu,
            "absolute_gate_lufs": task.measurement.absolute_gate_lufs,
            "partial_window_policy": task.measurement.partial_window_policy,
        }
    })
    measurement_revision = sha256_bytes(canonical_json_bytes(measurement_descriptor))
    decision_descriptor.append({
        "task_defaults": {
            "dynamic_range_min_db": task.measurement.dynamic_range_min_db,
            "dynamic_range_max_db": task.measurement.dynamic_range_max_db,
            "target_lufs": task.measurement.target_lufs,
            "compression_threshold_lufs": task.measurement.compression_threshold_lufs,
            "compression_ratio": task.measurement.compression_ratio,
            "target_sample_rate": task.measurement.target_sample_rate,
            "output_bit_depth": task.measurement.output_bit_depth,
        },
        "measurement_revision": measurement_revision,
    })
    decision_revision = sha256_bytes(canonical_json_bytes(decision_descriptor))
    snapshot_summary = {
        "v1": v1_summary,
        "v2": v2_summary,
        "measurement_revision": measurement_revision,
        "decision_revision": decision_revision,
        "records": len(unified),
        "conflicts": len(conflicts),
    }
    return UnifiedView(
        records=unified,
        conflicts=tuple(conflicts),
        issues=tuple(issues),
        snapshot_summary=snapshot_summary,
        measurement_revision=measurement_revision,
        decision_revision=decision_revision,
    )


def criterion_data(view: UnifiedView, canonical_id: str, criterion: str
                   ) -> dict[str, Any] | None:
    record = view.get(canonical_id, criterion)
    if record is None:
        record = view.get("*", criterion)
    return record.data if record is not None else None


def resolve_numeric(view: UnifiedView, canonical_id: str, criterion: str,
                    keys: Iterable[str], default: float) -> float:
    data = criterion_data(view, canonical_id, criterion) or {}
    for key in keys:
        if key in data:
            value = data[key]
            if finite_number(value):
                return float(value)
    return default


def resolve_window(view: UnifiedView, canonical_id: str, default_ms: float,
                   default_hop_ms: float) -> tuple[float, float, str]:
    data = criterion_data(view, canonical_id, "window") or {}
    window = data.get("window_ms")
    if not finite_number(window):
        for criterion in ("loudness", "integrated_loudness"):
            candidate = criterion_data(view, canonical_id, criterion) or {}
            if finite_number(candidate.get("window_ms")):
                data = candidate
                window = candidate["window_ms"]
                break
    hop = data.get("hop_ms")
    policy = data.get("policy", data.get("partial_window_policy"))
    return (
        float(window) if finite_number(window) else default_ms,
        float(hop) if finite_number(hop) else default_hop_ms,
        str(policy) if policy in ("zero_pad", "drop", "shorten")
        else "",
    )


def snapshot_source_index(view: UnifiedView) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for canonical_id, criteria in sorted(view.records.items()):
        for criterion, record in sorted(criteria.items()):
            result.append({
                "canonical_id": canonical_id,
                "criterion": criterion,
                "generation": record.generation,
                "effective_ms": record.effective_ms,
                "source": record.source,
                "data": record.data,
            })
    return result
