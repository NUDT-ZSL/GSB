from __future__ import annotations

from typing import Any

from .config import ConfigError
from .io_utils import read_json


KNOWN_SUMMARIES = {
    "boundary_samples",
    "clear_pairs",
    "reward_delta_distribution",
    "bucket_frequency",
    "annotator_groups",
    "failure_list",
}


def load_dependencies(path: str) -> dict[str, Any]:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ConfigError("E_DEPENDENCY_INVALID", "dependency declaration must be an object")
    summary_dependencies = raw.get("summary_dependencies")
    stage_dependencies = raw.get("stage_dependencies")
    edges = raw.get("edges", [])
    if not isinstance(summary_dependencies, dict):
        raise ConfigError(
            "E_DEPENDENCY_INVALID", "summary_dependencies must be an object"
        )
    if not isinstance(stage_dependencies, dict):
        raise ConfigError(
            "E_DEPENDENCY_INVALID", "stage_dependencies must be an object"
        )
    if not isinstance(edges, list):
        raise ConfigError("E_DEPENDENCY_INVALID", "edges must be an array")
    missing = KNOWN_SUMMARIES.difference(summary_dependencies)
    if missing:
        raise ConfigError(
            "E_DEPENDENCY_INVALID",
            "missing summary dependencies: " + ",".join(sorted(missing)),
        )
    for name, dependency in summary_dependencies.items():
        if not isinstance(dependency, dict):
            raise ConfigError(
                "E_DEPENDENCY_INVALID", f"summary dependency {name} must be an object"
            )
        batches = dependency.get("batches", [])
        buckets = dependency.get("buckets", [])
        if batches != "*" and not isinstance(batches, list):
            raise ConfigError(
                "E_DEPENDENCY_INVALID", f"{name}.batches must be '*' or an array"
            )
        if buckets != "*" and not isinstance(buckets, list):
            raise ConfigError(
                "E_DEPENDENCY_INVALID", f"{name}.buckets must be '*' or an array"
            )
    for stage, dependencies in stage_dependencies.items():
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) for item in dependencies
        ):
            raise ConfigError(
                "E_DEPENDENCY_INVALID", f"stage dependency {stage} must be string array"
            )
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict) or "from" not in edge or "to" not in edge:
            raise ConfigError(
                "E_DEPENDENCY_INVALID", f"edges[{index}] must contain from/to"
            )
    _assert_stage_graph_acyclic(stage_dependencies)
    normalized = dict(raw)
    normalized["summary_dependencies"] = summary_dependencies
    normalized["stage_dependencies"] = stage_dependencies
    normalized["edges"] = edges
    return normalized


def _assert_stage_graph_acyclic(stage_dependencies: dict[str, list[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ConfigError("E_DEPENDENCY_INVALID", f"cycle at stage {node}")
        visiting.add(node)
        for dependency in stage_dependencies.get(node, []):
            if dependency in stage_dependencies:
                visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for stage in stage_dependencies:
        visit(stage)


def stage_ancestors(dependencies: dict[str, Any], stage: str) -> set[str]:
    result: set[str] = set()

    def visit(current: str) -> None:
        for dependency in dependencies["stage_dependencies"].get(current, []):
            if dependency not in result:
                result.add(dependency)
                visit(dependency)

    visit(stage)
    return result


def build_summary_references(
    dependencies: dict[str, Any],
    batch_ids: list[str],
    active_buckets: list[int],
) -> dict[str, dict[str, Any]]:
    references: dict[str, dict[str, Any]] = {}
    for name in sorted(dependencies["summary_dependencies"]):
        declaration = dependencies["summary_dependencies"][name]
        declared_batches = declaration.get("batches", [])
        declared_buckets = declaration.get("buckets", [])
        if declared_batches == "*":
            resolved_batches = list(batch_ids)
        else:
            allowed = set(declared_batches)
            resolved_batches = [item for item in batch_ids if item in allowed]
        if declared_buckets == "*":
            resolved_buckets = list(active_buckets)
        else:
            allowed_buckets = {int(item) for item in declared_buckets}
            resolved_buckets = [
                item for item in active_buckets if item in allowed_buckets
            ]
        references[name] = {
            "batches": resolved_batches,
            "buckets": resolved_buckets,
            "stages": list(declaration.get("stages", [])),
        }
    return references


def affected_summary_outputs(
    dependencies: dict[str, Any],
    changed_batches: set[str],
    changed_buckets: set[int],
    changed_stages: set[str],
) -> list[str]:
    affected: list[str] = []
    for name, declaration in sorted(dependencies["summary_dependencies"].items()):
        declared_batches = declaration.get("batches", [])
        declared_buckets = declaration.get("buckets", [])
        declared_stages = set(declaration.get("stages", []))
        batch_hit = declared_batches == "*" or bool(
            changed_batches.intersection(declared_batches)
        )
        bucket_hit = declared_buckets == "*" or bool(
            changed_buckets.intersection({int(item) for item in declared_buckets})
        )
        stage_hit = bool(declared_stages.intersection(changed_stages))
        if batch_hit or bucket_hit or stage_hit:
            affected.append(name)
    return affected
