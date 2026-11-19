"""Pipeline parsing, dependency validation, and deterministic topology."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import CACHE_PIPELINE_INVALID, CacheError
from .util import (
    artifact_name,
    canonical_task_key,
    load_json_file,
    object_digest,
    require_exact_keys,
    require_int,
    require_list,
    require_object,
    require_str,
)


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    ttl_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ttl_seconds": self.ttl_seconds}


@dataclass(frozen=True)
class TaskSpec:
    task_key: str
    depends_on: tuple[str, ...]
    artifacts: tuple[ArtifactSpec, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_key,
            "depends_on": list(self.depends_on),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


@dataclass
class Pipeline:
    pipeline_id: str
    pipeline_version: int
    tasks: dict[str, TaskSpec]
    topological_order: tuple[str, ...]
    source_path: str
    fingerprint: str = ""
    reverse_dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "pipeline_version": self.pipeline_version,
            "tasks": [self.tasks[key].to_dict() for key in self.topological_order],
            "fingerprint": self.fingerprint,
        }

    def downstream_closure(self, changed_tasks: Iterable[str]) -> list[str]:
        affected = {task for task in changed_tasks if task in self.tasks}
        queue = list(sorted(affected))
        while queue:
            current = heapq.heappop(queue)
            for dependent in self.reverse_dependencies.get(current, ()):
                if dependent not in affected:
                    affected.add(dependent)
                    heapq.heappush(queue, dependent)
        return sorted(affected)


def _pipeline_error(
    message: str,
    *,
    path: Path,
    detail: dict[str, Any] | None = None,
) -> CacheError:
    fields = {"path": str(path)}
    if detail:
        fields.update(detail)
    return CacheError(CACHE_PIPELINE_INVALID, message, fields=fields)


def parse_pipeline(path: str | Path) -> Pipeline:
    pipeline_path = Path(path)
    value, _ = load_json_file(pipeline_path, code=CACHE_PIPELINE_INVALID)
    require_exact_keys(
        value,
        {"format", "pipeline_id", "pipeline_version", "tasks"},
        optional={"default_ttl_seconds"},
        field="pipeline",
        code=CACHE_PIPELINE_INVALID,
    )
    if value["format"] != "cacheman-pipeline-v1":
        raise _pipeline_error(
            "pipeline has an unexpected format marker",
            path=pipeline_path,
            detail={"format": value["format"]},
        )
    pipeline_id = require_str(
        value["pipeline_id"],
        "pipeline_id",
        code=CACHE_PIPELINE_INVALID,
    )
    pipeline_version = require_int(
        value["pipeline_version"],
        "pipeline_version",
        minimum=1,
        code=CACHE_PIPELINE_INVALID,
    )
    default_ttl: int | None = None
    if "default_ttl_seconds" in value and value["default_ttl_seconds"] is not None:
        default_ttl = require_int(
            value["default_ttl_seconds"],
            "default_ttl_seconds",
            minimum=0,
            code=CACHE_PIPELINE_INVALID,
        )

    raw_tasks = require_list(value["tasks"], "tasks", code=CACHE_PIPELINE_INVALID)
    tasks: dict[str, TaskSpec] = {}
    for index, raw_task in enumerate(raw_tasks):
        field = f"tasks[{index}]"
        task_object = require_object(raw_task, field, code=CACHE_PIPELINE_INVALID)
        require_exact_keys(
            task_object,
            {"task", "depends_on", "artifacts"},
            optional={"ttl_seconds"},
            field=field,
            code=CACHE_PIPELINE_INVALID,
        )
        task_key = canonical_task_key(
            task_object["task"],
            field=f"{field}.task",
            code=CACHE_PIPELINE_INVALID,
        )
        if task_key in tasks:
            raise _pipeline_error(
                "pipeline contains a duplicate task key",
                path=pipeline_path,
                detail={"task_key": task_key},
            )
        dependency_values = require_list(
            task_object["depends_on"],
            f"{field}.depends_on",
            code=CACHE_PIPELINE_INVALID,
        )
        dependencies: list[str] = []
        for dependency_index, raw_dependency in enumerate(dependency_values):
            dependency = canonical_task_key(
                raw_dependency,
                field=f"{field}.depends_on[{dependency_index}]",
                code=CACHE_PIPELINE_INVALID,
            )
            if dependency in dependencies:
                raise _pipeline_error(
                    "pipeline task repeats a dependency",
                    path=pipeline_path,
                    detail={"task_key": task_key, "dependency": dependency},
                )
            dependencies.append(dependency)

        task_ttl = default_ttl
        if "ttl_seconds" in task_object and task_object["ttl_seconds"] is not None:
            task_ttl = require_int(
                task_object["ttl_seconds"],
                f"{field}.ttl_seconds",
                minimum=0,
                code=CACHE_PIPELINE_INVALID,
            )
        raw_artifacts = require_list(
            task_object["artifacts"],
            f"{field}.artifacts",
            code=CACHE_PIPELINE_INVALID,
        )
        if not raw_artifacts:
            raise _pipeline_error(
                "pipeline task must reference at least one artifact",
                path=pipeline_path,
                detail={"task_key": task_key},
            )
        artifacts: list[ArtifactSpec] = []
        artifact_names: set[str] = set()
        for artifact_index, raw_artifact in enumerate(raw_artifacts):
            artifact_field = f"{field}.artifacts[{artifact_index}]"
            if isinstance(raw_artifact, str):
                name = artifact_name(
                    raw_artifact,
                    field=artifact_field,
                    code=CACHE_PIPELINE_INVALID,
                )
                ttl = task_ttl
            else:
                artifact_object = require_object(
                    raw_artifact,
                    artifact_field,
                    code=CACHE_PIPELINE_INVALID,
                )
                require_exact_keys(
                    artifact_object,
                    {"name"},
                    optional={"ttl_seconds"},
                    field=artifact_field,
                    code=CACHE_PIPELINE_INVALID,
                )
                name = artifact_name(
                    artifact_object["name"],
                    field=f"{artifact_field}.name",
                    code=CACHE_PIPELINE_INVALID,
                )
                ttl = task_ttl
                if (
                    "ttl_seconds" in artifact_object
                    and artifact_object["ttl_seconds"] is not None
                ):
                    ttl = require_int(
                        artifact_object["ttl_seconds"],
                        f"{artifact_field}.ttl_seconds",
                        minimum=0,
                        code=CACHE_PIPELINE_INVALID,
                    )
            if name in artifact_names:
                raise _pipeline_error(
                    "pipeline task repeats an artifact name",
                    path=pipeline_path,
                    detail={"task_key": task_key, "artifact": name},
                )
            artifact_names.add(name)
            artifacts.append(ArtifactSpec(name=name, ttl_seconds=ttl))
        tasks[task_key] = TaskSpec(
            task_key=task_key,
            depends_on=tuple(dependencies),
            artifacts=tuple(artifacts),
        )

    for task_key, task in tasks.items():
        if task_key in task.depends_on:
            raise _pipeline_error(
                "pipeline task cannot depend on itself",
                path=pipeline_path,
                detail={"task_key": task_key},
            )
        for dependency in task.depends_on:
            if dependency not in tasks:
                raise _pipeline_error(
                    "pipeline task references an unknown dependency",
                    path=pipeline_path,
                    detail={"task_key": task_key, "dependency": dependency},
                )

    indegree = {task_key: len(task.depends_on) for task_key, task in tasks.items()}
    dependents: dict[str, list[str]] = {task_key: [] for task_key in tasks}
    for task_key, task in tasks.items():
        for dependency in task.depends_on:
            dependents[dependency].append(task_key)
    ready = [task_key for task_key, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        task_key = heapq.heappop(ready)
        order.append(task_key)
        for dependent in sorted(dependents[task_key]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(order) != len(tasks):
        cyclic = sorted(task_key for task_key, degree in indegree.items() if degree > 0)
        raise _pipeline_error(
            "pipeline dependency graph contains a cycle",
            path=pipeline_path,
            detail={"cyclic_tasks": cyclic},
        )

    reverse = {
        task_key: tuple(sorted(dependents[task_key]))
        for task_key in tasks
    }
    canonical = {
        "format": "cacheman-pipeline-v1",
        "pipeline_id": pipeline_id,
        "pipeline_version": pipeline_version,
        "tasks": [tasks[key].to_dict() for key in order],
    }
    return Pipeline(
        pipeline_id=pipeline_id,
        pipeline_version=pipeline_version,
        tasks=tasks,
        topological_order=tuple(order),
        source_path=str(pipeline_path),
        fingerprint=object_digest(canonical),
        reverse_dependencies=reverse,
    )


def load_pipelines(paths: Iterable[str | Path]) -> list[Pipeline]:
    pipelines = [parse_pipeline(path) for path in paths]
    seen: set[str] = set()
    for pipeline in pipelines:
        if pipeline.pipeline_id in seen:
            raise CacheError(
                CACHE_PIPELINE_INVALID,
                "duplicate pipeline id in the input set",
                fields={"pipeline_id": pipeline.pipeline_id},
            )
        seen.add(pipeline.pipeline_id)
    return sorted(pipelines, key=lambda item: item.pipeline_id)
