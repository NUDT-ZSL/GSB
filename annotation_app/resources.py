"""Configurable resource caps with explicit fail-or-degrade behavior."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .config import Limits
from .errors import ErrorCode, fail
from .model import CanonicalSample, JsonDict, V1Sample, V2Sample


@dataclass
class ResourceResult:
    sample: CanonicalSample
    report: JsonDict


def _counts(sample: CanonicalSample) -> tuple[int, int]:
    if isinstance(sample, V1Sample):
        entities = sum(len(doc.entities) for doc in sample.documents)
        arguments = sum(len(doc.arguments) for doc in sample.documents)
        nodes = sum(len(doc.entities) + len(doc.triggers) for doc in sample.documents)
        return entities + arguments, nodes
    entities = sum(
        1
        for _, node in sample.iter_nodes()
        if node.kind == "entity"
    )
    arguments = sum(
        1
        for _, edge in sample.iter_edges()
        if edge.kind == "argument"
    )
    nodes = sum(len(document.nodes) for document in sample.documents)
    return entities + arguments, nodes


def enforce_limits(
    sample: CanonicalSample,
    limits: Limits,
    *,
    stage: str,
    allow_degrade: bool = True,
) -> ResourceResult:
    annotation_total, node_count = _counts(sample)
    violations: list[JsonDict] = []
    if annotation_total > limits.max_annotation_total:
        violations.append(
            {
                "category": "annotations",
                "limit": limits.max_annotation_total,
                "actual": annotation_total,
            }
        )
    if node_count > limits.max_graph_nodes:
        violations.append(
            {
                "category": "graph_nodes",
                "limit": limits.max_graph_nodes,
                "actual": node_count,
            }
        )
    if not violations:
        return ResourceResult(
            sample=sample,
            report={
                "status": "within_limits",
                "annotation_total": annotation_total,
                "graph_nodes": node_count,
            },
        )
    if limits.on_limit == "entities_only" and allow_degrade:
        degraded = _entities_only(sample, stage, violations)
        return ResourceResult(
            sample=degraded,
            report={
                "status": "degraded",
                "category": "entities_only",
                "violations": violations,
                "limits": limits.to_dict(),
            },
        )
    fail(
        ErrorCode.RESOURCE_LIMIT,
        "sample exceeds configured resource limits",
        path="$",
        stage=stage,
        details={
            "violations": violations,
            "on_limit": limits.on_limit,
            "degradation_allowed": allow_degrade,
        },
    )


def _entities_only(
    sample: CanonicalSample,
    stage: str,
    violations: list[JsonDict],
) -> CanonicalSample:
    if isinstance(sample, V1Sample):
        degraded = copy.deepcopy(sample)
        removed_triggers = 0
        removed_relations = 0
        removed_arguments = 0
        for document in degraded.documents:
            removed_triggers += len(document.triggers)
            removed_relations += len(document.relations)
            removed_arguments += len(document.arguments)
            document.triggers = []
            document.relations = []
            document.arguments = []
        degraded.metadata["degradation"] = {
            "category": "entities_only",
            "stage": stage,
            "violations": violations,
            "removed": {
                "triggers": removed_triggers,
                "relations": removed_relations,
                "arguments": removed_arguments,
            },
        }
        return degraded

    degraded = sample.clone()
    removed_triggers = 0
    removed_edges = 0
    for document in degraded.documents:
        kept_nodes = []
        for node in document.nodes:
            if node.kind == "trigger":
                removed_triggers += 1
            else:
                kept_nodes.append(node)
        removed_edges += len(document.edges)
        document.nodes = kept_nodes
        document.edges = []
    removed_coref = len(degraded.coref)
    degraded.coref = []
    degraded.metadata["degradation"] = {
        "category": "entities_only",
        "stage": stage,
        "violations": violations,
        "removed": {
            "triggers": removed_triggers,
            "edges": removed_edges,
            "coref_groups": removed_coref,
        },
    }
    return degraded

