"""Cross-generation manifest normalization, arbitration, and provenance."""

from __future__ import annotations

from typing import Any

from .errors import GitOpsError
from .manifests import V1Manifest, V2Shard
from .project import Project
from .util import canonical_json, sha256_json, validate_dependencies


SOURCE_PRIORITY = {"v1": 10, "v2": 20}
DECISION_ORDER = "logical_clock_ms>manifest_generation>source_format_priority>source_id"


def _base_candidate(
    *,
    environment: str,
    app: str,
    image_version: str,
    replicas: int,
    resources: dict[str, Any],
    depends_on: tuple[str, ...],
    logical_clock_ms: int,
    manifest_revision: int,
    source_format: str,
    source_id: str,
    source_manifest_generation: int,
    raw_time: dict[str, Any],
    absolute_effective_time_ms: int,
    time_semantics: dict[str, Any],
    tombstone: bool,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "absolute_effective_time_ms": absolute_effective_time_ms,
        "app": app,
        "depends_on": list(depends_on),
        "environment": environment,
        "image_version": image_version,
        "logical_clock_ms": logical_clock_ms,
        "manifest_revision": manifest_revision,
        "params": params or {},
        "raw_time": raw_time,
        "replicas": replicas,
        "resources": resources,
        "source_format": source_format,
        "source_id": source_id,
        "source_manifest_generation": source_manifest_generation,
        "time_semantics": time_semantics,
        "tombstone": tombstone,
    }


def _v1_candidate(
    project: Project,
    source: V1Manifest,
    environment: str,
    app_name: str,
    injected_clock_ms: int,
) -> dict[str, Any]:
    app = source.apps[app_name]
    absolute = injected_clock_ms + app.effective_seconds * 1000
    semantics = {
        "conversion": "logical_clock_ms + relative_effective_seconds * 1000",
        "direction": "add_relative_to_clock",
        "injected_logical_clock_ms": injected_clock_ms,
        "rounding": "floor",
        "scale_ms_per_unit": 1000,
        "source_unit": "second",
        "target_unit": "millisecond",
    }
    return _base_candidate(
        environment=environment,
        app=app.name,
        image_version=app.image_version,
        replicas=app.replicas,
        resources={},
        depends_on=app.depends_on,
        logical_clock_ms=source.logical_clock_ms,
        manifest_revision=source.generation,
        source_format="v1",
        source_id=source.source_id,
        source_manifest_generation=source.generation,
        raw_time={"kind": "relative_seconds", "value": app.effective_seconds},
        absolute_effective_time_ms=absolute,
        time_semantics=semantics,
        tombstone=False,
        params=app.params,
    )


def _v2_candidate(environment: str, source: V2Shard, app_name: str) -> dict[str, Any]:
    app = source.apps[app_name]
    return _base_candidate(
        environment=environment,
        app=app.name,
        image_version=app.image_version,
        replicas=app.replicas,
        resources=app.resources,
        depends_on=app.depends_on,
        logical_clock_ms=source.logical_clock_ms,
        manifest_revision=app.generation,
        source_format="v2",
        source_id=source.source_id,
        source_manifest_generation=source.generation,
        raw_time={"kind": "absolute_millis", "value": app.effective_at_ms},
        absolute_effective_time_ms=app.effective_at_ms,
        time_semantics={
            "conversion": "preserve",
            "direction": "none",
            "rounding": "none",
            "scale_ms_per_unit": 1,
            "source_unit": "millisecond",
            "target_unit": "millisecond",
        },
        tombstone=app.tombstone,
    )


def _candidate_rank(candidate: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        candidate["logical_clock_ms"],
        candidate["manifest_revision"],
        SOURCE_PRIORITY[candidate["source_format"]],
        candidate["source_id"],
    )


def _winner_reason(candidates: list[dict[str, Any]], winner: dict[str, Any]) -> str:
    if len(candidates) == 1:
        return "only_source"
    max_clock = max(item["logical_clock_ms"] for item in candidates)
    same_clock = [item for item in candidates if item["logical_clock_ms"] == max_clock]
    if len(same_clock) == 1:
        return "higher_logical_clock"
    max_generation = max(item["manifest_revision"] for item in same_clock)
    same_generation = [
        item for item in same_clock if item["manifest_revision"] == max_generation
    ]
    if len(same_generation) == 1:
        return "higher_manifest_generation"
    max_priority = max(
        SOURCE_PRIORITY[item["source_format"]] for item in same_generation
    )
    same_priority = [
        item
        for item in same_generation
        if SOURCE_PRIORITY[item["source_format"]] == max_priority
    ]
    if len(same_priority) > 1:
        return "source_id_tie_break"
    if winner["source_format"] == "v2":
        return "source_format_priority_v2"
    return "source_format_priority_v1"


def _merge_environment(
    project: Project,
    environment: str,
    v1: V1Manifest,
    v2: V2Shard,
    injected_clock_ms: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = {}
    if v1.environment_scope is None or environment in v1.environment_scope:
        for app_name in sorted(v1.apps):
            candidate = _v1_candidate(
                project,
                v1,
                environment,
                app_name,
                injected_clock_ms,
            )
            candidates.setdefault(app_name, []).append(candidate)
    for app_name in sorted(v2.apps):
        candidate = _v2_candidate(environment, v2, app_name)
        candidates.setdefault(app_name, []).append(candidate)

    entries: list[dict[str, Any]] = []
    migration_decisions: list[dict[str, Any]] = []
    for app_name in sorted(candidates):
        app_candidates = sorted(
            candidates[app_name],
            key=lambda item: (SOURCE_PRIORITY[item["source_format"]], item["source_id"]),
        )
        winner = max(app_candidates, key=_candidate_rank)
        reason = _winner_reason(app_candidates, winner)
        candidate_records = []
        for item in app_candidates:
            record = dict(item)
            record["rank"] = list(_candidate_rank(item))
            record["selected"] = item is winner
            candidate_records.append(record)
            if item["source_format"] == "v1":
                decision_body = {
                    "absolute_effective_time_ms": item["absolute_effective_time_ms"],
                    "app": app_name,
                    "environment": environment,
                    "injected_logical_clock_ms": injected_clock_ms,
                    "raw_time": item["raw_time"],
                    "rounding": "floor",
                    "scale_ms_per_unit": 1000,
                    "source_id": item["source_id"],
                    "target_unit": "millisecond",
                }
                migration_decisions.append(
                    {
                        **decision_body,
                        "decision_id": sha256_json(decision_body),
                        "source_format": "v1",
                    }
                )
        entry = dict(winner)
        entry["active"] = not winner["tombstone"]
        entry["decision"] = {
            "candidates": candidate_records,
            "order": DECISION_ORDER,
            "reason": reason,
            "selected_rank": list(_candidate_rank(winner)),
            "source_format_priority": SOURCE_PRIORITY,
        }
        entry["provenance"] = {
            "logical_clock_ms": winner["logical_clock_ms"],
            "manifest_revision": winner["manifest_revision"],
            "raw_time": winner["raw_time"],
            "source_format": winner["source_format"],
            "source_id": winner["source_id"],
        }
        entries.append(entry)

    active_entries = [entry for entry in entries if entry["active"]]
    active_names = [entry["app"] for entry in active_entries]
    dependencies = {
        entry["app"]: list(entry["depends_on"]) for entry in active_entries
    }
    validate_dependencies(
        active_names,
        dependencies,
        context=f"merged:{environment}",
    )
    payload = {
        "dependencies": dependencies,
        "entries": entries,
        "environment": environment,
        "format": "unified-desired-state-v1",
        "injected_logical_clock_ms": injected_clock_ms,
        "migration_decisions": sorted(
            migration_decisions,
            key=lambda item: (item["app"], item["source_format"], item["decision_id"]),
        ),
        "summary": {
            "active_apps": len(active_entries),
            "apps": len(entries),
            "by_source": {
                "v1": sum(1 for entry in entries if entry["source_format"] == "v1"),
                "v2": sum(1 for entry in entries if entry["source_format"] == "v2"),
            },
            "migration_decisions": len(migration_decisions),
            "tombstones": sum(1 for entry in entries if entry["tombstone"]),
        },
    }
    payload["merge_key"] = sha256_json(payload)
    return payload, migration_decisions


def merge_sources(
    project: Project,
    v1: V1Manifest,
    v2_shards: dict[str, V2Shard],
    injected_clock_ms: int,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for environment in project.environments:
        payload, _ = _merge_environment(
            project,
            environment,
            v1,
            v2_shards[environment],
            injected_clock_ms,
        )
        result[environment] = payload
    return result


def assert_merge_stable(first: dict[str, Any], second: dict[str, Any]) -> None:
    """Defensive determinism check used by tests and the load path."""

    if canonical_json(first) != canonical_json(second):
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "manifest merge is not deterministic",
            {"first": first.get("merge_key"), "second": second.get("merge_key")},
        )
