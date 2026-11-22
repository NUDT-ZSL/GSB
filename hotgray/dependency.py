from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import Manifest, ResourceSpec

CACHE_TYPES = ("load_cache", "warmup", "inference")


@dataclass(frozen=True)
class ChangedNode:
    node_id: str
    trigger: str
    version: int
    layer: str | None = None
    shard_index: int | None = None
    resource_id: str | None = None
    expected: Any = None
    actual: Any = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result = {
            "node_id": self.node_id,
            "trigger": self.trigger,
            "version": self.version,
        }
        for key in ("layer", "shard_index", "resource_id", "expected", "actual"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if self.detail:
            result["detail"] = dict(self.detail)
        return result


@dataclass(frozen=True)
class CacheDecision:
    replica_id: str
    cache_type: str
    version: int
    decision: str
    trigger: str
    path: tuple[str, ...]
    at_ms: int
    source: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "replica_id": self.replica_id,
            "cache_type": self.cache_type,
            "version": self.version,
            "decision": self.decision,
            "trigger": self.trigger,
            "path": list(self.path),
            "at_ms": self.at_ms,
        }
        if self.source is not None:
            payload["source"] = dict(self.source)
        return payload


@dataclass
class InvalidationReport:
    changed_nodes: tuple[ChangedNode, ...]
    decisions: tuple[CacheDecision, ...]

    @property
    def affected_replicas(self) -> list[str]:
        return sorted(
            {
                decision.replica_id
                for decision in self.decisions
                if decision.decision == "invalidated"
            }
        )

    @property
    def reusable(self) -> list[dict[str, Any]]:
        return [
            decision.as_dict()
            for decision in self.decisions
            if decision.decision == "reused"
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": [node.as_dict() for node in self.changed_nodes],
            "affected_replicas": self.affected_replicas,
            "decisions": [decision.as_dict() for decision in self.decisions],
            "reusable": self.reusable,
        }


def _resource_changed(baseline: ResourceSpec, target: ResourceSpec) -> bool:
    return (
        baseline.digest != target.digest
        or baseline.revision != target.revision
        or baseline.depends_on != target.depends_on
        or dict(baseline.params) != dict(target.params)
    )


def compute_target_changes(
    manifest: Manifest,
    *,
    baseline_version: int,
    target_version: int,
) -> tuple[ChangedNode, ...]:
    baseline = manifest.model(baseline_version)
    target = manifest.model(target_version)
    baseline_shards = {
        (layer.name, shard.index): shard
        for layer in baseline.layers
        for shard in layer.shards
    }
    changes: list[ChangedNode] = []
    for layer in target.layers:
        for shard in layer.shards:
            key = (layer.name, shard.index)
            old = baseline_shards.get(key)
            if old is None:
                changes.append(
                    ChangedNode(
                        node_id=f"shard:{target_version}:{layer.name}:{shard.index}",
                        trigger="shard_added",
                        version=target_version,
                        layer=layer.name,
                        shard_index=shard.index,
                        expected=shard.digest,
                        actual=None,
                    )
                )
                continue
            if (
                old.digest != shard.digest
                or old.byte_start != shard.byte_start
                or old.byte_end != shard.byte_end
            ):
                changes.append(
                    ChangedNode(
                        node_id=f"shard:{target_version}:{layer.name}:{shard.index}",
                        trigger="shard_patch",
                        version=target_version,
                        layer=layer.name,
                        shard_index=shard.index,
                        expected=shard.digest,
                        actual=old.digest,
                    )
                )

    baseline_resources = manifest.version_resource_snapshots(baseline_version)
    target_resources = manifest.version_resource_snapshots(target_version)
    for resource_id in sorted(target_resources):
        target_resource = manifest.resources[resource_id]
        old_resource = manifest.resources.get(resource_id) if resource_id in baseline_resources else None
        if old_resource is None or _resource_changed(old_resource, target_resource):
            changes.append(
                ChangedNode(
                    node_id=f"resource:{resource_id}",
                    trigger="config_change",
                    version=target_version,
                    resource_id=resource_id,
                    expected=target_resource.digest,
                    actual=old_resource.digest if old_resource is not None else None,
                    detail={"kind": target_resource.kind, "revision": target_resource.revision},
                )
            )
    return tuple(sorted(changes, key=lambda node: node.node_id))


def build_cache_dependency_graph(
    manifest: Manifest,
    *,
    version: int,
    replica_ids: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    model = manifest.model(version)
    graph: dict[str, set[str]] = {}

    def add(node_id: str, dependency: str) -> None:
        graph.setdefault(node_id, set()).add(dependency)

    model_node = f"model:{version}"
    graph.setdefault(model_node, set())
    for layer in model.layers:
        layer_node = f"layer:{version}:{layer.name}"
        graph.setdefault(layer_node, set())
        add(model_node, layer_node)
        for shard in layer.shards:
            shard_node = f"shard:{version}:{layer.name}:{shard.index}"
            graph.setdefault(shard_node, set())
            add(layer_node, shard_node)

    for resource_id in sorted(manifest.version_resource_snapshots(version)):
        resource_node = f"resource:{resource_id}"
        graph.setdefault(resource_node, set())
        for dependency in manifest.resources[resource_id].depends_on:
            add(resource_node, f"resource:{dependency}")

    for resource_id in sorted(model.references().values()):
        add(model_node, f"resource:{resource_id}")

    layers = tuple(f"layer:{version}:{layer.name}" for layer in model.layers)
    for replica_id in sorted(set(replica_ids)):
        load_cache = f"cache:{replica_id}:load_cache:{version}"
        warmup = f"cache:{replica_id}:warmup:{version}"
        inference = f"cache:{replica_id}:inference:{version}"
        graph.setdefault(load_cache, set())
        graph.setdefault(warmup, set())
        graph.setdefault(inference, set())
        for layer_node in layers:
            add(load_cache, layer_node)
        add(warmup, load_cache)
        add(warmup, model_node)
        add(inference, warmup)
    return {
        node: tuple(sorted(dependencies))
        for node, dependencies in sorted(graph.items())
    }


def _shortest_dependency_path(
    graph: Mapping[str, tuple[str, ...]],
    start: str,
    target: str,
) -> tuple[str, ...] | None:
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(start, (start,))])
    visited = {start}
    while queue:
        node, path = queue.popleft()
        if node == target:
            return path
        for dependency in graph.get(node, ()):  # already stable-sorted
            if dependency in visited:
                continue
            visited.add(dependency)
            queue.append((dependency, path + (dependency,)))
    return None


def analyze_invalidation(
    manifest: Manifest,
    *,
    baseline_version: int,
    target_version: int,
    target_replica_ids: Iterable[str],
    all_replica_ids: Iterable[str],
    at_ms: int,
) -> InvalidationReport:
    target_replica_ids = tuple(sorted(set(target_replica_ids)))
    all_replica_ids = tuple(sorted(set(all_replica_ids)))
    changes = compute_target_changes(
        manifest,
        baseline_version=baseline_version,
        target_version=target_version,
    )
    graph = build_cache_dependency_graph(
        manifest,
        version=target_version,
        replica_ids=target_replica_ids,
    )
    decisions: list[CacheDecision] = []
    for replica_id in target_replica_ids:
        for cache_type in CACHE_TYPES:
            cache_node = f"cache:{replica_id}:{cache_type}:{target_version}"
            found: list[tuple[int, str, tuple[str, ...], ChangedNode]] = []
            for changed in changes:
                dependency_path = _shortest_dependency_path(
                    graph,
                    cache_node,
                    changed.node_id,
                )
                if dependency_path is None:
                    continue
                propagation = tuple(reversed(dependency_path))
                found.append((len(propagation), changed.node_id, propagation, changed))
            if found:
                for _, _, propagation, changed in sorted(
                    found,
                    key=lambda item: (item[0], item[1]),
                ):
                    decisions.append(
                        CacheDecision(
                            replica_id=replica_id,
                            cache_type=cache_type,
                            version=target_version,
                            decision="invalidated",
                            trigger=changed.trigger,
                            path=propagation,
                            at_ms=at_ms,
                            source=changed.as_dict(),
                        )
                    )
            else:
                decisions.append(
                    CacheDecision(
                        replica_id=replica_id,
                        cache_type=cache_type,
                        version=target_version,
                        decision="reused",
                        trigger="target_unaffected",
                        path=(cache_node,),
                        at_ms=at_ms,
                    )
                )

    for replica_id in all_replica_ids:
        for cache_type in CACHE_TYPES:
            cache_node = f"cache:{replica_id}:{cache_type}:{baseline_version}"
            decisions.append(
                CacheDecision(
                    replica_id=replica_id,
                    cache_type=cache_type,
                    version=baseline_version,
                    decision="reused",
                    trigger="baseline_preserved",
                    path=(cache_node,),
                    at_ms=at_ms,
                )
            )

    decisions.sort(
        key=lambda item: (
            item.replica_id,
            item.version,
            item.cache_type,
            item.decision,
            item.trigger,
            item.path,
        )
    )
    return InvalidationReport(changed_nodes=changes, decisions=tuple(decisions))


def analyze_resource_change(
    manifest: Manifest,
    *,
    resource_id: str,
    baseline_version: int,
    target_version: int,
    at_ms: int,
    replica_ids: Iterable[str],
) -> InvalidationReport:
    """Analyze a resource mutation without mutating the manifest snapshot.

    This helper is used by tests and diagnostic tooling. The engine persists
    resource changes only after the same graph-based decision has been made.
    """
    target_replica_ids = tuple(sorted(set(replica_ids)))
    graph = build_cache_dependency_graph(
        manifest,
        version=target_version,
        replica_ids=target_replica_ids,
    )
    changed = ChangedNode(
        node_id=f"resource:{resource_id}",
        trigger="config_change",
        version=target_version,
        resource_id=resource_id,
        detail={"kind": manifest.resources[resource_id].kind},
    )
    decisions: list[CacheDecision] = []
    for replica_id in target_replica_ids:
        for cache_type in CACHE_TYPES:
            cache_node = f"cache:{replica_id}:{cache_type}:{target_version}"
            path = _shortest_dependency_path(graph, cache_node, changed.node_id)
            if path is None:
                decisions.append(
                    CacheDecision(
                        replica_id=replica_id,
                        cache_type=cache_type,
                        version=target_version,
                        decision="reused",
                        trigger="target_unaffected",
                        path=(cache_node,),
                        at_ms=at_ms,
                    )
                )
            else:
                decisions.append(
                    CacheDecision(
                        replica_id=replica_id,
                        cache_type=cache_type,
                        version=target_version,
                        decision="invalidated",
                        trigger="config_change",
                        path=tuple(reversed(path)),
                        at_ms=at_ms,
                        source=changed.as_dict(),
                    )
                )
    decisions.sort(
        key=lambda item: (
            item.replica_id,
            item.version,
            item.cache_type,
            item.decision,
            item.trigger,
            item.path,
        )
    )
    return InvalidationReport(changed_nodes=(changed,), decisions=tuple(decisions))
