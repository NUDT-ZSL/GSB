"""Auditable, policy-driven v1 <-> v2 migration."""

from __future__ import annotations

import copy
import functools
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .errors import AppError, ErrorCode, fail
from .model import (
    CanonicalSample,
    Confidence,
    Document,
    Edge,
    JsonDict,
    Node,
    V1Argument,
    V1Document,
    V1Entity,
    V1Relation,
    V1Sample,
    V1Trigger,
    V2Sample,
    diff_graphs,
    graph_snapshot,
)
from .ontology import OntologySnapshot


SUPPORTED_V2_TO_V1_POLICIES = {
    "coref": {"drop"},
    "multi_trigger_event": {"split"},
    "confidence": {"drop"},
    "canonical_id": {"drop"},
}


def _migration_attribution(source_version: str):
    def decorator(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except AppError as exc:
                exc.attributed(
                    stage="migration",
                    version=source_version,
                    format="json",
                )
                raise

        return wrapper

    return decorator


@dataclass
class MigrationReport:
    source_version: str
    target_version: str
    ontology_version: int
    ontology_checksum: str
    operations: list[JsonDict] = field(default_factory=list)
    synthesized: list[JsonDict] = field(default_factory=list)
    dropped: list[JsonDict] = field(default_factory=list)
    conflicts: list[JsonDict] = field(default_factory=list)
    differences: list[JsonDict] = field(default_factory=list)
    graph_after: JsonDict = field(default_factory=dict)
    ok: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "ontology_version": self.ontology_version,
            "ontology_checksum": self.ontology_checksum,
            "operations": self.operations,
            "synthesized": self.synthesized,
            "dropped": self.dropped,
            "conflicts": self.conflicts,
            "differences": self.differences,
            "graph_after": self.graph_after,
            "ok": self.ok,
        }


@dataclass
class MigrationResult:
    sample: CanonicalSample
    report: MigrationReport


def _confidence_from_policy(
    ontology: OntologySnapshot,
    section: str,
    key: str,
    default: tuple[float, float],
) -> Confidence:
    raw = ontology.policy(section, key, default=list(default))
    if (
        not isinstance(raw, list)
        or len(raw) != 2
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in raw)
    ):
        fail(
            ErrorCode.ONTOLOGY_CONTRACT,
            f"migration policy {section}.{key} must be a two-number interval",
            path=f"$.migration_policy.{section}.{key}",
            stage="migration",
        )
    low, high = float(raw[0]), float(raw[1])
    if not (0.0 <= low <= high <= 1.0):
        fail(
            ErrorCode.ONTOLOGY_CONTRACT,
            f"migration policy {section}.{key} is not a valid confidence interval",
            path=f"$.migration_policy.{section}.{key}",
            stage="migration",
        )
    return Confidence(low=low, high=high)


def _stable_canonical_id(entity_type: str, text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    return f"{entity_type.lower()}:{normalized}"


def _canonical_id_for(
    ontology: OntologySnapshot,
    doc_id: str,
    entity: V1Entity,
) -> tuple[str, bool]:
    overrides = ontology.policy("v1_to_v2", "canonical_id_overrides", default={})
    if not isinstance(overrides, dict):
        fail(
            ErrorCode.ONTOLOGY_CONTRACT,
            "canonical_id_overrides must be an object",
            path="$.migration_policy.v1_to_v2.canonical_id_overrides",
            stage="migration",
        )
    for key in (f"{doc_id}:{entity.id}", entity.id):
        value = overrides.get(key)
        if value is not None:
            if not isinstance(value, str) or not value:
                fail(
                    ErrorCode.ONTOLOGY_CONTRACT,
                    "canonical id override must be a non-empty string",
                    path=f"$.migration_policy.v1_to_v2.canonical_id_overrides.{key}",
                    stage="migration",
                )
            return value, False
    strategy = ontology.policy(
        "v1_to_v2", "canonical_id_strategy", default="stable_type_text"
    )
    if strategy != "stable_type_text":
        fail(
            ErrorCode.ONTOLOGY_CONTRACT,
            f"unsupported canonical_id_strategy {strategy!r}",
            path="$.migration_policy.v1_to_v2.canonical_id_strategy",
            stage="migration",
        )
    return _stable_canonical_id(entity.type, entity.text), True


def _map_role(ontology: OntologySnapshot, role: str, argument_id: str, path: str) -> tuple[str, bool]:
    if role in ontology.argument_roles:
        return role, False
    role_map = ontology.policy("v1_to_v2", "role_map", default={})
    if not isinstance(role_map, dict):
        fail(
            ErrorCode.ONTOLOGY_CONTRACT,
            "role_map must be an object",
            path="$.migration_policy.v1_to_v2.role_map",
            stage="migration",
        )
    mapped = role_map.get(role)
    unknown_policy = ontology.policy(
        "v1_to_v2", "unknown_role_policy", default="reject"
    )
    if unknown_policy not in {"reject", "map"}:
        fail(
            ErrorCode.ONTOLOGY_CONTRACT,
            "unknown_role_policy must be 'reject' or 'map'",
            path="$.migration_policy.v1_to_v2.unknown_role_policy",
            stage="migration",
        )
    if mapped is None:
        fail(
            ErrorCode.ROLE_INVALID,
            f"v1 argument role {role!r} cannot be expressed by the active role set",
            path=path,
            stage="migration",
            version="v1",
            annotation_id=argument_id,
            details={
                "unknown_role_policy": unknown_policy,
                "role_map": role_map,
                "controlled_values": sorted(ontology.argument_roles),
            },
        )
    if not isinstance(mapped, str) or mapped not in ontology.argument_roles:
        fail(
            ErrorCode.ROLE_INVALID,
            f"role mapping for {role!r} does not resolve inside the controlled set",
            path=path,
            stage="migration",
            version="v1",
            annotation_id=argument_id,
            details={"mapped": mapped, "controlled_values": sorted(ontology.argument_roles)},
        )
    return mapped, True


@_migration_attribution("v1")
def upgrade_v1_to_v2(
    sample: V1Sample,
    ontology: OntologySnapshot,
) -> MigrationResult:
    """Upgrade v1 and preserve all v1-expressible graph semantics."""

    report = MigrationReport(
        source_version="v1",
        target_version="v2",
        ontology_version=ontology.version,
        ontology_checksum=ontology.checksum_value,
    )
    default_node_confidence = _confidence_from_policy(
        ontology, "v1_to_v2", "default_node_confidence", (1.0, 1.0)
    )
    default_edge_confidence = _confidence_from_policy(
        ontology, "v1_to_v2", "default_edge_confidence", (1.0, 1.0)
    )
    documents: list[Document] = []
    total_entities = 0
    total_relations = 0
    total_triggers = 0
    total_arguments = 0

    for doc_index, source_doc in enumerate(sample.documents):
        document = Document(doc_id=source_doc.doc_id, text=source_doc.text)
        for entity_index, entity in enumerate(source_doc.entities):
            path = f"$.documents[{doc_index}].entities[{entity_index}].type"
            ontology.require_entity_type(entity.type, path, entity.id)
            canonical_id, synthesized = _canonical_id_for(
                ontology, source_doc.doc_id, entity
            )
            document.nodes.append(
                Node(
                    id=entity.id,
                    kind="entity",
                    type=entity.type,
                    start=entity.start,
                    end=entity.end,
                    text=entity.text,
                    confidence=default_node_confidence,
                    canonical_id=canonical_id,
                )
            )
            total_entities += 1
            if synthesized:
                report.synthesized.append(
                    {
                        "field": "canonical_id",
                        "path": f"$.documents[{doc_index}].entities[{entity_index}].canonical_id",
                        "annotation_id": entity.id,
                        "value": canonical_id,
                        "policy": "stable_type_text",
                    }
                )
        for trigger_index, trigger in enumerate(source_doc.triggers):
            event_id = f"evt:{trigger.id}"
            document.nodes.append(
                Node(
                    id=trigger.id,
                    kind="trigger",
                    type=trigger.type,
                    start=trigger.start,
                    end=trigger.end,
                    text=trigger.text,
                    confidence=default_node_confidence,
                    event_id=event_id,
                )
            )
            total_triggers += 1
            report.synthesized.append(
                {
                    "field": "event_id",
                    "path": f"$.documents[{doc_index}].triggers[{trigger_index}].event_id",
                    "annotation_id": trigger.id,
                    "value": event_id,
                    "policy": "one_implicit_event_per_v1_trigger",
                }
            )
        for relation_index, relation in enumerate(source_doc.relations):
            path = f"$.documents[{doc_index}].relations[{relation_index}].type"
            ontology.require_relation_type(relation.type, path, relation.id)
            document.edges.append(
                Edge(
                    id=relation.id,
                    kind="relation",
                    type=relation.type,
                    source=relation.head,
                    target=relation.tail,
                    confidence=default_edge_confidence,
                )
            )
            total_relations += 1
        trigger_event = {
            trigger.id: f"evt:{trigger.id}" for trigger in source_doc.triggers
        }
        for argument_index, argument in enumerate(source_doc.arguments):
            path = f"$.documents[{doc_index}].arguments[{argument_index}].role"
            role, mapped = _map_role(ontology, argument.role, argument.id, path)
            event_id = trigger_event.get(argument.trigger_id)
            if event_id is None:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"argument trigger {argument.trigger_id!r} does not resolve",
                    path=f"$.documents[{doc_index}].arguments[{argument_index}].trigger_id",
                    stage="migration",
                    version="v1",
                    annotation_id=argument.id,
                )
            document.edges.append(
                Edge(
                    id=argument.id,
                    kind="argument",
                    source=argument.entity_id,
                    target=argument.trigger_id,
                    role=role,
                    event_id=event_id,
                    confidence=default_edge_confidence,
                )
            )
            total_arguments += 1
            if mapped:
                report.synthesized.append(
                    {
                        "field": "role",
                        "path": f"$.documents[{doc_index}].arguments[{argument_index}].role",
                        "annotation_id": argument.id,
                        "from": argument.role,
                        "value": role,
                        "policy": "role_map",
                    }
                )
        documents.append(document)

    metadata = copy.deepcopy(sample.metadata)
    metadata["migration"] = {
        "from": "v1",
        "to": "v2",
        "ontology_version": ontology.version,
        "ontology_checksum": ontology.checksum_value,
        "policy": "one_implicit_event_per_v1_trigger",
    }
    upgraded = V2Sample(
        sample_id=sample.sample_id,
        documents=documents,
        coref=[],
        metadata=metadata,
    )
    from .consistency import validate_v2

    validate_v2(upgraded, ontology, stage="migration")
    report.operations = [
        {"name": "entities_to_nodes", "count": total_entities},
        {"name": "triggers_to_event_nodes", "count": total_triggers},
        {"name": "relations_to_edges", "count": total_relations},
        {"name": "arguments_to_event_edges", "count": total_arguments},
    ]
    report.graph_after = graph_snapshot(upgraded)
    return MigrationResult(sample=upgraded, report=report)


def _check_downgrade_policy(ontology: OntologySnapshot) -> dict[str, str]:
    raw = ontology.policy("v2_to_v1", default={})
    if not isinstance(raw, dict):
        fail(
            ErrorCode.ONTOLOGY_CONTRACT,
            "v2_to_v1 migration policy must be an object",
            path="$.migration_policy.v2_to_v1",
            stage="migration",
        )
    effective: dict[str, str] = {}
    for key, allowed in SUPPORTED_V2_TO_V1_POLICIES.items():
        value = raw.get(key, next(iter(allowed)))
        if value not in allowed:
            fail(
                ErrorCode.ONTOLOGY_CONTRACT,
                f"unsupported v2_to_v1 policy {key}={value!r}",
                path=f"$.migration_policy.v2_to_v1.{key}",
                stage="migration",
                details={"allowed": sorted(allowed)},
            )
        effective[key] = value
    return effective


@_migration_attribution("v2")
def downgrade_v2_to_v1(
    sample: V2Sample,
    ontology: OntologySnapshot,
) -> MigrationResult:
    """Downgrade using explicit loss/merge rules with per-item details."""

    from .consistency import validate_v2

    validate_v2(sample, ontology, stage="migration")
    policies = _check_downgrade_policy(ontology)
    report = MigrationReport(
        source_version="v2",
        target_version="v1",
        ontology_version=ontology.version,
        ontology_checksum=ontology.checksum_value,
    )
    event_triggers: dict[str, list[str]] = {}
    for doc_index, document in enumerate(sample.documents):
        for node_index, node in enumerate(document.nodes):
            if node.kind == "trigger":
                event_triggers.setdefault(node.event_id or "", []).append(node.id)
    for event_id, trigger_ids in sorted(event_triggers.items()):
        if len(trigger_ids) <= 1:
            continue
        report.conflicts.append(
            {
                "category": "event_multi_trigger",
                "event_id": event_id,
                "trigger_ids": sorted(trigger_ids),
                "rule": "split_to_per_trigger_v1_events",
                "path": f"$.graph.events[event_id={event_id}]",
                "detail": (
                    "v1 has no explicit multi-trigger event ownership; "
                    "each trigger becomes one implicit v1 event"
                ),
            }
        )

    for group_index, group in enumerate(sample.coref):
        if policies["coref"] == "drop":
            report.dropped.append(
                {
                    "category": "coref",
                    "path": f"$.coref[{group_index}]",
                    "annotation_id": group.id,
                    "value": group.to_dict(),
                    "rule": "unsupported_in_v1",
                }
            )

    documents: list[V1Document] = []
    for doc_index, source_doc in enumerate(sample.documents):
        document = V1Document(doc_id=source_doc.doc_id, text=source_doc.text)
        for node_index, node in enumerate(source_doc.nodes):
            if policies["confidence"] == "drop":
                report.dropped.append(
                    {
                        "category": "node_confidence",
                        "path": f"$.documents[{doc_index}].nodes[{node_index}].confidence",
                        "annotation_id": node.id,
                        "value": node.confidence.to_dict(),
                        "rule": "confidence_not_expressible_in_v1",
                    }
                )
            if node.kind == "entity":
                if policies["canonical_id"] == "drop":
                    report.dropped.append(
                        {
                            "category": "canonical_id",
                            "path": f"$.documents[{doc_index}].nodes[{node_index}].canonical_id",
                            "annotation_id": node.id,
                            "value": node.canonical_id,
                            "rule": "canonical_id_not_expressible_in_v1",
                        }
                    )
                document.entities.append(
                    V1Entity(
                        id=node.id,
                        type=node.type,
                        start=node.start,
                        end=node.end,
                        text=node.text,
                    )
                )
            else:
                document.triggers.append(
                    V1Trigger(
                        id=node.id,
                        type=node.type,
                        start=node.start,
                        end=node.end,
                        text=node.text,
                    )
                )
        for edge_index, edge in enumerate(source_doc.edges):
            if policies["confidence"] == "drop":
                report.dropped.append(
                    {
                        "category": "edge_confidence",
                        "path": f"$.documents[{doc_index}].edges[{edge_index}].confidence",
                        "annotation_id": edge.id,
                        "value": edge.confidence.to_dict(),
                        "rule": "confidence_not_expressible_in_v1",
                    }
                )
            if edge.kind == "relation":
                document.relations.append(
                    V1Relation(
                        id=edge.id,
                        type=edge.type or "",
                        head=edge.source,
                        tail=edge.target,
                    )
                )
            else:
                document.arguments.append(
                    V1Argument(
                        id=edge.id,
                        trigger_id=edge.target,
                        entity_id=edge.source,
                        role=edge.role or "",
                    )
                )
        documents.append(document)

    metadata = copy.deepcopy(sample.metadata)
    metadata["migration"] = {
        "from": "v2",
        "to": "v1",
        "ontology_version": ontology.version,
        "ontology_checksum": ontology.checksum_value,
        "policies": policies,
        "dropped_count": len(report.dropped),
        "conflict_count": len(report.conflicts),
    }
    downgraded = V1Sample(
        sample_id=sample.sample_id,
        documents=documents,
        metadata=metadata,
    )

    # A deterministic v1->v2 re-upgrade makes every semantic delta explicit.
    roundtrip = upgrade_v1_to_v2(downgraded, ontology)
    report.differences = diff_graphs(
        graph_snapshot(sample), graph_snapshot(roundtrip.sample)
    )
    report.operations = [
        {"name": "entities_from_nodes", "count": sum(len(doc.entities) for doc in documents)},
        {"name": "triggers_from_nodes", "count": sum(len(doc.triggers) for doc in documents)},
        {"name": "relations_from_edges", "count": sum(len(doc.relations) for doc in documents)},
        {"name": "arguments_from_edges", "count": sum(len(doc.arguments) for doc in documents)},
    ]
    report.graph_after = graph_snapshot(downgraded)
    return MigrationResult(sample=downgraded, report=report)


def migrate_roundtrip(
    sample: CanonicalSample,
    ontology: OntologySnapshot,
) -> dict[str, Any]:
    """Run a full roundtrip and return both artifacts and audit details."""

    if isinstance(sample, V1Sample):
        up = upgrade_v1_to_v2(sample, ontology)
        down = downgrade_v2_to_v1(up.sample, ontology)
        original_graph = graph_snapshot(sample)
        result_graph = graph_snapshot(down.sample)
        return {
            "source_version": "v1",
            "upgrade": up.report.to_dict(),
            "downgrade": down.report.to_dict(),
            "sample": down.sample,
            "differences": diff_graphs(original_graph, result_graph),
        }
    down = downgrade_v2_to_v1(sample, ontology)
    up = upgrade_v1_to_v2(down.sample, ontology)
    return {
        "source_version": "v2",
        "downgrade": down.report.to_dict(),
        "upgrade": up.report.to_dict(),
        "sample": up.sample,
        "differences": diff_graphs(graph_snapshot(sample), graph_snapshot(up.sample)),
    }

