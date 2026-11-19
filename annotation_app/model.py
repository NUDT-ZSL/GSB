"""Canonical v1/v2 data models and stable graph snapshots."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator


JsonDict = dict[str, Any]


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Confidence:
    low: float
    high: float

    def to_dict(self) -> JsonDict:
        return {"low": self.low, "high": self.high}


@dataclass
class V1Entity:
    id: str
    type: str
    start: int
    end: int
    text: str

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "type": self.type,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


@dataclass
class V1Trigger:
    id: str
    type: str
    start: int
    end: int
    text: str

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "type": self.type,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


@dataclass
class V1Relation:
    id: str
    type: str
    head: str
    tail: str

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "type": self.type,
            "head": self.head,
            "tail": self.tail,
        }


@dataclass
class V1Argument:
    id: str
    trigger_id: str
    entity_id: str
    role: str

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "trigger_id": self.trigger_id,
            "entity_id": self.entity_id,
            "role": self.role,
        }


@dataclass
class V1Document:
    doc_id: str
    text: str
    entities: list[V1Entity] = field(default_factory=list)
    relations: list[V1Relation] = field(default_factory=list)
    triggers: list[V1Trigger] = field(default_factory=list)
    arguments: list[V1Argument] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "doc_id": self.doc_id,
            "text": self.text,
            "entities": [item.to_dict() for item in self.entities],
            "relations": [item.to_dict() for item in self.relations],
            "triggers": [item.to_dict() for item in self.triggers],
            "arguments": [item.to_dict() for item in self.arguments],
        }


@dataclass
class V1Sample:
    sample_id: str
    documents: list[V1Document] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    @property
    def version(self) -> str:
        return "v1"

    def to_dict(self) -> JsonDict:
        return {
            "version": "v1",
            "sample_id": self.sample_id,
            "documents": [doc.to_dict() for doc in self.documents],
            **copy.deepcopy(self.metadata),
        }

    def iter_entities(self) -> Iterator[tuple[V1Document, V1Entity]]:
        for doc in self.documents:
            for entity in doc.entities:
                yield doc, entity

    def iter_triggers(self) -> Iterator[tuple[V1Document, V1Trigger]]:
        for doc in self.documents:
            for trigger in doc.triggers:
                yield doc, trigger

    def iter_relations(self) -> Iterator[tuple[V1Document, V1Relation]]:
        for doc in self.documents:
            for relation in doc.relations:
                yield doc, relation

    def iter_arguments(self) -> Iterator[tuple[V1Document, V1Argument]]:
        for doc in self.documents:
            for argument in doc.arguments:
                yield doc, argument

    def entity_index(self) -> dict[str, tuple[V1Document, V1Entity]]:
        return {entity.id: (doc, entity) for doc, entity in self.iter_entities()}

    def trigger_index(self) -> dict[str, tuple[V1Document, V1Trigger]]:
        return {trigger.id: (doc, trigger) for doc, trigger in self.iter_triggers()}


@dataclass
class Node:
    id: str
    kind: str
    type: str
    start: int
    end: int
    text: str
    confidence: Confidence
    canonical_id: str | None = None
    event_id: str | None = None

    def to_dict(self) -> JsonDict:
        value: JsonDict = {
            "id": self.id,
            "kind": self.kind,
            "type": self.type,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "confidence": self.confidence.to_dict(),
        }
        if self.kind == "entity":
            value["canonical_id"] = self.canonical_id
        elif self.kind == "trigger":
            value["event_id"] = self.event_id
        return value


@dataclass
class Edge:
    id: str
    kind: str
    source: str
    target: str
    confidence: Confidence
    type: str | None = None
    role: str | None = None
    event_id: str | None = None

    def to_dict(self) -> JsonDict:
        value: JsonDict = {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "confidence": self.confidence.to_dict(),
        }
        if self.kind == "relation":
            value["type"] = self.type
        elif self.kind == "argument":
            value["role"] = self.role
            value["event_id"] = self.event_id
        return value


@dataclass
class CorefGroup:
    id: str
    canonical_id: str
    members: list[str]

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "canonical_id": self.canonical_id,
            "members": list(self.members),
        }


@dataclass
class Document:
    doc_id: str
    text: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "doc_id": self.doc_id,
            "text": self.text,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    def node_by_id(self, node_id: str) -> Node | None:
        return next((node for node in self.nodes if node.id == node_id), None)


@dataclass
class V2Sample:
    sample_id: str
    documents: list[Document] = field(default_factory=list)
    coref: list[CorefGroup] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    @property
    def version(self) -> str:
        return "v2"

    def to_dict(self) -> JsonDict:
        return {
            "version": "v2",
            "sample_id": self.sample_id,
            "documents": [doc.to_dict() for doc in self.documents],
            "coref": [group.to_dict() for group in self.coref],
            **copy.deepcopy(self.metadata),
        }

    def clone(self) -> "V2Sample":
        return copy.deepcopy(self)

    def iter_nodes(self) -> Iterator[tuple[Document, Node]]:
        for doc in self.documents:
            for node in doc.nodes:
                yield doc, node

    def iter_edges(self) -> Iterator[tuple[Document, Edge]]:
        for doc in self.documents:
            for edge in doc.edges:
                yield doc, edge

    def node_index(self) -> dict[str, tuple[Document, Node]]:
        return {node.id: (doc, node) for doc, node in self.iter_nodes()}

    def edge_index(self) -> dict[str, tuple[Document, Edge]]:
        return {edge.id: (doc, edge) for doc, edge in self.iter_edges()}

    def coref_membership(self) -> dict[str, str]:
        return {
            member: group.id
            for group in self.coref
            for member in group.members
        }


CanonicalSample = V1Sample | V2Sample


def sample_checksum(sample: CanonicalSample) -> str:
    return checksum(sample.to_dict())


def graph_snapshot(sample: CanonicalSample) -> JsonDict:
    """Return a representation suitable for cross-module graph comparison."""

    if isinstance(sample, V1Sample):
        entities: list[JsonDict] = []
        triggers: list[JsonDict] = []
        relations: list[JsonDict] = []
        arguments: list[JsonDict] = []
        for doc, entity in sample.iter_entities():
            entities.append(
                {
                    "doc": doc.doc_id,
                    "id": entity.id,
                    "type": entity.type,
                    "start": entity.start,
                    "end": entity.end,
                    "text": entity.text,
                }
            )
        for doc, trigger in sample.iter_triggers():
            triggers.append(
                {
                    "doc": doc.doc_id,
                    "id": trigger.id,
                    "type": trigger.type,
                    "start": trigger.start,
                    "end": trigger.end,
                    "text": trigger.text,
                    "event_id": f"evt:{trigger.id}",
                }
            )
        for doc, relation in sample.iter_relations():
            relations.append(
                {
                    "doc": doc.doc_id,
                    "id": relation.id,
                    "type": relation.type,
                    "source": relation.head,
                    "target": relation.tail,
                }
            )
        for doc, argument in sample.iter_arguments():
            arguments.append(
                {
                    "doc": doc.doc_id,
                    "id": argument.id,
                    "role": argument.role,
                    "source": argument.entity_id,
                    "target": argument.trigger_id,
                    "event_id": f"evt:{argument.trigger_id}",
                }
            )
        events: dict[str, list[str]] = {}
        for trigger in triggers:
            events.setdefault(trigger["event_id"], []).append(trigger["id"])
        event_rows = [
            {"event_id": event_id, "triggers": sorted(trigger_ids)}
            for event_id, trigger_ids in sorted(events.items())
        ]
    else:
        entities = []
        triggers = []
        relations = []
        arguments = []
        for doc, node in sample.iter_nodes():
            row = {
                "doc": doc.doc_id,
                "id": node.id,
                "type": node.type,
                "start": node.start,
                "end": node.end,
                "text": node.text,
            }
            if node.kind == "entity":
                row["canonical_id"] = node.canonical_id
                entities.append(row)
            else:
                row["event_id"] = node.event_id
                triggers.append(row)
        for doc, edge in sample.iter_edges():
            row = {
                "doc": doc.doc_id,
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
            }
            if edge.kind == "relation":
                row["type"] = edge.type
                relations.append(row)
            else:
                row["role"] = edge.role
                row["event_id"] = edge.event_id
                arguments.append(row)
        events = {}
        for trigger in triggers:
            events.setdefault(trigger["event_id"], []).append(trigger["id"])
        event_rows = [
            {"event_id": event_id, "triggers": sorted(trigger_ids)}
            for event_id, trigger_ids in sorted(events.items())
        ]

    return {
        "entities": sorted(entities, key=lambda row: row["id"]),
        "relations": sorted(relations, key=lambda row: row["id"]),
        "events": event_rows,
        "arguments": sorted(arguments, key=lambda row: row["id"]),
    }


def diff_graphs(expected: JsonDict, actual: JsonDict) -> list[JsonDict]:
    """Return stable, per-category differences with a concrete first path."""

    differences: list[JsonDict] = []
    for category in ("entities", "relations", "events", "arguments"):
        expected_rows = {stable_json(row): row for row in expected.get(category, [])}
        actual_rows = {stable_json(row): row for row in actual.get(category, [])}
        missing = [row for key, row in expected_rows.items() if key not in actual_rows]
        extra = [row for key, row in actual_rows.items() if key not in expected_rows]
        for row in sorted(missing, key=stable_json):
            differences.append(
                {
                    "category": category,
                    "kind": "missing",
                    "path": f"$.graph.{category}[id={row.get('id', row.get('event_id'))}]",
                    "value": row,
                }
            )
        for row in sorted(extra, key=stable_json):
            differences.append(
                {
                    "category": category,
                    "kind": "unexpected",
                    "path": f"$.graph.{category}[id={row.get('id', row.get('event_id'))}]",
                    "value": row,
                }
            )
    differences.sort(key=lambda item: (item["category"], item["path"], item["kind"]))
    return differences


def first_graph_difference(expected: JsonDict, actual: JsonDict) -> JsonDict | None:
    diffs = diff_graphs(expected, actual)
    return diffs[0] if diffs else None


def flatten_nodes(sample: V2Sample) -> Iterable[tuple[Document, Node]]:
    return sample.iter_nodes()

