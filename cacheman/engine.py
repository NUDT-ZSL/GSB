"""Merge, replacement, reuse judgement, recovery, and verification engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    CACHE_ARTIFACT_MISSING,
    CACHE_DIGEST_MISMATCH,
    CACHE_GENERATION_REGRESSION,
    CACHE_INDEX_EMPTY,
    CACHE_INTERNAL_ERROR,
    CACHE_MANIFEST_VERSION_ROLLBACK,
    CACHE_STATE_CORRUPT,
    CacheError,
)
from .indexer import (
    build_snapshot,
    candidate_from_entry,
    tombstone_from_dict,
)
from .loader import dataset_input_hash, load_dataset, load_dataset_for_clock
from .models import Candidate, LoadedDataset, SourceInfo, Tombstone
from .pipeline import Pipeline, load_pipelines
from .store import ConcurrentStateChange, StateStore
from .util import (
    canonical_json,
    object_digest,
    sha256_bytes,
)


_MAX_STATE_RETRIES = 3


def _push_fault(fault_point: str | None, configured: str | None) -> None:
    if fault_point is not None and configured == fault_point:
        os._exit(75)


def _source_info_from_dict(value: dict[str, Any]) -> SourceInfo:
    return SourceInfo(
        source_id=str(value["source_id"]),
        source_version=int(value["source_version"]),
        manifest_id=str(value["manifest_id"]),
        manifest_version=int(value["manifest_version"]),
        logical_clock_ms=int(value["logical_clock_ms"]),
        source_root=str(value["source_root"]),
        source_hash=str(value["source_hash"]),
        generation_min=value.get("generation_min"),
        generation_max=value.get("generation_max"),
        shard_hashes=tuple(str(item) for item in value.get("shard_hashes", [])),
        descriptor=dict(value.get("descriptor", {})),
    )


def _combined_input_hash(
    sources: Iterable[SourceInfo],
    tombstones: Iterable[Tombstone],
    *,
    kind: str,
) -> str:
    source_by_id: dict[str, dict[str, Any]] = {}
    for source in sources:
        source_by_id[source.source_id] = source.to_dict()
    tombstone_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for tombstone in tombstones:
        value = tombstone.to_dict()
        tombstone_by_key[
            (
                value["task_key"],
                value["artifact"],
                value["digest"],
                value["source_id"],
            )
        ] = value
    return object_digest(
        {
            "kind": kind,
            "sources": [source_by_id[key] for key in sorted(source_by_id)],
            "tombstones": [tombstone_by_key[key] for key in sorted(tombstone_by_key)],
        }
    )


def _merge_report(
    *,
    operation: str,
    status: str,
    snapshot: dict[str, Any],
    source_input_hash: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = {
        "ok": True,
        "operation": operation,
        "status": status,
        "index_version": int(snapshot["index_version"]),
        "input_hash": source_input_hash,
        "data_digest": snapshot["data_digest"],
        "logical_clock_ms": int(snapshot["logical_clock_ms"]),
        "manifest_version": int(snapshot["manifest_version"]),
        "cache_generation_floor": int(snapshot["cache_generation_floor"]),
        "cache_generation_ceiling": int(snapshot["cache_generation_ceiling"]),
        "source_versions": [
            {
                "source_id": source["source_id"],
                "source_version": source["source_version"],
                "manifest_id": source["manifest_id"],
                "manifest_version": source["manifest_version"],
                "generation_min": source["generation_min"],
                "generation_max": source["generation_max"],
            }
            for source in snapshot["sources"]
        ],
        "summary": snapshot["summary"],
    }
    if extra:
        report.update(extra)
    return report


def _snapshot_manifest_version(snapshot: dict[str, Any]) -> int:
    return int(snapshot["manifest_version"])


def _snapshot_generation_floor(snapshot: dict[str, Any]) -> int:
    return int(snapshot["cache_generation_floor"])


def execute_merge(
    store: StateStore,
    *,
    v1_paths: list[str],
    v2_paths: list[str],
    now_ms: int | None,
    clock_tolerance_ms: int,
    fault_point: str | None = None,
    dataset: LoadedDataset | None = None,
) -> dict[str, Any]:
    if dataset is None:
        dataset, effective_now = load_dataset_for_clock(
            v1_paths=v1_paths,
            v2_paths=v2_paths,
            now_ms=now_ms,
        )
    else:
        effective_now = dataset.default_now_ms() if now_ms is None else now_ms
    input_hash = dataset_input_hash(dataset)
    payload = {
        "kind": "merge",
        "v1_paths": list(v1_paths),
        "v2_paths": list(v2_paths),
        "now_ms": effective_now,
        "clock_tolerance_ms": clock_tolerance_ms,
    }
    last_concurrent: dict[str, Any] | None = None
    for _ in range(_MAX_STATE_RETRIES):
        current = store.current_snapshot()
        base_version = 0 if current is None else int(current["index_version"])
        target_version = base_version + 1
        snapshot = build_snapshot(
            candidates=dataset.candidates,
            tombstones=dataset.tombstones,
            sources=dataset.sources,
            now_ms=effective_now,
            index_version=target_version,
            previous_index_version=None if current is None else base_version,
            input_hash=input_hash,
        )
        op_id = store.make_operation_id("merge", input_hash, base_version)
        prepared = store.prepare_operation(
            op_id=op_id,
            kind="merge",
            base_index_version=base_version,
            target_index_version=target_version,
            input_hash=input_hash,
            payload=payload,
            now_ms=effective_now,
            clock_tolerance_ms=clock_tolerance_ms,
            manifest_version=_snapshot_manifest_version(snapshot),
            generation_floor=_snapshot_generation_floor(snapshot),
        )
        if prepared["status"] == "noop":
            current_snapshot = store.current_snapshot()
            assert current_snapshot is not None
            return _merge_report(
                operation="merge",
                status="noop",
                snapshot=current_snapshot,
                source_input_hash=input_hash,
            )
        if prepared["status"] == "concurrent_change":
            last_concurrent = prepared
            continue
        _push_fault("after_prepare", fault_point)
        staged = store.stage_snapshot(op_id, snapshot)
        if staged["status"] in {"superseded", "completed"}:
            last_concurrent = staged
            continue
        _push_fault("after_stage", fault_point)
        published = store.publish_staged(op_id)
        if published["status"] == "superseded":
            last_concurrent = published
            continue
        current_snapshot = store.current_snapshot()
        assert current_snapshot is not None
        status = "committed" if published["status"] == "published" else "noop"
        return _merge_report(
            operation="merge",
            status=status,
            snapshot=current_snapshot,
            source_input_hash=input_hash,
            extra={"operation_id": op_id},
        )
    raise CacheError(
        CACHE_INTERNAL_ERROR,
        "merge could not obtain a stable index version after concurrent updates",
        fields={"last_concurrent_result": last_concurrent},
    )


def classify_replacement(
    current: dict[str, Any] | None,
    incoming: LoadedDataset,
) -> dict[str, Any]:
    incoming_input_hash = dataset_input_hash(incoming)
    if current is None:
        return {
            "kind": "full_replacement",
            "reason": "no_committed_index",
            "incoming_input_hash": incoming_input_hash,
            "added_shard_hashes": sorted(
                {
                    digest
                    for source in incoming.sources
                    for digest in source.shard_hashes
                }
            ),
            "removed_shard_hashes": [],
        }
    if incoming_input_hash == current["input_hash"]:
        return {
            "kind": "noop",
            "reason": "identical_source_set",
            "incoming_input_hash": incoming_input_hash,
            "added_shard_hashes": [],
            "removed_shard_hashes": [],
        }
    incoming_manifest_version = max(
        (source.manifest_version for source in incoming.sources),
        default=0,
    )
    current_manifest_version = int(current["manifest_version"])
    if incoming_manifest_version < current_manifest_version:
        raise CacheError(
            CACHE_MANIFEST_VERSION_ROLLBACK,
            "incoming cache package manifest version would roll back the index",
            fields={
                "incoming_manifest_version": incoming_manifest_version,
                "committed_manifest_version": current_manifest_version,
                "committed_index_version": current["index_version"],
            },
        )
    incoming_floor = min(
        (
            source.generation_min
            for source in incoming.sources
            if source.source_version == 2 and source.generation_min is not None
        ),
        default=0,
    )
    current_floor = int(current["cache_generation_floor"])
    if incoming_floor < current_floor:
        raise CacheError(
            CACHE_GENERATION_REGRESSION,
            "incoming cache package generation floor would roll back the index",
            fields={
                "incoming_generation_floor": incoming_floor,
                "committed_generation_floor": current_floor,
                "committed_index_version": current["index_version"],
            },
        )
    current_hashes = {
        str(value)
        for source in current["sources"]
        for value in source.get("shard_hashes", [])
    }
    incoming_hashes = {
        digest for source in incoming.sources for digest in source.shard_hashes
    }
    added = sorted(incoming_hashes - current_hashes)
    removed = sorted(current_hashes - incoming_hashes)
    current_sources = {source["source_id"]: source for source in current["sources"]}
    incoming_sources = {source.source_id: source for source in incoming.sources}
    changed_source_ids = sorted(
        source_id
        for source_id in set(current_sources) & set(incoming_sources)
        if current_sources[source_id].get("source_hash")
        != incoming_sources[source_id].source_hash
    )
    removed_source_ids = sorted(set(current_sources) - set(incoming_sources))
    current_ceiling = int(current["cache_generation_ceiling"])

    if not added and not removed and not changed_source_ids and not removed_source_ids:
        return {
            "kind": "noop",
            "reason": "same_manifest_generation_and_shards",
            "incoming_input_hash": incoming_input_hash,
            "added_shard_hashes": [],
            "removed_shard_hashes": [],
        }
    if (
        incoming_floor > current_ceiling
        and incoming_hashes
        and not (incoming_hashes & current_hashes)
    ):
        return {
            "kind": "full_replacement",
            "reason": "generation_floor_above_current_ceiling_and_disjoint_shards",
            "incoming_input_hash": incoming_input_hash,
            "added_shard_hashes": added,
            "removed_shard_hashes": removed,
        }
    if (
        current_hashes
        and current_hashes.issubset(incoming_hashes)
        and incoming_floor <= current_ceiling
    ):
        return {
            "kind": "incremental",
            "reason": "existing_shards_retained_with_additions",
            "incoming_input_hash": incoming_input_hash,
            "added_shard_hashes": added,
            "removed_shard_hashes": removed,
        }
    if (
        changed_source_ids
        and not removed_source_ids
        and not removed
        and incoming_floor <= current_ceiling
    ):
        return {
            "kind": "incremental",
            "reason": "same_shards_with_changed_source_metadata",
            "incoming_input_hash": incoming_input_hash,
            "added_shard_hashes": added,
            "removed_shard_hashes": removed,
            "changed_source_ids": changed_source_ids,
        }
    if (
        incoming_manifest_version == current_manifest_version
        and added
        and not removed
        and not removed_source_ids
    ):
        return {
            "kind": "incremental",
            "reason": "same_manifest_version_with_new_shards",
            "incoming_input_hash": incoming_input_hash,
            "added_shard_hashes": added,
            "removed_shard_hashes": removed,
        }
    return {
        "kind": "full_replacement",
        "reason": "source_set_or_generation_window_replaced",
        "incoming_input_hash": incoming_input_hash,
        "added_shard_hashes": added,
        "removed_shard_hashes": removed,
        "changed_source_ids": changed_source_ids,
        "removed_source_ids": removed_source_ids,
    }


def _combine_incremental(
    current: dict[str, Any],
    incoming: LoadedDataset,
) -> tuple[LoadedDataset, str]:
    incoming_source_ids = {source.source_id for source in incoming.sources}
    current_sources = [
        _source_info_from_dict(source)
        for source in current["sources"]
        if source["source_id"] not in incoming_source_ids
    ]
    sources_by_id = {source.source_id: source for source in current_sources}
    for source in incoming.sources:
        sources_by_id[source.source_id] = source
    sources = [sources_by_id[source_id] for source_id in sorted(sources_by_id)]

    candidates: list[Candidate] = [
        candidate_from_entry(entry)
        for entry in current["entries"]
        if entry["source_id"] not in incoming_source_ids
    ]
    candidates.extend(incoming.candidates)
    tombstones: list[Tombstone] = [
        tombstone_from_dict(value)
        for value in current["tombstones"]
        if value["source_id"] not in incoming_source_ids
    ]
    tombstones.extend(incoming.tombstones)
    dataset = LoadedDataset(
        candidates=candidates,
        tombstones=tombstones,
        sources=sources,
    )
    return dataset, _combined_input_hash(
        sources,
        tombstones,
        kind="incremental",
    )


def _entry_semantics(entry: dict[str, Any]) -> str:
    return canonical_json(
        {
            "digests": sorted(entry["digests"]),
            "size_bytes": entry["size_bytes"],
            "absolute_written_ms": entry["absolute_written_ms"],
            "tombstoned": entry["tombstoned"],
        }
    )


def changed_tasks(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[str]:
    if previous is None:
        return []
    previous_by_task: dict[str, list[str]] = {}
    for entry in previous["entries"]:
        previous_by_task.setdefault(entry["task_key"], []).append(_entry_semantics(entry))
    current_by_task: dict[str, list[str]] = {}
    for entry in current["entries"]:
        current_by_task.setdefault(entry["task_key"], []).append(_entry_semantics(entry))
    changed = []
    for task_key in sorted(set(previous_by_task) | set(current_by_task)):
        if sorted(previous_by_task.get(task_key, [])) != sorted(
            current_by_task.get(task_key, [])
        ):
            changed.append(task_key)
    return changed


def _affected_tasks(
    pipelines: Iterable[Pipeline],
    changed: Iterable[str],
) -> dict[str, list[str]]:
    changed_list = list(changed)
    return {
        pipeline.pipeline_id: pipeline.downstream_closure(changed_list)
        for pipeline in sorted(pipelines, key=lambda item: item.pipeline_id)
    }


def execute_replace(
    store: StateStore,
    *,
    package_path: str,
    package_descriptor: dict[str, Any],
    incoming: LoadedDataset,
    pipelines: list[Pipeline],
    now_ms: int | None,
    clock_tolerance_ms: int,
    fault_point: str | None = None,
) -> dict[str, Any]:
    effective_now = (
        max(
            store.logical_clock_watermark(),
            int(package_descriptor["logical_clock_ms"]),
        )
        if now_ms is None
        else now_ms
    )
    store.check_clock(effective_now, clock_tolerance_ms)
    last_concurrent: dict[str, Any] | None = None
    for _ in range(_MAX_STATE_RETRIES):
        current = store.current_snapshot()
        classification = classify_replacement(current, incoming)
        kind = classification["kind"]
        if kind == "noop":
            assert current is not None
            changed = changed_tasks(
                store.get_snapshot(int(current["previous_index_version"]))
                if current.get("previous_index_version")
                else None,
                current,
            )
            return {
                "ok": True,
                "operation": "replace",
                "status": "noop",
                "replacement_kind": "noop",
                "replacement_reason": classification["reason"],
                "index_version": int(current["index_version"]),
                "input_hash": current["input_hash"],
                "data_digest": current["data_digest"],
                "added_shard_hashes": [],
                "removed_shard_hashes": [],
                "affected_tasks": _affected_tasks(pipelines, changed),
            }
        if kind == "incremental":
            assert current is not None
            dataset, input_hash = _combine_incremental(current, incoming)
            reason = classification["reason"]
        else:
            dataset = incoming
            input_hash = object_digest(
                {
                    "kind": "full_replacement",
                    "package_id": package_descriptor["package_id"],
                    "sources": incoming.semantic_descriptor()["sources"],
                    "tombstones": incoming.semantic_descriptor()["tombstones"],
                }
            )
            reason = classification["reason"]
        base_version = 0 if current is None else int(current["index_version"])
        target_version = base_version + 1
        snapshot = build_snapshot(
            candidates=dataset.candidates,
            tombstones=dataset.tombstones,
            sources=dataset.sources,
            now_ms=effective_now,
            index_version=target_version,
            previous_index_version=None if current is None else base_version,
            input_hash=input_hash,
        )
        op_kind = "replace:" + kind
        op_id = store.make_operation_id(op_kind, input_hash, base_version)
        payload = {
            "kind": "replace",
            "package_path": package_path,
            "now_ms": effective_now,
            "clock_tolerance_ms": clock_tolerance_ms,
            "pipelines": [pipeline.source_path for pipeline in pipelines],
        }
        prepared = store.prepare_operation(
            op_id=op_id,
            kind=op_kind,
            base_index_version=base_version,
            target_index_version=target_version,
            input_hash=input_hash,
            payload=payload,
            now_ms=effective_now,
            clock_tolerance_ms=clock_tolerance_ms,
            manifest_version=int(package_descriptor["manifest_version"]),
            generation_floor=int(package_descriptor["cache_generation_floor"]),
        )
        if prepared["status"] == "noop":
            current_snapshot = store.current_snapshot()
            assert current_snapshot is not None
            return _merge_report(
                operation="replace",
                status="noop",
                snapshot=current_snapshot,
                source_input_hash=input_hash,
                extra={"replacement_kind": "noop", "replacement_reason": "idempotent"},
            )
        if prepared["status"] == "concurrent_change":
            last_concurrent = prepared
            continue
        _push_fault("after_prepare", fault_point)
        staged = store.stage_snapshot(op_id, snapshot)
        if staged["status"] in {"superseded", "completed"}:
            last_concurrent = staged
            continue
        _push_fault("after_stage", fault_point)
        published = store.publish_staged(op_id)
        if published["status"] == "superseded":
            last_concurrent = published
            continue
        current_snapshot = store.current_snapshot()
        assert current_snapshot is not None
        previous_snapshot = (
            store.get_snapshot(int(current_snapshot["previous_index_version"]))
            if current_snapshot.get("previous_index_version")
            else None
        )
        changed = changed_tasks(previous_snapshot, current_snapshot)
        status = "committed" if published["status"] == "published" else "noop"
        return _merge_report(
            operation="replace",
            status=status,
            snapshot=current_snapshot,
            source_input_hash=input_hash,
            extra={
                "operation_id": op_id,
                "replacement_kind": kind,
                "replacement_reason": reason,
                "added_shard_hashes": classification["added_shard_hashes"],
                "removed_shard_hashes": classification["removed_shard_hashes"],
                "affected_tasks": _affected_tasks(pipelines, changed),
            },
        )
    raise CacheError(
        CACHE_INTERNAL_ERROR,
        "replace could not obtain a stable index version after concurrent updates",
        fields={"last_concurrent_result": last_concurrent},
    )


def _artifact_evaluation(
    *,
    artifact_name_value: str,
    artifact_entry: dict[str, Any] | None,
    ttl_seconds: int | None,
    now_ms: int,
) -> dict[str, Any]:
    if artifact_entry is None:
        return {
            "artifact": artifact_name_value,
            "status": "missing",
            "reusable": False,
            "reason": "artifact_missing",
            "ttl_seconds": ttl_seconds,
        }
    expires_at_ms = (
        None
        if ttl_seconds is None
        else int(artifact_entry["absolute_written_ms"]) + ttl_seconds * 1000
    )
    if artifact_entry["tombstoned"]:
        status = "tombstoned"
        reason = "artifact_tombstoned"
    elif expires_at_ms is not None and now_ms >= expires_at_ms:
        status = "expired"
        reason = "artifact_expired"
    else:
        status = "reusable"
        reason = "reusable"
    return {
        "artifact": artifact_name_value,
        "status": status,
        "reusable": status == "reusable",
        "reason": reason,
        "ttl_seconds": ttl_seconds,
        "expires_at_ms": expires_at_ms,
        "absolute_written_ms": int(artifact_entry["absolute_written_ms"]),
        "digests": list(artifact_entry["digests"]),
        "source_version": int(artifact_entry["source_version"]),
        "source_id": artifact_entry["source_id"],
        "source_manifest_version": int(artifact_entry["source_manifest_version"]),
        "source_cache_generation": artifact_entry["source_cache_generation"],
        "source_hit_tag": artifact_entry["source_hit_tag"],
        "time_precision": artifact_entry["precision_tag"],
        "tombstones": artifact_entry.get("matching_tombstones", []),
    }


def _task_source_versions(artifacts: list[dict[str, Any]]) -> list[str]:
    versions: set[str] = set()
    for artifact in artifacts:
        if artifact.get("source_version") == 1:
            versions.add(f"v1:{artifact['source_manifest_version']}")
        elif artifact.get("source_version") == 2:
            generation = artifact.get("source_cache_generation")
            versions.add(f"v2:{artifact['source_manifest_version']}/gen{generation}")
    return sorted(versions)


def _task_precision_tags(artifacts: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(artifact["time_precision"])
            for artifact in artifacts
            if artifact.get("time_precision")
        }
    )


def _decision_fingerprint(result: dict[str, Any]) -> str:
    return object_digest(
        {
            "task_key": result["task_key"],
            "reusable": result["reusable"],
            "reason": result["reason"],
            "dependency_path": result["dependency_path"],
            "artifacts": [
                {
                    "artifact": artifact["artifact"],
                    "status": artifact["status"],
                    "reason": artifact["reason"],
                }
                for artifact in result["artifacts"]
            ],
        }
    )


def _artifact_input_projection(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "digests": sorted(entry["digests"]),
        "size_bytes": int(entry["size_bytes"]),
        "absolute_written_ms": int(entry["absolute_written_ms"]),
        "tombstoned": bool(entry["tombstoned"]),
        "tombstones": [
            {
                "digest": tombstone["digest"],
                "invalidated_at_ms": int(tombstone["invalidated_at_ms"]),
            }
            for tombstone in entry.get("matching_tombstones", [])
        ],
    }


def execute_judge(
    store: StateStore,
    *,
    pipelines: list[Pipeline],
    now_ms: int | None,
    clock_tolerance_ms: int,
) -> dict[str, Any]:
    current = store.current_snapshot()
    if current is None:
        raise CacheError(
            CACHE_INDEX_EMPTY,
            "no committed index exists; run merge before judge",
            fields={},
        )
    effective_now = (
        int(current["logical_clock_ms"]) if now_ms is None else now_ms
    )
    store.check_clock(effective_now, clock_tolerance_ms)
    report_key = object_digest(
        {
            "engine": "reuse-judge-v1",
            "index_version": int(current["index_version"]),
            "now_ms": effective_now,
            "clock_tolerance_ms": clock_tolerance_ms,
            "pipelines": [
                {
                    "pipeline_id": pipeline.pipeline_id,
                    "pipeline_version": pipeline.pipeline_version,
                    "fingerprint": pipeline.fingerprint,
                }
                for pipeline in pipelines
            ],
        }
    )
    existing = store.get_judgement_report(report_key)
    if existing is not None:
        return json.loads(existing)

    previous = (
        store.get_snapshot(int(current["previous_index_version"]))
        if current.get("previous_index_version")
        else None
    )
    changed = changed_tasks(previous, current)
    artifact_index = {
        (entry["task_key"], entry["artifact"]): entry
        for entry in current["entries"]
    }
    cache_records: list[dict[str, Any]] = []
    pipeline_reports: list[dict[str, Any]] = []
    invalidation_records: list[dict[str, Any]] = []
    total_reusable = 0
    total_blocked = 0
    total_reused_decisions = 0

    for pipeline in pipelines:
        results: list[dict[str, Any]] = []
        result_by_task: dict[str, dict[str, Any]] = {}
        pipeline_reused = 0
        for task_key in pipeline.topological_order:
            task = pipeline.tasks[task_key]
            artifact_details: list[dict[str, Any]] = []
            for artifact_spec in task.artifacts:
                artifact_details.append(
                    _artifact_evaluation(
                        artifact_name_value=artifact_spec.name,
                        artifact_entry=artifact_index.get((task_key, artifact_spec.name)),
                        ttl_seconds=artifact_spec.ttl_seconds,
                        now_ms=effective_now,
                    )
                )
            dependency_states = [
                {
                    "task_key": dependency,
                    "reusable": result_by_task[dependency]["reusable"],
                    "reason": result_by_task[dependency]["reason"],
                    "decision_fingerprint": result_by_task[dependency][
                        "decision_fingerprint"
                    ],
                }
                for dependency in task.depends_on
            ]
            input_material = {
                "pipeline_id": pipeline.pipeline_id,
                "pipeline_version": pipeline.pipeline_version,
                "task": task.to_dict(),
                "now_ms": effective_now,
                "artifacts": [
                    _artifact_input_projection(
                        artifact_index.get((task_key, artifact_spec.name))
                    )
                    for artifact_spec in task.artifacts
                ],
                "dependencies": dependency_states,
            }
            task_input_hash = object_digest(input_material)
            cached = store.lookup_judgement(
                pipeline.pipeline_id,
                pipeline.pipeline_version,
                task_key,
                task_input_hash,
            )
            reused = cached is not None
            if cached is not None:
                cached_from = int(cached.pop("_cached_from_index_version"))
                result = cached
                result["artifacts"] = artifact_details
                result["source_cache_versions"] = _task_source_versions(artifact_details)
                result["precision_tags"] = _task_precision_tags(artifact_details)
                pipeline_reused += 1
                total_reused_decisions += 1
            else:
                cached_from = None
                upstream_failure = next(
                    (
                        result_by_task[dependency]
                        for dependency in task.depends_on
                        if not result_by_task[dependency]["reusable"]
                    ),
                    None,
                )
                own_failure = next(
                    (
                        artifact
                        for artifact in artifact_details
                        if not artifact["reusable"]
                    ),
                    None,
                )
                if upstream_failure is not None:
                    first_trigger = {
                        "kind": "upstream_not_reusable",
                        "dependency": upstream_failure["task_key"],
                        "dependency_reason": upstream_failure["reason"],
                    }
                    dependency_path = list(upstream_failure["dependency_path"]) + [
                        task_key
                    ]
                    reusable = False
                    reason = "upstream_not_reusable"
                elif own_failure is not None:
                    first_trigger = {
                        "kind": own_failure["reason"],
                        "artifact": own_failure["artifact"],
                        "status": own_failure["status"],
                    }
                    dependency_path = [task_key]
                    reusable = False
                    reason = own_failure["reason"]
                else:
                    first_trigger = None
                    dependency_path = [task_key]
                    reusable = True
                    reason = "reusable"
                result = {
                    "task_key": task_key,
                    "reusable": reusable,
                    "reason": reason,
                    "first_trigger": first_trigger,
                    "dependency_path": dependency_path,
                    "artifacts": artifact_details,
                    "source_cache_versions": _task_source_versions(artifact_details),
                    "precision_tags": _task_precision_tags(artifact_details),
                    "dependencies": dependency_states,
                    "evaluated_index_version": int(current["index_version"]),
                    "evaluated_at_ms": effective_now,
                }
                result["decision_fingerprint"] = _decision_fingerprint(result)
                cache_records.append(
                    {
                        "pipeline_id": pipeline.pipeline_id,
                        "pipeline_version": pipeline.pipeline_version,
                        "task_key": task_key,
                        "input_hash": task_input_hash,
                        "result": result,
                    }
                )
            result = dict(result)
            result["index_version"] = int(current["index_version"])
            result["decision_reused"] = reused
            result["cached_from_index_version"] = cached_from
            if result["reusable"]:
                total_reusable += 1
            else:
                total_blocked += 1
            results.append(result)
            result_by_task[task_key] = result
        affected = pipeline.downstream_closure(changed) if changed else []
        for task_key in affected:
            invalidation_records.append(
                {
                    "pipeline_id": pipeline.pipeline_id,
                    "pipeline_version": pipeline.pipeline_version,
                    "task_key": task_key,
                    "reason": "index_change",
                }
            )
        pipeline_reports.append(
            {
                "pipeline_id": pipeline.pipeline_id,
                "pipeline_version": pipeline.pipeline_version,
                "pipeline_fingerprint": pipeline.fingerprint,
                "topological_order": list(pipeline.topological_order),
                "results": results,
                "invalidated_tasks": affected,
                "reused_decision_count": pipeline_reused,
            }
        )

    report = {
        "ok": True,
        "operation": "judge",
        "format": "cacheman-judge-v1",
        "index_version": int(current["index_version"]),
        "logical_clock_ms": effective_now,
        "logical_clock_watermark_ms": max(
            store.logical_clock_watermark(), effective_now
        ),
        "changed_tasks": changed,
        "pipelines": pipeline_reports,
        "summary": {
            "pipeline_count": len(pipeline_reports),
            "task_count": total_reusable + total_blocked,
            "reusable_task_count": total_reusable,
            "blocked_task_count": total_blocked,
            "reused_decision_count": total_reused_decisions,
            "invalidated_task_count": sum(
                len(pipeline["invalidated_tasks"]) for pipeline in pipeline_reports
            ),
        },
    }
    for _ in range(_MAX_STATE_RETRIES):
        try:
            report_json = store.commit_judgements(
                expected_index_version=int(current["index_version"]),
                logical_clock_ms=effective_now,
                clock_tolerance_ms=clock_tolerance_ms,
                report_key=report_key,
                report=report,
                cache_records=cache_records,
                invalidations=invalidation_records,
            )
            return json.loads(report_json)
        except ConcurrentStateChange:
            current = store.current_snapshot()
            if current is None:
                raise CacheError(
                    CACHE_INDEX_EMPTY,
                    "index disappeared during judge",
                    fields={},
                )
            if int(current["index_version"]) == int(report["index_version"]):
                continue
    raise CacheError(
        CACHE_INTERNAL_ERROR,
        "judge could not commit against a stable index version",
        fields={"expected_index_version": report["index_version"]},
    )


def _resume_prepared(
    store: StateStore,
    operation: dict[str, Any],
    *,
    clock_tolerance_ms: int,
) -> dict[str, Any]:
    payload = operation["payload"]
    current = store.current_snapshot()
    current_version = 0 if current is None else int(current["index_version"])
    if current_version != int(operation["base_index_version"]):
        if current is not None and current["input_hash"] == operation["input_hash"]:
            store.mark_operation_completed(
                operation["op_id"],
                int(payload.get("now_ms", 0)),
            )
            result = {
                "op_id": operation["op_id"],
                "kind": operation["kind"],
                "status": "completed_by_concurrent_writer",
                "index_version": current_version,
            }
        else:
            store.mark_operation_superseded(
                operation["op_id"],
                int(payload.get("now_ms", 0)),
            )
            result = {
                "op_id": operation["op_id"],
                "kind": operation["kind"],
                "status": "superseded",
                "index_version": current_version,
            }
        return result
    if payload["kind"] == "merge":
        report = execute_merge(
            store,
            v1_paths=list(payload["v1_paths"]),
            v2_paths=list(payload["v2_paths"]),
            now_ms=int(payload["now_ms"]),
            clock_tolerance_ms=int(payload.get("clock_tolerance_ms", clock_tolerance_ms)),
            fault_point=None,
        )
        return {
            "op_id": operation["op_id"],
            "kind": operation["kind"],
            "status": "replayed",
            "index_version": report["index_version"],
        }
    if payload["kind"] == "replace":
        from .loader import parse_package

        descriptor, incoming = parse_package(
            payload["package_path"],
            now_ms=int(payload["now_ms"]),
        )
        pipelines = load_pipelines(payload.get("pipelines", []))
        report = execute_replace(
            store,
            package_path=payload["package_path"],
            package_descriptor=descriptor,
            incoming=incoming,
            pipelines=pipelines,
            now_ms=int(payload["now_ms"]),
            clock_tolerance_ms=int(payload.get("clock_tolerance_ms", clock_tolerance_ms)),
            fault_point=None,
        )
        return {
            "op_id": operation["op_id"],
            "kind": operation["kind"],
            "status": "replayed",
            "index_version": report["index_version"],
        }
    raise CacheError(
        CACHE_STATE_CORRUPT,
        "pending operation has an unknown payload kind",
        fields={"op_id": operation["op_id"], "payload_kind": payload.get("kind")},
    )


def execute_resume(
    store: StateStore,
    *,
    clock_tolerance_ms: int,
) -> dict[str, Any]:
    pending_before = store.list_pending_operations()
    removed_orphan_rows = store.cleanup_orphan_stages()
    recovered: list[dict[str, Any]] = []
    for operation in pending_before:
        if operation["status"] == "staged":
            published = store.publish_staged(operation["op_id"])
            recovered.append(
                {
                    "op_id": operation["op_id"],
                    "kind": operation["kind"],
                    "status": (
                        "replayed"
                        if published["status"] in {"published", "idempotent", "completed"}
                        else published["status"]
                    ),
                    "index_version": published.get("index_version"),
                }
            )
        elif operation["status"] == "prepared":
            recovered.append(
                _resume_prepared(
                    store,
                    operation,
                    clock_tolerance_ms=clock_tolerance_ms,
                )
            )
    pending_after = store.list_pending_operations()
    return {
        "ok": True,
        "operation": "resume",
        "pending_before": len(pending_before),
        "pending_after": len(pending_after),
        "orphan_stage_rows_removed": removed_orphan_rows,
        "recovered": sorted(recovered, key=lambda item: item["op_id"]),
        "index_version": store.current_version(),
    }


def execute_verify(
    store: StateStore,
    *,
    v1_paths: list[str] | None = None,
    v2_paths: list[str] | None = None,
    pipeline_paths: list[str] | None = None,
) -> dict[str, Any]:
    state_report = store.verify_state()
    artifact_issues: list[dict[str, Any]] = []
    source_report: dict[str, Any] | None = None
    pipeline_report: dict[str, Any] | None = None
    snapshot = store.current_snapshot()
    if snapshot is not None:
        for entry in snapshot["entries"]:
            root = Path(entry["source_root"])
            path = Path(entry["content_path"])
            if not path.is_absolute():
                path = root / path
            try:
                content = path.read_bytes()
            except OSError as exc:
                artifact_issues.append(
                    {
                        "code": CACHE_ARTIFACT_MISSING,
                        "task_key": entry["task_key"],
                        "artifact": entry["artifact"],
                        "path": str(path),
                        "reason": str(exc),
                    }
                )
                continue
            actual = "sha256:" + sha256_bytes(content)
            if actual not in entry["digests"]:
                artifact_issues.append(
                    {
                        "code": CACHE_DIGEST_MISMATCH,
                        "task_key": entry["task_key"],
                        "artifact": entry["artifact"],
                        "expected_digests": entry["digests"],
                        "actual_sha256": actual,
                    }
                )
            if len(content) != int(entry["size_bytes"]):
                artifact_issues.append(
                    {
                        "code": CACHE_DIGEST_MISMATCH,
                        "task_key": entry["task_key"],
                        "artifact": entry["artifact"],
                        "reason": "size_mismatch",
                        "expected_size_bytes": int(entry["size_bytes"]),
                        "actual_size_bytes": len(content),
                    }
                )
    if v1_paths or v2_paths:
        dataset = load_dataset(v1_paths=v1_paths or [], v2_paths=v2_paths or [])
        source_report = {
            "source_count": len(dataset.sources),
            "artifact_count": len(dataset.candidates),
            "tombstone_count": len(dataset.tombstones),
            "input_hash": dataset_input_hash(dataset),
        }
    if pipeline_paths:
        pipelines = load_pipelines(pipeline_paths)
        pipeline_report = {
            "pipeline_count": len(pipelines),
            "pipeline_ids": [pipeline.pipeline_id for pipeline in pipelines],
        }
    ok = bool(state_report["ok"]) and not artifact_issues
    report = {
        "ok": ok,
        "operation": "verify",
        "state": state_report,
        "artifact_issues": artifact_issues,
        "artifact_checked_count": 0 if snapshot is None else len(snapshot["entries"]),
        "sources_checked": source_report,
        "pipelines_checked": pipeline_report,
    }
    if not ok:
        error_code = (
            CACHE_STATE_CORRUPT
            if not state_report["ok"]
            else artifact_issues[0]["code"]
        )
        report["error_code"] = error_code
        report["error"] = {
            "code": error_code,
            "message": "verification found inconsistent state or artifact bytes",
        }
    return report
