from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import (
    canonical_json,
    digest_file,
    digest_json,
    load_json,
    parse_json_text,
)
from .db import (
    FileLock,
    database_exists,
    initialize,
    open_existing,
    require_active_snapshot,
    transaction,
    get_meta,
    get_meta_int,
    set_meta,
)
from .errors import (
    E_INPUT_SCHEMA,
    E_SNAPSHOT_CONFLICT,
    E_SNAPSHOT_INCOMPLETE,
    E_SNAPSHOT_ROLLBACK,
    E_STATE_CORRUPT,
    fail,
)
from .state import (
    insert_experiment,
    insert_metric_record,
    load_experiments,
    load_metric_records,
    record_signature,
)
from .faults import maybe_fault
from .validation import (
    Experiment,
    MetricRecord,
    SourceEntry,
    V1Snapshot,
    V2Snapshot,
    validate_experiments,
    validate_metrics,
    validate_metric_reference_targets,
    validate_v1_snapshot,
    validate_v2_snapshot,
)
from .state import load_fingerprint_history


@dataclass
class PackageRef:
    package_path: str | None
    base_dir: str
    v1_path: str | None
    v2_manifest_path: str | None
    declared_snapshot_version: str | None
    declared_version_number: int | None
    declared_generation_floor: int | None
    declared_logical_clock_ms: int | None
    package_hash: str


@dataclass
class PreparedSnapshot:
    v1: V1Snapshot | None
    v2: V2Snapshot | None
    snapshot_version: str
    version_number: int
    trace_generation: int
    trace_generation_floor: int
    logical_clock_ms: int
    package_digest: str
    source_hashes: list[str]
    metric_digests: list[str]
    source_rows: list[dict[str, Any]]
    entries: list[SourceEntry]


@dataclass
class MergeDecision:
    selected: SourceEntry
    decision: dict[str, Any]
    candidates: list[dict[str, Any]]

    @property
    def rank(self) -> tuple[int, int, int]:
        return (
            self.selected.source_clock_ms,
            self.selected.trace_generation,
            self.selected.source_priority,
        )


def _resolve_package_child(base: str, value: Any, label: str) -> str:
    if isinstance(value, str):
        relative = value
    elif isinstance(value, dict):
        relative = value.get("path", value.get("file", value.get("manifest")))
    else:
        fail(E_INPUT_SCHEMA, f"{label} must be a path string or object")
    if not isinstance(relative, str) or not relative:
        fail(E_INPUT_SCHEMA, f"{label} path is missing")
    path = os.path.realpath(os.path.join(base, relative))
    root = os.path.realpath(base)
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        fail(E_INPUT_SCHEMA, f"{label} escapes its package directory", path=relative)
    return path


def parse_package_ref(
    *,
    package_path: str | None = None,
    v1_path: str | None = None,
    v2_manifest_path: str | None = None,
) -> PackageRef:
    declared_version: str | None = None
    declared_number: int | None = None
    declared_floor: int | None = None
    declared_clock: int | None = None
    if package_path:
        root = load_json(package_path)
        if not isinstance(root, dict):
            fail(E_INPUT_SCHEMA, "Snapshot package must be an object")
        package_dir = os.path.realpath(os.path.dirname(package_path))
        base_declared = root.get("base_dir", ".")
        if not isinstance(base_declared, str) or not base_declared:
            fail(E_INPUT_SCHEMA, "Package base_dir must be a non-empty string")
        base = os.path.realpath(os.path.join(package_dir, base_declared))
        if not os.path.isdir(base):
            fail(E_INPUT_SCHEMA, "Package base_dir does not exist", base_dir=base_declared)
        v1_ref = root.get("v1", root.get("legacy"))
        v2_ref = root.get("v2", root.get("manifest"))
        if v1_ref is None and v2_ref is None:
            fail(E_INPUT_SCHEMA, "Snapshot package must reference v1 and/or v2")
        if v1_ref is not None:
            v1_path = _resolve_package_child(base, v1_ref, "package v1")
            if isinstance(v1_ref, dict) and (v1_ref.get("sha256") or v1_ref.get("hash")):
                declared_hash = v1_ref.get("sha256", v1_ref.get("hash"))
                if not declared_hash.startswith("sha256:"):
                    declared_hash = "sha256:" + declared_hash
                actual_hash = digest_file(v1_path)
                if actual_hash != declared_hash:
                    fail(
                        E_SNAPSHOT_INCOMPLETE,
                        "Package v1 file hash does not match",
                        declared=declared_hash,
                        actual=actual_hash,
                    )
        if v2_ref is not None:
            v2_manifest_path = _resolve_package_child(base, v2_ref, "package v2 manifest")
            if isinstance(v2_ref, dict) and (v2_ref.get("sha256") or v2_ref.get("hash")):
                declared_hash = v2_ref.get("sha256", v2_ref.get("hash"))
                if not declared_hash.startswith("sha256:"):
                    declared_hash = "sha256:" + declared_hash
                actual_hash = digest_file(v2_manifest_path)
                if actual_hash != declared_hash:
                    fail(
                        E_SNAPSHOT_INCOMPLETE,
                        "Package v2 manifest hash does not match",
                        declared=declared_hash,
                        actual=actual_hash,
                    )
        declared_version = root.get("snapshot_version", root.get("version"))
        declared_number = root.get("version_number", root.get("manifest_version"))
        declared_floor = root.get(
            "tracking_generation_floor", root.get("generation_floor")
        )
        declared_clock = root.get("logical_clock_ms", root.get("logical_clock"))
        package_hash = digest_file(package_path)
        base_dir = base
    else:
        if not v1_path and not v2_manifest_path:
            fail(E_INPUT_SCHEMA, "Provide --package or at least one of --v1/--v2-manifest")
        base_dir = os.path.dirname(
            os.path.realpath(v2_manifest_path or v1_path)
        )
        package_hash = digest_json(
            {
                "v1": None if not v1_path else digest_file(v1_path),
                "v2": None
                if not v2_manifest_path
                else digest_file(v2_manifest_path),
            }
        )

    if declared_version is not None and not isinstance(declared_version, str):
        fail(E_INPUT_SCHEMA, "Package snapshot_version must be a string")
    if declared_number is not None and (
        isinstance(declared_number, bool) or not isinstance(declared_number, int)
    ):
        fail(E_INPUT_SCHEMA, "Package version_number must be an integer")
    if declared_floor is not None and (
        isinstance(declared_floor, bool)
        or not isinstance(declared_floor, int)
        or declared_floor < 0
    ):
        fail(E_INPUT_SCHEMA, "Package generation floor must be a non-negative integer")
    if declared_clock is not None and (
        isinstance(declared_clock, bool)
        or not isinstance(declared_clock, int)
        or declared_clock < 0
    ):
        fail(E_INPUT_SCHEMA, "Package logical clock must be a non-negative integer")
    return PackageRef(
        package_path=package_path,
        base_dir=base_dir,
        v1_path=v1_path,
        v2_manifest_path=v2_manifest_path,
        declared_snapshot_version=declared_version,
        declared_version_number=declared_number,
        declared_generation_floor=declared_floor,
        declared_logical_clock_ms=declared_clock,
        package_hash=package_hash,
    )


def prepare_snapshot(
    reference: PackageRef,
    experiments: Iterable[Experiment],
    *,
    explicit_snapshot_version: str | None = None,
    fingerprint_history: dict[str, set[str]] | None = None,
) -> PreparedSnapshot:
    v1 = (
        validate_v1_snapshot(
            reference.v1_path, experiments, fingerprint_history=fingerprint_history
        )
        if reference.v1_path
        else None
    )
    v2 = (
        validate_v2_snapshot(reference.v2_manifest_path, experiments)
        if reference.v2_manifest_path
        else None
    )
    if v1 is None and v2 is None:
        fail(E_INPUT_SCHEMA, "Snapshot contains neither v1 nor v2 data")
    snapshot_version = (
        explicit_snapshot_version
        or reference.declared_snapshot_version
        or (v2.snapshot_version if v2 else v1.snapshot_version)
    )
    version_number = (
        reference.declared_version_number
        if reference.declared_version_number is not None
        else (v2.version_number if v2 else v1.version_number)
    )
    trace_generation = max(
        [snapshot.trace_generation for snapshot in (v1, v2) if snapshot is not None]
    )
    trace_generation_floor = (
        reference.declared_generation_floor
        if reference.declared_generation_floor is not None
        else (v2.trace_generation_floor if v2 else v1.trace_generation)
    )
    if trace_generation_floor > trace_generation:
        fail(
            E_INPUT_SCHEMA,
            "Package generation floor exceeds its tracking generation",
            floor=trace_generation_floor,
            generation=trace_generation,
        )
    logical_clock_ms = (
        reference.declared_logical_clock_ms
        if reference.declared_logical_clock_ms is not None
        else max([snapshot.logical_clock_ms for snapshot in (v1, v2) if snapshot is not None])
    )
    entries = ([] if v1 is None else list(v1.entries)) + (
        [] if v2 is None else list(v2.entries)
    )
    source_hashes = []
    source_rows: list[dict[str, Any]] = []
    if v1 is not None:
        source_hashes.append(v1.file_hash)
        source_rows.append(
            {
                "source_format": "v1",
                "source_index": 0,
                "source_file": os.path.abspath(v1.path),
                "source_version": v1.snapshot_version,
                "source_clock_ms": v1.logical_clock_ms,
                "trace_generation": v1.trace_generation,
                "shard_hash": v1.file_hash,
                "priority": 1,
                "raw_summary_json": canonical_json(
                    {
                        "snapshot_version": v1.snapshot_version,
                        "logical_clock_ms": v1.logical_clock_ms,
                        "trace_generation": v1.trace_generation,
                        "entry_count": len(v1.entries),
                    }
                ),
            }
        )
    if v2 is not None:
        for index, shard_hash in enumerate(v2.shard_hashes):
            source_hashes.append(shard_hash)
            source_rows.append(
                {
                    "source_format": "v2",
                    "source_index": index,
                    "source_file": os.path.abspath(v2.shard_paths[index]),
                    "source_version": v2.snapshot_version,
                    "source_clock_ms": v2.shard_clocks[index],
                    "trace_generation": v2.shard_generations[index],
                    "shard_hash": shard_hash,
                    "priority": 2,
                    "raw_summary_json": canonical_json(
                        {
                            "snapshot_version": v2.snapshot_version,
                            "shard_index": index,
                            "logical_clock_ms": v2.shard_clocks[index],
                            "trace_generation": v2.shard_generations[index],
                            "shard_path": os.path.basename(v2.shard_paths[index]),
                        }
                    ),
                }
            )
    metric_digests = sorted({entry.metric_digest for entry in entries})
    package_digest = digest_json(
        {
            "snapshot_version": snapshot_version,
            "version_number": version_number,
            "trace_generation": trace_generation,
            "trace_generation_floor": trace_generation_floor,
            "logical_clock_ms": logical_clock_ms,
            "source_hashes": sorted(source_hashes),
            "metric_digests": metric_digests,
        }
    )
    return PreparedSnapshot(
        v1=v1,
        v2=v2,
        snapshot_version=snapshot_version,
        version_number=version_number,
        trace_generation=trace_generation,
        trace_generation_floor=trace_generation_floor,
        logical_clock_ms=logical_clock_ms,
        package_digest=package_digest,
        source_hashes=sorted(source_hashes),
        metric_digests=metric_digests,
        source_rows=source_rows,
        entries=entries,
    )


def _entry_rank(entry: SourceEntry) -> tuple[int, int, int]:
    return (entry.source_clock_ms, entry.trace_generation, entry.source_priority)


def _entry_json(entry: SourceEntry) -> dict[str, Any]:
    return {
        **entry.as_json(),
        "raw_json": entry.raw_json,
        "source_version": entry.source_version,
    }


def merge_snapshot_entries(entries: Iterable[SourceEntry]) -> list[MergeDecision]:
    groups: dict[tuple[str, str], list[SourceEntry]] = {}
    for entry in entries:
        groups.setdefault((entry.experiment_id, entry.metric), []).append(entry)
    results: list[MergeDecision] = []
    for group_key in sorted(groups):
        candidates = groups[group_key]
        ordered = sorted(
            candidates,
            key=lambda entry: (
                -entry.source_clock_ms,
                -entry.trace_generation,
                -entry.source_priority,
                entry.entry_key,
            ),
        )
        selected = ordered[0]
        runners_up = ordered[1:]
        if not runners_up:
            reason = "single_candidate"
        else:
            best_loser = runners_up[0]
            if selected.source_clock_ms != best_loser.source_clock_ms:
                reason = "higher_logical_clock"
            elif selected.trace_generation != best_loser.trace_generation:
                reason = "higher_trace_generation_at_equal_clock"
            elif selected.source_priority != best_loser.source_priority:
                reason = "higher_source_priority_at_equal_clock_and_generation"
            else:
                reason = "deterministic_entry_key_tiebreak"
        rejected = [
            {
                "entry_key": entry.entry_key,
                "source_format": entry.source_format,
                "source_version": entry.source_version,
                "rank": {
                    "logical_clock_ms": entry.source_clock_ms,
                    "trace_generation": entry.trace_generation,
                    "source_priority": entry.source_priority,
                },
                "metric_digest": entry.metric_digest,
                "absolute_refresh_at_ms": entry.absolute_refresh_at_ms,
                "reason": reason,
            }
            for entry in runners_up
        ]
        decision = {
            "rule": "logical_clock_then_trace_generation_then_source_priority_then_entry_key",
            "reason": reason,
            "selected": {
                "entry_key": selected.entry_key,
                "source_format": selected.source_format,
                "source_version": selected.source_version,
                "rank": {
                    "logical_clock_ms": selected.source_clock_ms,
                    "trace_generation": selected.trace_generation,
                    "source_priority": selected.source_priority,
                },
            },
            "rejected_candidates": rejected,
        }
        results.append(
            MergeDecision(
                selected=selected,
                decision=decision,
                candidates=[_entry_json(entry) for entry in ordered],
            )
        )
    return results


def _source_entry_from_row(row: sqlite3.Row) -> SourceEntry:
    return SourceEntry(
        source_format=str(row["source_format"]),
        entry_key=str(row["entry_key"]),
        alias=str(row["alias"]),
        experiment_id=str(row["experiment_id"]),
        metric=str(row["metric"]),
        relative_refresh_seconds=(
            None
            if row["relative_refresh_seconds"] is None
            else int(row["relative_refresh_seconds"])
        ),
        absolute_refresh_at_ms=int(row["absolute_refresh_at_ms"]),
        source_clock_ms=int(row["source_clock_ms"]),
        trace_generation=int(row["trace_generation"]),
        source_priority=int(row["source_priority"]),
        metric_digest=str(row["metric_digest"]),
        tombstone=bool(row["tombstone"]),
        conversion_scale=int(row["conversion_scale"]),
        conversion_remainder_ms=int(row["conversion_remainder_ms"]),
        rounding_direction=str(row["rounding_direction"]),
        raw_time_json=parse_json_text(str(row["raw_time_json"])),
        raw_json=str(row["raw_json"]),
        source_version=str(row["source_version"]),
    )


def _current_source_entries(
    connection: sqlite3.Connection, snapshot_version: str
) -> list[SourceEntry]:
    rows = connection.execute(
        """
        SELECT * FROM snapshot_source_entries
        WHERE snapshot_version = ?
        ORDER BY source_format, entry_key
        """,
        (snapshot_version,),
    ).fetchall()
    return [_source_entry_from_row(row) for row in rows]


def _compare_snapshot_versions(
    connection: sqlite3.Connection, prepared: PreparedSnapshot
) -> dict[str, Any]:
    active = get_meta(connection, "active_snapshot_version")
    if not active:
        return {
            "classification": "full_replace",
            "previous_snapshot_version": None,
            "checks": {"initial_load": True},
        }
    current = connection.execute(
        "SELECT * FROM snapshots WHERE snapshot_version = ?", (active,)
    ).fetchone()
    if current is None:
        fail(E_STATE_CORRUPT, "Active snapshot pointer references a missing snapshot", active=active)
    current_hashes = {
        str(row["shard_hash"])
        for row in connection.execute(
            "SELECT shard_hash FROM snapshot_sources WHERE snapshot_version = ?", (active,)
        )
    }
    current_digests = {
        str(row["metric_digest"])
        for row in connection.execute(
            "SELECT metric_digest FROM snapshot_view_entries WHERE snapshot_version = ?",
            (active,),
        )
    }
    checks = {
        "previous_version_number": int(current["version_number"]),
        "candidate_version_number": prepared.version_number,
        "previous_trace_generation": int(current["trace_generation"]),
        "previous_generation_floor": int(current["trace_generation_floor"]),
        "candidate_trace_generation": prepared.trace_generation,
        "candidate_generation_floor": prepared.trace_generation_floor,
        "previous_hashes": sorted(current_hashes),
        "candidate_hashes": prepared.source_hashes,
        "previous_metric_digests": sorted(current_digests),
        "candidate_metric_digests": prepared.metric_digests,
    }
    same_version = (
        prepared.snapshot_version == active
        or prepared.version_number == int(current["version_number"])
    )
    if same_version:
        if (
            prepared.snapshot_version == active
            and prepared.package_digest == str(current["package_digest"])
        ):
            return {
                "classification": "noop",
                "previous_snapshot_version": active,
                "checks": checks,
            }
        if prepared.snapshot_version == active:
            fail(
                E_SNAPSHOT_CONFLICT,
                "The same snapshot version was submitted with different content",
                snapshot_version=prepared.snapshot_version,
            )
        fail(
            E_SNAPSHOT_CONFLICT,
            "Snapshot version number is already active under a different version string",
            active_snapshot_version=active,
            candidate_snapshot_version=prepared.snapshot_version,
        )
    if prepared.version_number < int(current["version_number"]):
        fail(
            E_SNAPSHOT_ROLLBACK,
            "Snapshot version rollback is forbidden",
            active_version_number=int(current["version_number"]),
            candidate_version_number=prepared.version_number,
        )
    if prepared.trace_generation < int(current["trace_generation"]):
        fail(
            E_SNAPSHOT_ROLLBACK,
            "Snapshot tracking generation rollback is forbidden",
            active_trace_generation=int(current["trace_generation"]),
            candidate_trace_generation=prepared.trace_generation,
        )
    if prepared.trace_generation_floor < int(current["trace_generation_floor"]):
        fail(
            E_SNAPSHOT_ROLLBACK,
            "Snapshot tracking generation floor rollback is forbidden",
            active_trace_generation_floor=int(current["trace_generation_floor"]),
            candidate_trace_generation_floor=prepared.trace_generation_floor,
        )
    existing = connection.execute(
        "SELECT package_digest FROM snapshots WHERE snapshot_version = ?",
        (prepared.snapshot_version,),
    ).fetchone()
    if existing is not None:
        if str(existing["package_digest"]) == prepared.package_digest:
            return {
                "classification": "noop",
                "previous_snapshot_version": active,
                "checks": checks,
            }
        fail(
            E_SNAPSHOT_CONFLICT,
            "Snapshot version already exists with different content",
            snapshot_version=prepared.snapshot_version,
        )
    incremental = current_hashes.issubset(set(prepared.source_hashes)) and current_digests.issubset(
        set(prepared.metric_digests)
    )
    checks["current_hashes_are_subset"] = current_hashes.issubset(
        set(prepared.source_hashes)
    )
    checks["current_metric_digests_are_subset"] = current_digests.issubset(
        set(prepared.metric_digests)
    )
    return {
        "classification": "incremental_append" if incremental else "full_replace",
        "previous_snapshot_version": active,
        "checks": checks,
    }


def _compare_supplied_state(
    connection: sqlite3.Connection,
    experiments: list[Experiment] | None,
    records: list[MetricRecord] | None,
) -> None:
    if experiments is not None:
        stored = load_experiments(connection)
        supplied = {experiment.id: experiment for experiment in experiments}
        if set(stored) != set(supplied):
            fail(
                E_SNAPSHOT_CONFLICT,
                "Supplied experiments differ from the loaded workbench state",
            )
        for experiment_id, experiment in supplied.items():
            if (
                experiment.declaration() != stored[experiment_id].declaration()
                or experiment.aliases != stored[experiment_id].aliases
            ):
                fail(
                    E_SNAPSHOT_CONFLICT,
                    "Supplied experiment differs from the loaded workbench state",
                    experiment_id=experiment_id,
                )
    if records is not None:
        stored_records = load_metric_records(connection)
        if record_signature(stored_records) != record_signature(records):
            fail(
                E_SNAPSHOT_CONFLICT,
                "Supplied metric records differ from the loaded workbench state",
            )


def _insert_view_stage(
    connection: sqlite3.Connection,
    snapshot_version: str,
    decisions: list[MergeDecision],
) -> None:
    connection.execute(
        "CREATE TEMP TABLE stage_view AS SELECT * FROM snapshot_view_entries WHERE 0"
    )
    for decision in decisions:
        entry = decision.selected
        connection.execute(
            """
            INSERT INTO stage_view(
                snapshot_version, experiment_id, metric, alias,
                selected_source_format, selected_source_version, selected_entry_key,
                selected_clock_ms, selected_generation, selected_priority,
                relative_refresh_seconds, absolute_refresh_at_ms,
                conversion_scale, conversion_remainder_ms, rounding_direction,
                metric_digest, tombstone, raw_time_json, decision_json, candidates_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_version,
                entry.experiment_id,
                entry.metric,
                entry.alias,
                entry.source_format,
                entry.source_version,
                entry.entry_key,
                entry.source_clock_ms,
                entry.trace_generation,
                entry.source_priority,
                entry.relative_refresh_seconds,
                entry.absolute_refresh_at_ms,
                entry.conversion_scale,
                entry.conversion_remainder_ms,
                entry.rounding_direction,
                entry.metric_digest,
                1 if entry.tombstone else 0,
                canonical_json(entry.raw_time_json),
                canonical_json(decision.decision),
                canonical_json(decision.candidates),
            ),
        )
    connection.execute(
        "INSERT INTO snapshot_view_entries SELECT * FROM stage_view"
    )
    connection.execute("DROP TABLE stage_view")


def _insert_source_entries_stage(
    connection: sqlite3.Connection,
    snapshot_version: str,
    entries: list[SourceEntry],
) -> None:
    connection.execute(
        "CREATE TEMP TABLE stage_source_entries AS SELECT * FROM snapshot_source_entries WHERE 0"
    )
    deduplicated: dict[tuple[str, str], SourceEntry] = {}
    for entry in entries:
        deduplicated[(entry.source_format, entry.entry_key)] = entry
    for entry in deduplicated.values():
        connection.execute(
            """
            INSERT INTO stage_source_entries(
                snapshot_version, source_format, entry_key, source_version, alias,
                experiment_id, metric, relative_refresh_seconds, absolute_refresh_at_ms,
                source_clock_ms, trace_generation, source_priority, metric_digest,
                tombstone, conversion_scale, conversion_remainder_ms,
                rounding_direction, raw_time_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_version,
                entry.source_format,
                entry.entry_key,
                entry.source_version,
                entry.alias,
                entry.experiment_id,
                entry.metric,
                entry.relative_refresh_seconds,
                entry.absolute_refresh_at_ms,
                entry.source_clock_ms,
                entry.trace_generation,
                entry.source_priority,
                entry.metric_digest,
                1 if entry.tombstone else 0,
                entry.conversion_scale,
                entry.conversion_remainder_ms,
                entry.rounding_direction,
                canonical_json(entry.raw_time_json),
                entry.raw_json,
            ),
        )
    connection.execute(
        "INSERT INTO snapshot_source_entries SELECT * FROM stage_source_entries"
    )
    connection.execute("DROP TABLE stage_source_entries")


def load_tracking(
    db_path: str,
    *,
    package_path: str | None = None,
    v1_path: str | None = None,
    v2_manifest_path: str | None = None,
    experiments_path: str | None = None,
    metrics_path: str | None = None,
    explicit_snapshot_version: str | None = None,
    logical_clock_ms: int | None = None,
) -> dict[str, Any]:
    exists = database_exists(db_path)
    reference = parse_package_ref(
        package_path=package_path,
        v1_path=v1_path,
        v2_manifest_path=v2_manifest_path,
    )

    supplied_experiments: list[Experiment] | None = None
    supplied_records: list[MetricRecord] | None = None
    if experiments_path:
        supplied_experiments = validate_experiments(load_json(experiments_path))
    if metrics_path:
        if supplied_experiments is None:
            fail(
                E_INPUT_SCHEMA,
                "Loading metric records also requires the experiment lineage file",
            )
        supplied_records = validate_metrics(load_json(metrics_path), supplied_experiments)

    stored_experiments: dict[str, Experiment] = {}
    fingerprint_history: dict[str, set[str]] | None = None
    stored_records: list[MetricRecord] = []
    if exists:
        reader = open_existing(db_path, readonly=True)
        try:
            stored_experiments = load_experiments(reader)
            stored_records = load_metric_records(reader)
            fingerprint_history = load_fingerprint_history(reader)
            _compare_supplied_state(reader, supplied_experiments, supplied_records)
        finally:
            reader.close()
    elif supplied_experiments is None:
        fail(
            E_INPUT_SCHEMA,
            "Initial load requires --experiments and --metrics",
        )
    if not exists and supplied_records is None:
        fail(E_INPUT_SCHEMA, "Initial load requires --experiments and --metrics")

    experiments_for_validation = (
        supplied_experiments
        if supplied_experiments is not None
        else list(stored_experiments.values())
    )
    prepared = prepare_snapshot(
        reference,
        experiments_for_validation,
        explicit_snapshot_version=explicit_snapshot_version,
        fingerprint_history=fingerprint_history,
    )
    records_for_reference_validation = (
        supplied_records if supplied_records is not None else stored_records
    )
    known_metric_keys = {
        (record.experiment_id, record.metric)
        for record in records_for_reference_validation
    }
    known_metric_keys.update(
        (entry.experiment_id, entry.metric)
        for entry in prepared.entries
        if not entry.tombstone
    )
    known_metric_keys.update(
        (experiment.id, metric)
        for experiment in experiments_for_validation
        for metric in experiment.baseline_metrics
    )
    known_metric_keys.update(
        (experiment.id, metric)
        for experiment in experiments_for_validation
        for metric in experiment.metric_references
    )
    validate_metric_reference_targets(
        experiments_for_validation, known_metric_keys
    )
    if logical_clock_ms is not None:
        if prepared.logical_clock_ms > logical_clock_ms:
            fail(
                E_INPUT_SCHEMA,
                "Injected load clock is earlier than the snapshot logical clock",
                snapshot_clock_ms=prepared.logical_clock_ms,
                injected_clock_ms=logical_clock_ms,
            )
        prepared.logical_clock_ms = logical_clock_ms

    with FileLock(db_path):
        if not database_exists(db_path):
            initialize(db_path)
        connection = open_existing(db_path, require_active=False)
        try:
            compatibility = _compare_snapshot_versions(connection, prepared)
            if compatibility["classification"] == "noop":
                compatibility["operation"] = "load"
                compatibility["snapshot_version"] = prepared.snapshot_version
                compatibility["ok"] = True
                return compatibility

            with transaction(connection):
                fresh = not bool(get_meta(connection, "active_snapshot_version"))
                if fresh:
                    if supplied_experiments is None or supplied_records is None:
                        fail(
                            E_INPUT_SCHEMA,
                            "Initial load requires experiment lineage and metric records",
                        )
                    for experiment in supplied_experiments:
                        insert_experiment(connection, experiment, 0)
                    for record in supplied_records:
                        insert_metric_record(connection, record)
                else:
                    _compare_supplied_state(connection, supplied_experiments, supplied_records)

                classification = compatibility["classification"]
                previous = compatibility["previous_snapshot_version"]
                if classification == "incremental_append":
                    combined_entries = _current_source_entries(connection, previous) + list(
                        prepared.entries
                    )
                    combined_sources = [
                        {
                            "source_format": row["source_format"],
                            "source_index": row["source_index"],
                            "source_file": row["source_file"],
                            "source_version": row["source_version"],
                            "source_clock_ms": row["source_clock_ms"],
                            "trace_generation": row["trace_generation"],
                            "shard_hash": row["shard_hash"],
                            "priority": row["priority"],
                            "raw_summary_json": row["raw_summary_json"],
                        }
                        for row in connection.execute(
                            "SELECT * FROM snapshot_sources WHERE snapshot_version = ?",
                            (previous,),
                        )
                    ] + prepared.source_rows
                    source_entries_for_snapshot = combined_entries
                else:
                    combined_sources = prepared.source_rows
                    source_entries_for_snapshot = prepared.entries

                decisions = merge_snapshot_entries(source_entries_for_snapshot)
                source_summary = {
                    **compatibility,
                    "snapshot_version": prepared.snapshot_version,
                    "version_number": prepared.version_number,
                    "trace_generation": prepared.trace_generation,
                    "trace_generation_floor": prepared.trace_generation_floor,
                    "logical_clock_ms": prepared.logical_clock_ms,
                    "source_hashes": prepared.source_hashes,
                    "metric_digests": prepared.metric_digests,
                }
                connection.execute(
                    """
                    INSERT INTO snapshots(
                        snapshot_version, version_number, version_key_json,
                        logical_clock_ms, trace_generation, trace_generation_floor,
                        package_digest, classification, source_summary_json,
                        previous_snapshot_version, created_data_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prepared.snapshot_version,
                        prepared.version_number,
                        canonical_json(
                            _version_key(prepared.snapshot_version, prepared.version_number)
                        ),
                        prepared.logical_clock_ms,
                        prepared.trace_generation,
                        prepared.trace_generation_floor,
                        prepared.package_digest,
                        classification,
                        canonical_json(source_summary),
                        previous,
                        get_meta_int(connection, "data_epoch"),
                    ),
                )
                for source in combined_sources:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO snapshot_sources(
                            snapshot_version, source_format, source_index, source_file,
                            source_version, source_clock_ms, trace_generation,
                            shard_hash, priority, raw_summary_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            prepared.snapshot_version,
                            source["source_format"],
                            source["source_index"],
                            source["source_file"],
                            source["source_version"],
                            source["source_clock_ms"],
                            source["trace_generation"],
                            source["shard_hash"],
                            source["priority"],
                            source["raw_summary_json"],
                        ),
                    )
                _insert_source_entries_stage(
                    connection, prepared.snapshot_version, source_entries_for_snapshot
                )
                _insert_view_stage(connection, prepared.snapshot_version, decisions)
                maybe_fault("before_snapshot_pointer_switch")
                set_meta(connection, "active_snapshot_version", prepared.snapshot_version)
                current_clock = get_meta_int(connection, "logical_clock_watermark_ms")
                set_meta(
                    connection,
                    "logical_clock_watermark_ms",
                    max(current_clock, prepared.logical_clock_ms),
                )
                compatibility["ok"] = True
                compatibility["operation"] = "load"
                compatibility["snapshot_version"] = prepared.snapshot_version
                compatibility["merged_entry_count"] = len(decisions)
                compatibility["source_entry_count"] = len(source_entries_for_snapshot)
                return compatibility
        finally:
            connection.close()


def _version_key(version: str, number: int) -> dict[str, Any]:
    pieces: list[int] = []
    current = ""
    for character in version:
        if character.isdigit():
            current += character
        elif current:
            pieces.append(int(current))
            current = ""
    if current:
        pieces.append(int(current))
    return {"number": number, "numeric_parts": pieces, "text": version}


def snapshot_info(connection: sqlite3.Connection, snapshot_version: str | None = None) -> dict[str, Any]:
    active = snapshot_version or require_active_snapshot(connection)
    row = connection.execute(
        "SELECT * FROM snapshots WHERE snapshot_version = ?", (active,)
    ).fetchone()
    if row is None:
        fail(E_STATE_CORRUPT, "Snapshot metadata is missing", snapshot_version=active)
    return {
        "snapshot_version": row["snapshot_version"],
        "version_number": int(row["version_number"]),
        "logical_clock_ms": int(row["logical_clock_ms"]),
        "trace_generation": int(row["trace_generation"]),
        "trace_generation_floor": int(row["trace_generation_floor"]),
        "package_digest": row["package_digest"],
        "classification": row["classification"],
        "previous_snapshot_version": row["previous_snapshot_version"],
        "active": active == get_meta(connection, "active_snapshot_version"),
        "source_summary": parse_json_text(row["source_summary_json"]),
    }

