"""Strict v1/v2 readers with version dispatch and fail-closed validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .errors import AppError, ErrorCode, fail
from .model import (
    Confidence,
    CorefGroup,
    Document,
    Edge,
    Node,
    V1Argument,
    V1Document,
    V1Entity,
    V1Relation,
    V1Sample,
    V1Trigger,
    V2Sample,
)
from .ontology import OntologySnapshot


KNOWN_V1_TOP = {"version", "sample_id", "documents"}
KNOWN_V2_TOP = {"version", "sample_id", "documents", "coref"}


def _require(
    obj: dict[str, Any],
    key: str,
    path: str,
    *,
    stage: str,
    format_name: str,
    version: str,
) -> Any:
    if key not in obj:
        fail(
            ErrorCode.SCHEMA_MISSING,
            f"missing required field {key!r}",
            path=f"{path}.{key}",
            stage=stage,
            format_name=format_name,
            version=version,
        )
    return obj[key]


def _require_dict(
    value: Any,
    path: str,
    *,
    stage: str,
    format_name: str,
    version: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(
            ErrorCode.SCHEMA_TYPE,
            "expected an object",
            path=path,
            stage=stage,
            format_name=format_name,
            version=version,
        )
    return value


def _require_list(
    value: Any,
    path: str,
    *,
    stage: str,
    format_name: str,
    version: str,
) -> list[Any]:
    if not isinstance(value, list):
        fail(
            ErrorCode.SCHEMA_TYPE,
            "expected a list",
            path=path,
            stage=stage,
            format_name=format_name,
            version=version,
        )
    return value


def _require_str(
    value: Any,
    path: str,
    *,
    stage: str,
    format_name: str,
    version: str,
    nonempty: bool = True,
) -> str:
    if not isinstance(value, str):
        fail(
            ErrorCode.SCHEMA_TYPE,
            "expected a string",
            path=path,
            stage=stage,
            format_name=format_name,
            version=version,
        )
    if nonempty and not value:
        fail(
            ErrorCode.SCHEMA_TYPE,
            "expected a non-empty string",
            path=path,
            stage=stage,
            format_name=format_name,
            version=version,
        )
    return value


def _require_offset(
    value: Any,
    path: str,
    *,
    format_name: str,
    version: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            ErrorCode.OFFSET_TYPE,
            "offset must be a Unicode code-point integer",
            path=path,
            stage="parse",
            format_name=format_name,
            version=version,
            details={"actual_type": type(value).__name__, "actual": value},
        )
    return value


def _validate_span(
    *,
    start: int,
    end: int,
    text_value: str,
    document_text: str,
    path: str,
    annotation_id: str,
    format_name: str,
    version: str,
) -> None:
    if start < 0 or end > len(document_text) or start >= end:
        fail(
            ErrorCode.SPAN_INVALID,
            "span must satisfy 0 <= start < end <= document code-point length",
            path=f"{path}.start",
            stage="parse",
            format_name=format_name,
            version=version,
            annotation_id=annotation_id,
            details={
                "start": start,
                "end": end,
                "text_length_code_points": len(document_text),
            },
        )
    expected = document_text[start:end]
    if text_value != expected:
        fail(
            ErrorCode.SPAN_INVALID,
            "annotation text does not match the document code-point slice",
            path=f"{path}.text",
            stage="parse",
            format_name=format_name,
            version=version,
            annotation_id=annotation_id,
            details={"expected": expected, "actual": text_value},
        )


def _validate_confidence(
    value: Any,
    path: str,
    *,
    format_name: str,
    version: str,
) -> Confidence:
    value = _require_dict(
        value,
        path,
        stage="parse",
        format_name=format_name,
        version=version,
    )
    for key in ("low", "high"):
        if key not in value:
            fail(
                ErrorCode.SCHEMA_MISSING,
                f"missing confidence field {key!r}",
                path=f"{path}.{key}",
                stage="parse",
                format_name=format_name,
                version=version,
            )
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            fail(
                ErrorCode.SCHEMA_TYPE,
                "confidence bound must be numeric",
                path=f"{path}.{key}",
                stage="parse",
                format_name=format_name,
                version=version,
            )
    low = float(value["low"])
    high = float(value["high"])
    if not (0.0 <= low <= high <= 1.0):
        fail(
            ErrorCode.SCHEMA_TYPE,
            "confidence interval must satisfy 0 <= low <= high <= 1",
            path=path,
            stage="parse",
            format_name=format_name,
            version=version,
            details={"low": low, "high": high},
        )
    return Confidence(low=low, high=high)


def _check_duplicate_id(
    seen: dict[str, str],
    identifier: str,
    *,
    first_path: str,
    current_path: str,
    format_name: str,
    version: str,
    category: str,
) -> None:
    if identifier in seen:
        fail(
            ErrorCode.DUP_ID,
            f"duplicate {category} id {identifier!r}",
            path=f"{current_path}.id",
            stage="parse",
            format_name=format_name,
            version=version,
            annotation_id=identifier,
            details={"first_path": first_path, "category": category},
        )


def parse_v1(
    value: Any,
    *,
    ontology: OntologySnapshot | None = None,
    format_name: str = "json",
) -> V1Sample:
    version = "v1"
    stage = "parse"
    root = _require_dict(
        value, "$", stage=stage, format_name=format_name, version=version
    )
    for key in ("version", "sample_id", "documents"):
        _require(root, key, "$", stage=stage, format_name=format_name, version=version)
    if root["version"] != "v1":
        fail(
            ErrorCode.VERSION_UNSUPPORTED,
            f"expected version 'v1', got {root['version']!r}",
            path="$.version",
            stage=stage,
            format_name=format_name,
            version=version,
        )
    sample_id = _require_str(
        root["sample_id"],
        "$.sample_id",
        stage=stage,
        format_name=format_name,
        version=version,
    )
    documents_value = _require_list(
        root["documents"],
        "$.documents",
        stage=stage,
        format_name=format_name,
        version=version,
    )

    node_ids: dict[str, str] = {}
    edge_ids: dict[str, str] = {}
    doc_ids: set[str] = set()
    documents: list[V1Document] = []

    for doc_index, doc_value in enumerate(documents_value):
        doc_path = f"$.documents[{doc_index}]"
        doc_obj = _require_dict(
            doc_value,
            doc_path,
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for key in ("doc_id", "text", "entities", "relations", "triggers", "arguments"):
            _require(
                doc_obj,
                key,
                doc_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
        doc_id = _require_str(
            doc_obj["doc_id"],
            f"{doc_path}.doc_id",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        if doc_id in doc_ids:
            fail(
                ErrorCode.DUP_ID,
                f"duplicate document id {doc_id!r}",
                path=f"{doc_path}.doc_id",
                stage=stage,
                format_name=format_name,
                version=version,
                annotation_id=doc_id,
            )
        doc_ids.add(doc_id)
        text = _require_str(
            doc_obj["text"],
            f"{doc_path}.text",
            stage=stage,
            format_name=format_name,
            version=version,
            nonempty=False,
        )
        document = V1Document(doc_id=doc_id, text=text)

        entities_value = _require_list(
            doc_obj["entities"],
            f"{doc_path}.entities",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for entity_index, entity_value in enumerate(entities_value):
            entity_path = f"{doc_path}.entities[{entity_index}]"
            entity_obj = _require_dict(
                entity_value,
                entity_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
            for key in ("id", "type", "start", "end", "text"):
                _require(
                    entity_obj,
                    key,
                    entity_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
            entity_id = _require_str(
                entity_obj["id"],
                f"{entity_path}.id",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            _check_duplicate_id(
                node_ids,
                entity_id,
                first_path=node_ids.get(entity_id, ""),
                current_path=entity_path,
                format_name=format_name,
                version=version,
                category="node",
            )
            node_ids[entity_id] = entity_path
            entity_type = _require_str(
                entity_obj["type"],
                f"{entity_path}.type",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            start = _require_offset(
                entity_obj["start"],
                f"{entity_path}.start",
                format_name=format_name,
                version=version,
            )
            end = _require_offset(
                entity_obj["end"],
                f"{entity_path}.end",
                format_name=format_name,
                version=version,
            )
            entity_text = _require_str(
                entity_obj["text"],
                f"{entity_path}.text",
                stage=stage,
                format_name=format_name,
                version=version,
                nonempty=False,
            )
            _validate_span(
                start=start,
                end=end,
                text_value=entity_text,
                document_text=text,
                path=entity_path,
                annotation_id=entity_id,
                format_name=format_name,
                version=version,
            )
            if ontology is not None:
                ontology.require_entity_type(entity_type, f"{entity_path}.type", entity_id)
            document.entities.append(
                V1Entity(
                    id=entity_id,
                    type=entity_type,
                    start=start,
                    end=end,
                    text=entity_text,
                )
            )

        triggers_value = _require_list(
            doc_obj["triggers"],
            f"{doc_path}.triggers",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for trigger_index, trigger_value in enumerate(triggers_value):
            trigger_path = f"{doc_path}.triggers[{trigger_index}]"
            trigger_obj = _require_dict(
                trigger_value,
                trigger_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
            for key in ("id", "type", "start", "end", "text"):
                _require(
                    trigger_obj,
                    key,
                    trigger_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
            trigger_id = _require_str(
                trigger_obj["id"],
                f"{trigger_path}.id",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            _check_duplicate_id(
                node_ids,
                trigger_id,
                first_path=node_ids.get(trigger_id, ""),
                current_path=trigger_path,
                format_name=format_name,
                version=version,
                category="node",
            )
            node_ids[trigger_id] = trigger_path
            trigger_type = _require_str(
                trigger_obj["type"],
                f"{trigger_path}.type",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            start = _require_offset(
                trigger_obj["start"],
                f"{trigger_path}.start",
                format_name=format_name,
                version=version,
            )
            end = _require_offset(
                trigger_obj["end"],
                f"{trigger_path}.end",
                format_name=format_name,
                version=version,
            )
            trigger_text = _require_str(
                trigger_obj["text"],
                f"{trigger_path}.text",
                stage=stage,
                format_name=format_name,
                version=version,
                nonempty=False,
            )
            _validate_span(
                start=start,
                end=end,
                text_value=trigger_text,
                document_text=text,
                path=trigger_path,
                annotation_id=trigger_id,
                format_name=format_name,
                version=version,
            )
            document.triggers.append(
                V1Trigger(
                    id=trigger_id,
                    type=trigger_type,
                    start=start,
                    end=end,
                    text=trigger_text,
                )
            )

        relations_value = _require_list(
            doc_obj["relations"],
            f"{doc_path}.relations",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for relation_index, relation_value in enumerate(relations_value):
            relation_path = f"{doc_path}.relations[{relation_index}]"
            relation_obj = _require_dict(
                relation_value,
                relation_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
            for key in ("id", "type", "head", "tail"):
                _require(
                    relation_obj,
                    key,
                    relation_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
            relation_id = _require_str(
                relation_obj["id"],
                f"{relation_path}.id",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            _check_duplicate_id(
                edge_ids,
                relation_id,
                first_path=edge_ids.get(relation_id, ""),
                current_path=relation_path,
                format_name=format_name,
                version=version,
                category="edge",
            )
            edge_ids[relation_id] = relation_path
            relation_type = _require_str(
                relation_obj["type"],
                f"{relation_path}.type",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            head = _require_str(
                relation_obj["head"],
                f"{relation_path}.head",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            tail = _require_str(
                relation_obj["tail"],
                f"{relation_path}.tail",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            if ontology is not None:
                ontology.require_relation_type(
                    relation_type, f"{relation_path}.type", relation_id
                )
            document.relations.append(
                V1Relation(id=relation_id, type=relation_type, head=head, tail=tail)
            )

        arguments_value = _require_list(
            doc_obj["arguments"],
            f"{doc_path}.arguments",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for argument_index, argument_value in enumerate(arguments_value):
            argument_path = f"{doc_path}.arguments[{argument_index}]"
            argument_obj = _require_dict(
                argument_value,
                argument_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
            for key in ("id", "trigger_id", "entity_id", "role"):
                _require(
                    argument_obj,
                    key,
                    argument_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
            argument_id = _require_str(
                argument_obj["id"],
                f"{argument_path}.id",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            _check_duplicate_id(
                edge_ids,
                argument_id,
                first_path=edge_ids.get(argument_id, ""),
                current_path=argument_path,
                format_name=format_name,
                version=version,
                category="edge",
            )
            edge_ids[argument_id] = argument_path
            trigger_id = _require_str(
                argument_obj["trigger_id"],
                f"{argument_path}.trigger_id",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            entity_id = _require_str(
                argument_obj["entity_id"],
                f"{argument_path}.entity_id",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            role = _require_str(
                argument_obj["role"],
                f"{argument_path}.role",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            if ontology is not None:
                ontology.require_role(role, f"{argument_path}.role", argument_id)
            document.arguments.append(
                V1Argument(
                    id=argument_id,
                    trigger_id=trigger_id,
                    entity_id=entity_id,
                    role=role,
                )
            )
        documents.append(document)

    sample = V1Sample(
        sample_id=sample_id,
        documents=documents,
        metadata={
            key: value
            for key, value in root.items()
            if key not in KNOWN_V1_TOP
        },
    )
    _validate_v1_references(sample, format_name=format_name)
    return sample


def _validate_v1_references(sample: V1Sample, *, format_name: str) -> None:
    for doc_index, doc in enumerate(sample.documents):
        entity_ids = {entity.id for entity in doc.entities}
        trigger_ids = {trigger.id for trigger in doc.triggers}
        for relation_index, relation in enumerate(doc.relations):
            path = f"$.documents[{doc_index}].relations[{relation_index}]"
            if relation.head not in entity_ids:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"relation head {relation.head!r} does not resolve",
                    path=f"{path}.head",
                    stage="parse",
                    format_name=format_name,
                    version="v1",
                    annotation_id=relation.id,
                )
            if relation.tail not in entity_ids:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"relation tail {relation.tail!r} does not resolve",
                    path=f"{path}.tail",
                    stage="parse",
                    format_name=format_name,
                    version="v1",
                    annotation_id=relation.id,
                )
        for argument_index, argument in enumerate(doc.arguments):
            path = f"$.documents[{doc_index}].arguments[{argument_index}]"
            if argument.trigger_id not in trigger_ids:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"argument trigger {argument.trigger_id!r} does not resolve",
                    path=f"{path}.trigger_id",
                    stage="parse",
                    format_name=format_name,
                    version="v1",
                    annotation_id=argument.id,
                )
            if argument.entity_id not in entity_ids:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"argument entity {argument.entity_id!r} does not resolve",
                    path=f"{path}.entity_id",
                    stage="parse",
                    format_name=format_name,
                    version="v1",
                    annotation_id=argument.id,
                )


def parse_v2(
    value: Any,
    *,
    ontology: OntologySnapshot | None = None,
    format_name: str = "json",
) -> V2Sample:
    version = "v2"
    stage = "parse"
    root = _require_dict(
        value, "$", stage=stage, format_name=format_name, version=version
    )
    for key in ("version", "sample_id", "documents", "coref"):
        _require(root, key, "$", stage=stage, format_name=format_name, version=version)
    if root["version"] != "v2":
        fail(
            ErrorCode.VERSION_UNSUPPORTED,
            f"expected version 'v2', got {root['version']!r}",
            path="$.version",
            stage=stage,
            format_name=format_name,
            version=version,
        )
    sample_id = _require_str(
        root["sample_id"],
        "$.sample_id",
        stage=stage,
        format_name=format_name,
        version=version,
    )
    documents_value = _require_list(
        root["documents"],
        "$.documents",
        stage=stage,
        format_name=format_name,
        version=version,
    )
    coref_value = _require_list(
        root["coref"],
        "$.coref",
        stage=stage,
        format_name=format_name,
        version=version,
    )

    node_ids: dict[str, str] = {}
    edge_ids: dict[str, str] = {}
    doc_ids: set[str] = set()
    documents: list[Document] = []

    for doc_index, doc_value in enumerate(documents_value):
        doc_path = f"$.documents[{doc_index}]"
        doc_obj = _require_dict(
            doc_value,
            doc_path,
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for key in ("doc_id", "text", "nodes", "edges"):
            _require(
                doc_obj,
                key,
                doc_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
        doc_id = _require_str(
            doc_obj["doc_id"],
            f"{doc_path}.doc_id",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        if doc_id in doc_ids:
            fail(
                ErrorCode.DUP_ID,
                f"duplicate document id {doc_id!r}",
                path=f"{doc_path}.doc_id",
                stage=stage,
                format_name=format_name,
                version=version,
                annotation_id=doc_id,
            )
        doc_ids.add(doc_id)
        text = _require_str(
            doc_obj["text"],
            f"{doc_path}.text",
            stage=stage,
            format_name=format_name,
            version=version,
            nonempty=False,
        )
        document = Document(doc_id=doc_id, text=text)

        nodes_value = _require_list(
            doc_obj["nodes"],
            f"{doc_path}.nodes",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for node_index, node_value in enumerate(nodes_value):
            node_path = f"{doc_path}.nodes[{node_index}]"
            node_obj = _require_dict(
                node_value,
                node_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
            common = ("id", "kind", "type", "start", "end", "text", "confidence")
            for key in common:
                _require(
                    node_obj,
                    key,
                    node_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
            node_id = _require_str(
                node_obj["id"],
                f"{node_path}.id",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            _check_duplicate_id(
                node_ids,
                node_id,
                first_path=node_ids.get(node_id, ""),
                current_path=node_path,
                format_name=format_name,
                version=version,
                category="node",
            )
            node_ids[node_id] = node_path
            kind = _require_str(
                node_obj["kind"],
                f"{node_path}.kind",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            if kind not in {"entity", "trigger"}:
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    "node kind must be 'entity' or 'trigger'",
                    path=f"{node_path}.kind",
                    stage=stage,
                    format_name=format_name,
                    version=version,
                    annotation_id=node_id,
                )
            node_type = _require_str(
                node_obj["type"],
                f"{node_path}.type",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            start = _require_offset(
                node_obj["start"],
                f"{node_path}.start",
                format_name=format_name,
                version=version,
            )
            end = _require_offset(
                node_obj["end"],
                f"{node_path}.end",
                format_name=format_name,
                version=version,
            )
            node_text = _require_str(
                node_obj["text"],
                f"{node_path}.text",
                stage=stage,
                format_name=format_name,
                version=version,
                nonempty=False,
            )
            _validate_span(
                start=start,
                end=end,
                text_value=node_text,
                document_text=text,
                path=node_path,
                annotation_id=node_id,
                format_name=format_name,
                version=version,
            )
            confidence = _validate_confidence(
                node_obj["confidence"],
                f"{node_path}.confidence",
                format_name=format_name,
                version=version,
            )
            canonical_id: str | None = None
            event_id: str | None = None
            if kind == "entity":
                _require(
                    node_obj,
                    "canonical_id",
                    node_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
                canonical_id = _require_str(
                    node_obj["canonical_id"],
                    f"{node_path}.canonical_id",
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
                if ontology is not None:
                    ontology.require_entity_type(
                        node_type, f"{node_path}.type", node_id
                    )
            else:
                _require(
                    node_obj,
                    "event_id",
                    node_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
                event_id = _require_str(
                    node_obj["event_id"],
                    f"{node_path}.event_id",
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
            document.nodes.append(
                Node(
                    id=node_id,
                    kind=kind,
                    type=node_type,
                    start=start,
                    end=end,
                    text=node_text,
                    confidence=confidence,
                    canonical_id=canonical_id,
                    event_id=event_id,
                )
            )

        edges_value = _require_list(
            doc_obj["edges"],
            f"{doc_path}.edges",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for edge_index, edge_value in enumerate(edges_value):
            edge_path = f"{doc_path}.edges[{edge_index}]"
            edge_obj = _require_dict(
                edge_value,
                edge_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
            for key in ("id", "kind", "source", "target", "confidence"):
                _require(
                    edge_obj,
                    key,
                    edge_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
            edge_id = _require_str(
                edge_obj["id"],
                f"{edge_path}.id",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            _check_duplicate_id(
                edge_ids,
                edge_id,
                first_path=edge_ids.get(edge_id, ""),
                current_path=edge_path,
                format_name=format_name,
                version=version,
                category="edge",
            )
            edge_ids[edge_id] = edge_path
            kind = _require_str(
                edge_obj["kind"],
                f"{edge_path}.kind",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            if kind not in {"relation", "argument"}:
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    "edge kind must be 'relation' or 'argument'",
                    path=f"{edge_path}.kind",
                    stage=stage,
                    format_name=format_name,
                    version=version,
                    annotation_id=edge_id,
                )
            source = _require_str(
                edge_obj["source"],
                f"{edge_path}.source",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            target = _require_str(
                edge_obj["target"],
                f"{edge_path}.target",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            confidence = _validate_confidence(
                edge_obj["confidence"],
                f"{edge_path}.confidence",
                format_name=format_name,
                version=version,
            )
            edge_type: str | None = None
            role: str | None = None
            event_id: str | None = None
            if kind == "relation":
                _require(
                    edge_obj,
                    "type",
                    edge_path,
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
                edge_type = _require_str(
                    edge_obj["type"],
                    f"{edge_path}.type",
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
                if ontology is not None:
                    ontology.require_relation_type(
                        edge_type, f"{edge_path}.type", edge_id
                    )
            else:
                for key in ("role", "event_id"):
                    _require(
                        edge_obj,
                        key,
                        edge_path,
                        stage=stage,
                        format_name=format_name,
                        version=version,
                    )
                role = _require_str(
                    edge_obj["role"],
                    f"{edge_path}.role",
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
                event_id = _require_str(
                    edge_obj["event_id"],
                    f"{edge_path}.event_id",
                    stage=stage,
                    format_name=format_name,
                    version=version,
                )
                if ontology is not None:
                    ontology.require_role(role, f"{edge_path}.role", edge_id)
            document.edges.append(
                Edge(
                    id=edge_id,
                    kind=kind,
                    source=source,
                    target=target,
                    confidence=confidence,
                    type=edge_type,
                    role=role,
                    event_id=event_id,
                )
            )
        documents.append(document)

    coref_groups: list[CorefGroup] = []
    coref_ids: set[str] = set()
    for group_index, group_value in enumerate(coref_value):
        group_path = f"$.coref[{group_index}]"
        group_obj = _require_dict(
            group_value,
            group_path,
            stage=stage,
            format_name=format_name,
            version=version,
        )
        for key in ("id", "canonical_id", "members"):
            _require(
                group_obj,
                key,
                group_path,
                stage=stage,
                format_name=format_name,
                version=version,
            )
        group_id = _require_str(
            group_obj["id"],
            f"{group_path}.id",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        if group_id in coref_ids:
            fail(
                ErrorCode.DUP_ID,
                f"duplicate coref group id {group_id!r}",
                path=f"{group_path}.id",
                stage=stage,
                format_name=format_name,
                version=version,
                annotation_id=group_id,
            )
        coref_ids.add(group_id)
        canonical_id = _require_str(
            group_obj["canonical_id"],
            f"{group_path}.canonical_id",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        members_value = _require_list(
            group_obj["members"],
            f"{group_path}.members",
            stage=stage,
            format_name=format_name,
            version=version,
        )
        if len(members_value) < 2:
            fail(
                ErrorCode.COREF_UNRESOLVED,
                "coref group must contain at least two entity members",
                path=f"{group_path}.members",
                stage=stage,
                format_name=format_name,
                version=version,
                annotation_id=group_id,
            )
        members: list[str] = []
        for member_index, member_value in enumerate(members_value):
            member = _require_str(
                member_value,
                f"{group_path}.members[{member_index}]",
                stage=stage,
                format_name=format_name,
                version=version,
            )
            if member in members:
                fail(
                    ErrorCode.DUP_ID,
                    f"duplicate coref member {member!r}",
                    path=f"{group_path}.members[{member_index}]",
                    stage=stage,
                    format_name=format_name,
                    version=version,
                    annotation_id=group_id,
                )
            members.append(member)
        coref_groups.append(
            CorefGroup(id=group_id, canonical_id=canonical_id, members=members)
        )

    sample = V2Sample(
        sample_id=sample_id,
        documents=documents,
        coref=coref_groups,
        metadata={
            key: value
            for key, value in root.items()
            if key not in KNOWN_V2_TOP
        },
    )
    _validate_v2_references(sample, format_name=format_name)
    return sample


def _validate_v2_references(sample: V2Sample, *, format_name: str) -> None:
    membership: dict[str, str] = {}
    for group_index, group in enumerate(sample.coref):
        for member_index, member in enumerate(group.members):
            if member in membership:
                fail(
                    ErrorCode.COREF_UNRESOLVED,
                    "entity belongs to more than one coref group",
                    path=f"$.coref[{group_index}].members[{member_index}]",
                    stage="parse",
                    format_name=format_name,
                    version="v2",
                    annotation_id=group.id,
                    details={"other_group": membership[member]},
                )
            membership[member] = group.id

    node_lookup: dict[str, tuple[Document, Node]] = {
        node.id: (doc, node) for doc in sample.documents for node in doc.nodes
    }
    for group_index, group in enumerate(sample.coref):
        for member_index, member in enumerate(group.members):
            found = node_lookup.get(member)
            if found is None or found[1].kind != "entity":
                fail(
                    ErrorCode.COREF_UNRESOLVED,
                    f"coref member {member!r} does not resolve to an entity",
                    path=f"$.coref[{group_index}].members[{member_index}]",
                    stage="parse",
                    format_name=format_name,
                    version="v2",
                    annotation_id=group.id,
                )

    for doc_index, document in enumerate(sample.documents):
        node_lookup_doc = {node.id: node for node in document.nodes}
        for edge_index, edge in enumerate(document.edges):
            path = f"$.documents[{doc_index}].edges[{edge_index}]"
            source = node_lookup_doc.get(edge.source)
            target = node_lookup_doc.get(edge.target)
            if source is None:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"edge source {edge.source!r} does not resolve in the document",
                    path=f"{path}.source",
                    stage="parse",
                    format_name=format_name,
                    version="v2",
                    annotation_id=edge.id,
                )
            if target is None:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"edge target {edge.target!r} does not resolve in the document",
                    path=f"{path}.target",
                    stage="parse",
                    format_name=format_name,
                    version="v2",
                    annotation_id=edge.id,
                )
            if edge.kind == "relation":
                if source.kind != "entity":
                    fail(
                        ErrorCode.DANGLING_REF,
                        "relation source must be an entity node",
                        path=f"{path}.source",
                        stage="parse",
                        format_name=format_name,
                        version="v2",
                        annotation_id=edge.id,
                    )
                if target.kind != "entity":
                    fail(
                        ErrorCode.DANGLING_REF,
                        "relation target must be an entity node",
                        path=f"{path}.target",
                        stage="parse",
                        format_name=format_name,
                        version="v2",
                        annotation_id=edge.id,
                    )
            else:
                if source.kind != "entity":
                    fail(
                        ErrorCode.DANGLING_REF,
                        "argument source must be an entity node",
                        path=f"{path}.source",
                        stage="parse",
                        format_name=format_name,
                        version="v2",
                        annotation_id=edge.id,
                    )
                if target.kind != "trigger":
                    fail(
                        ErrorCode.DANGLING_REF,
                        "argument target must be a trigger node",
                        path=f"{path}.target",
                        stage="parse",
                        format_name=format_name,
                        version="v2",
                        annotation_id=edge.id,
                    )
                if target.event_id != edge.event_id:
                    fail(
                        ErrorCode.INCONSISTENT,
                        "argument event_id disagrees with its target trigger",
                        path=f"{path}.event_id",
                        stage="parse",
                        format_name=format_name,
                        version="v2",
                        annotation_id=edge.id,
                        details={
                            "edge_event_id": edge.event_id,
                            "trigger_event_id": target.event_id,
                        },
                    )


def parse_document(
    payload: str | bytes | dict[str, Any],
    *,
    ontology: OntologySnapshot | None = None,
    format_hint: str | None = None,
    max_import_bytes: int | None = None,
) -> V1Sample | V2Sample:
    """Parse JSON or line format with version dispatch.

    The function either returns a fully validated sample or raises AppError;
    callers can never act on a partially parsed result.
    """

    raw_bytes: bytes | None = None
    if isinstance(payload, bytes):
        raw_bytes = payload
        if max_import_bytes is not None and len(payload) > max_import_bytes:
            fail(
                ErrorCode.RESOURCE_LIMIT,
                "import exceeds the configured byte limit",
                path="$",
                stage="import",
                format_name=format_hint or "auto",
                details={"limit": max_import_bytes, "actual": len(payload)},
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppError(
                ErrorCode.FORMAT_INVALID,
                "input is not valid UTF-8",
                path="$",
                details={"byte_offset": exc.start},
                cause=exc,
            ) from exc
    elif isinstance(payload, str):
        text = payload
        raw_bytes = payload.encode("utf-8")
        if max_import_bytes is not None and len(raw_bytes) > max_import_bytes:
            fail(
                ErrorCode.RESOURCE_LIMIT,
                "import exceeds the configured byte limit",
                path="$",
                stage="import",
                format_name=format_hint or "auto",
                details={"limit": max_import_bytes, "actual": len(raw_bytes)},
            )
    elif isinstance(payload, dict):
        text = ""
    else:
        fail(
            ErrorCode.SCHEMA_TYPE,
            "input must be UTF-8 text, bytes, or a JSON object",
            path="$",
            stage="import",
            format_name=format_hint or "auto",
        )

    if isinstance(payload, dict):
        version = payload.get("version")
    else:
        stripped = text.lstrip("\ufeff \t\r\n")
        if stripped.startswith("#LEDGER-LINE|") or format_hint == "line":
            from .lineformat import parse_line_format

            return parse_line_format(
                text,
                ontology=ontology,
                max_import_bytes=max_import_bytes,
            )
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            fail(
                ErrorCode.FORMAT_INVALID,
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                path="$",
                stage="parse",
                format_name=format_hint or "json",
                line=exc.lineno,
                details={"column": exc.colno},
            )
        if not isinstance(value, dict):
            fail(
                ErrorCode.SCHEMA_TYPE,
                "top-level JSON value must be an object with a version header",
                path="$",
                stage="parse",
                format_name=format_hint or "json",
            )
        payload = value
        version = payload.get("version")

    if version is None:
        fail(
            ErrorCode.SCHEMA_MISSING,
            "missing version header",
            path="$.version",
            stage="parse",
            format_name=format_hint or "json",
        )
    if version == "v1":
        return parse_v1(payload, ontology=ontology, format_name=format_hint or "json")
    if version == "v2":
        return parse_v2(payload, ontology=ontology, format_name=format_hint or "json")
    fail(
        ErrorCode.VERSION_UNSUPPORTED,
        f"unsupported version header {version!r}",
        path="$.version",
        stage="parse",
        format_name=format_hint or "json",
    )


def read_file(
    path: Path,
    *,
    ontology: OntologySnapshot | None = None,
    max_import_bytes: int | None = None,
) -> V1Sample | V2Sample:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AppError(
            ErrorCode.FORMAT_INVALID,
            f"cannot read input file: {exc}",
            path="$",
            details={"file": str(path)},
            cause=exc,
        ) from exc
    return parse_document(
        payload,
        ontology=ontology,
        max_import_bytes=max_import_bytes,
    )


def read_and_map(
    path: Path,
    mapper: Callable[[Any], Any],
    *,
    max_import_bytes: int | None = None,
) -> Any:
    """Read a JSON file under the import-size cap before applying a mapper."""

    payload = path.read_bytes()
    if max_import_bytes is not None and len(payload) > max_import_bytes:
        fail(
            ErrorCode.RESOURCE_LIMIT,
            "import exceeds the configured byte limit",
            path="$",
            stage="import",
            format_name="json",
            details={"limit": max_import_bytes, "actual": len(payload)},
        )
    return mapper(json.loads(payload.decode("utf-8")))

