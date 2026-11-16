"""Deterministic fingerprinting and resource-level incremental compiler."""

from __future__ import annotations

import hashlib
import json

from .graph import DependencyGraph
from .models import Artifact, DeclarationVersion


def canonical_fingerprint(
    key: str,
    properties: dict[str, object],
    references: tuple[str, ...] | list[str],
) -> str:
    """Stable SHA-256 fingerprint of a resource's compiled input.

    Only the resource key, desired properties and references participate in
    the fingerprint, so identical declarations always compile identically.
    """
    payload = {
        "key": key,
        "properties": dict(sorted(properties.items())),
        "references": sorted(references),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_graph(declarations: list[DeclarationVersion]) -> DependencyGraph:
    """Build and validate the global reference graph for current versions."""
    nodes: set[str] = set()
    edges: set[tuple[str, str]] = set()
    known_keys: set[str] = set()
    for declaration in declarations:
        for resource in declaration.resources:
            known_keys.add(resource.key)
    for declaration in declarations:
        for resource in declaration.resources:
            nodes.add(resource.key)
            for reference in resource.references:
                edges.add((reference, resource.key))
    return DependencyGraph.build(nodes, edges)


def compile_view(
    current_declarations: list[DeclarationVersion],
    previous_artifacts: dict[str, Artifact] | None,
) -> tuple[dict[str, Artifact], DependencyGraph, set[str], set[str], int]:
    """Compile one complete workspace view from the current declaration set.

    Returns ``(artifacts, graph, changed_keys, invalidated_keys, graph_version_delta)``.

    Rules
    -----
    * A resource whose fingerprint is unchanged keeps its artifact version.
    * A changed/new resource gets a new immutable artifact version.
    * Transitive downstream resources are marked invalid (artifact version
      preserved) with the shortest deterministic propagation path.
    * Removed resources disappear from the view entirely.
    """
    previous_artifacts = previous_artifacts or {}
    graph = build_graph(current_declarations)

    desired: dict[str, Artifact] = {}
    changed_keys: set[str] = set()
    for declaration in current_declarations:
        for resource in declaration.resources:
            fingerprint = canonical_fingerprint(
                resource.key, resource.properties, resource.references
            )
            previous = previous_artifacts.get(resource.key)
            if previous is None or previous.input_fingerprint != fingerprint:
                next_version = (previous.artifact_version + 1) if previous else 1
                desired[resource.key] = Artifact(
                    resource_key=resource.key,
                    artifact_version=next_version,
                    input_fingerprint=fingerprint,
                    references=tuple(resource.references),
                    properties=dict(resource.properties),
                )
                changed_keys.add(resource.key)
            else:
                # Unchanged resource: immutable artifact version preserved.
                desired[resource.key] = Artifact(
                    resource_key=resource.key,
                    artifact_version=previous.artifact_version,
                    input_fingerprint=previous.input_fingerprint,
                    references=tuple(resource.references),
                    properties=dict(resource.properties),
                    invalid=previous.invalid,
                    invalid_reason=previous.invalid_reason,
                    invalidated_by=previous.invalidated_by,
                    invalidation_path=previous.invalidation_path,
                )

    removed_keys = set(previous_artifacts) - set(desired)
    roots = changed_keys | removed_keys
    propagation = graph.propagation_paths(roots)
    invalidated_keys: set[str] = set()
    for key, path in propagation.items():
        artifact = desired.get(key)
        if artifact is None or key in changed_keys:
            continue
        root = path[0]
        artifact.invalid = True
        artifact.invalid_reason = "upstream_changed"
        artifact.invalidated_by = root
        artifact.invalidation_path = path
        invalidated_keys.add(key)

    old_edges = set()
    if previous_artifacts:
        for artifact in previous_artifacts.values():
            for reference in artifact.references:
                old_edges.add((reference, artifact.resource_key))
    graph_delta = 1 if (
        set(graph.edges) != old_edges
        or set(graph.nodes) != set(previous_artifacts)
    ) else 0
    return desired, graph, changed_keys, invalidated_keys, graph_delta
