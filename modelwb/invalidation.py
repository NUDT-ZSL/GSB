from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import sha256_json
from .domain import Manifest
from .loaders import Catalog


def model_node(version: str) -> str:
    return f"model:{version}"


def shard_node(version: str, layer: str, shard_id: str) -> str:
    return f"shard:{version}:{layer}:{shard_id}"


def config_node(config_id: str) -> str:
    return f"config:{config_id}"


def feature_node(feature_id: str) -> str:
    return f"feature:{feature_id}"


def upstream_node(upstream_id: str) -> str:
    return f"upstream:{upstream_id}"


@dataclass
class InvalidationResult:
    event_id: str
    roots: list[str]
    impacted_nodes: list[str]
    affected_replica_ids: list[str]
    invalidated_cache_ids: list[str]
    reusable_cache_ids: list[str]
    details: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "roots": self.roots,
            "impacted_nodes": self.impacted_nodes,
            "affected_replica_ids": self.affected_replica_ids,
            "invalidated_cache_ids": self.invalidated_cache_ids,
            "reusable_cache_ids": self.reusable_cache_ids,
            "details": self.details,
        }


def dependency_graph(
    catalog: Catalog, manifests: Iterable[Manifest] | None = None
) -> dict[str, set[str]]:
    versions = list(manifests if manifests is not None else catalog.all_versions)
    graph: dict[str, set[str]] = {}

    def edge(source: str, target: str) -> None:
        graph.setdefault(source, set()).add(target)

    version_ids = {manifest.version for manifest in versions} | catalog.version_ids()
    for manifest in versions:
        model = model_node(manifest.version)
        graph.setdefault(model, set())
        if manifest.tombstone:
            continue
        for layer in manifest.layers:
            for shard in layer.shards:
                node = shard_node(manifest.version, layer.name, shard.shard_id)
                edge(model, node)
        edge(model, config_node(manifest.preprocess))
        edge(model, config_node(manifest.postprocess))
        for feature in manifest.feature_tables:
            edge(model, feature_node(feature))
        for upstream in manifest.upstream_models:
            if upstream in version_ids:
                edge(model, model_node(upstream))
            else:
                edge(model, upstream_node(upstream))
    for config_id, definition in catalog.registries.configs.items():
        node = config_node(config_id)
        graph.setdefault(node, set())
        for reference in _references(definition):
            edge(node, config_node(reference))
        for feature in definition.get("feature_tables", []):
            edge(node, feature_node(feature))
        for upstream in definition.get("upstream_models", []):
            if upstream in version_ids:
                edge(node, model_node(upstream))
            else:
                edge(node, upstream_node(upstream))
    for feature_id, definition in catalog.registries.feature_tables.items():
        node = feature_node(feature_id)
        graph.setdefault(node, set())
        for upstream in definition.get("upstream_models", []):
            if upstream in version_ids:
                edge(node, model_node(upstream))
            else:
                edge(node, upstream_node(upstream))
        for reference in definition.get("references", []):
            edge(node, feature_node(reference))
    for upstream_id, definition in catalog.registries.upstream_models.items():
        node = upstream_node(upstream_id)
        graph.setdefault(node, set())
        for feature in definition.get("feature_tables", []):
            edge(node, feature_node(feature))
    return graph


def _references(definition: dict[str, Any]) -> list[str]:
    refs = definition.get("references", definition.get("configs", []))
    if isinstance(refs, str):
        return [refs]
    if isinstance(refs, list):
        return [item for item in refs if isinstance(item, str)]
    return []


def impacted_closure(graph: dict[str, set[str]], roots: Iterable[str]) -> set[str]:
    reverse: dict[str, set[str]] = {}
    for source, targets in graph.items():
        reverse.setdefault(source, set())
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    affected = set(roots)
    queue = list(affected)
    while queue:
        current = queue.pop(0)
        for dependent in sorted(reverse.get(current, ())):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
    return affected


def calculate_invalidation(
    catalog: Catalog,
    *,
    base: Manifest,
    target: Manifest,
    changed_shards: list[dict[str, Any]],
    caches: list[dict[str, Any]],
    replicas: list[dict[str, Any]],
    operation_id: str,
) -> InvalidationResult:
    roots: set[str] = {model_node(base.version), model_node(target.version)}
    for change in changed_shards:
        roots.add(shard_node(base.version, change["layer"], change["shard_id"]))
        roots.add(shard_node(target.version, change["layer"], change["shard_id"]))
    for config_id in {base.preprocess, base.postprocess, target.preprocess, target.postprocess}:
        roots.add(config_node(config_id))
    for feature in set(base.feature_tables) | set(target.feature_tables):
        roots.add(feature_node(feature))
    for upstream in set(base.upstream_models) | set(target.upstream_models):
        if upstream in catalog.version_ids():
            roots.add(model_node(upstream))
        else:
            roots.add(upstream_node(upstream))
    graph = dependency_graph(catalog)
    impacted = impacted_closure(graph, roots)
    invalidated: list[str] = []
    reusable: list[str] = []
    details: list[dict[str, Any]] = []
    for cache in caches:
        owner = model_node(cache["version"])
        hit = owner in impacted
        detail = {
            "cache_id": cache["cache_id"],
            "replica_id": cache["replica_id"],
            "kind": cache["kind"],
            "version": cache["version"],
            "shard_chain_digest": cache["shard_chain_digest"],
            "decision": "invalidate" if hit else "reuse",
            "reason": (
                f"model node {owner} is in the invalidation closure"
                if hit
                else f"model node {owner} is outside the invalidation closure"
            ),
        }
        details.append(detail)
        if hit:
            invalidated.append(cache["cache_id"])
        else:
            reusable.append(cache["cache_id"])
    affected_replicas = {
        cache["replica_id"]
        for cache in caches
        if cache["cache_id"] in invalidated
    }
    for replica in replicas:
        if model_node(replica["baseline_version"]) in impacted:
            affected_replicas.add(replica["replica_id"])
    event_payload = {
        "operation_id": operation_id,
        "roots": sorted(roots),
        "invalidated": sorted(invalidated),
        "reusable": sorted(reusable),
        "impacted": sorted(impacted),
    }
    event_id = "inv-" + sha256_json(event_payload)[:24]
    return InvalidationResult(
        event_id=event_id,
        roots=sorted(roots),
        impacted_nodes=sorted(impacted),
        affected_replica_ids=sorted(affected_replicas),
        invalidated_cache_ids=sorted(invalidated),
        reusable_cache_ids=sorted(reusable),
        details=details,
    )
