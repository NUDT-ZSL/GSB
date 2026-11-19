"""Graph invariants, argument tracing, and cross-module self-checks."""

from __future__ import annotations

from typing import Any

from .errors import AppError, ErrorCode, ErrorLocation, fail
from .model import (
    CanonicalSample,
    JsonDict,
    V1Sample,
    V2Sample,
    diff_graphs,
    graph_snapshot,
    stable_json,
)
from .ontology import OntologySnapshot


def _v2_node_paths(sample: V2Sample) -> dict[str, tuple[int, int, str]]:
    paths: dict[str, tuple[int, int, str]] = {}
    for doc_index, document in enumerate(sample.documents):
        for node_index, node in enumerate(document.nodes):
            paths[node.id] = (
                doc_index,
                node_index,
                f"$.documents[{doc_index}].nodes[{node_index}]",
            )
    return paths


def _validate_v2_span(
    node: Any,
    document: Any,
    path: str,
    *,
    stage: str,
) -> None:
    for key in ("start", "end"):
        value = getattr(node, key)
        if isinstance(value, bool) or not isinstance(value, int):
            fail(
                ErrorCode.OFFSET_TYPE,
                "offset must be an integer code-point position",
                path=f"{path}.{key}",
                stage=stage,
                version="v2",
                annotation_id=node.id,
            )
    if (
        node.start < 0
        or node.end > len(document.text)
        or node.start >= node.end
        or document.text[node.start : node.end] != node.text
    ):
        fail(
            ErrorCode.SPAN_INVALID,
            "node span/text is inconsistent with the document",
            path=f"{path}.start",
            stage=stage,
            version="v2",
            annotation_id=node.id,
            details={
                "start": node.start,
                "end": node.end,
                "text": node.text,
                "expected": (
                    document.text[node.start : node.end]
                    if isinstance(node.start, int) and isinstance(node.end, int)
                    else None
                ),
            },
        )


def validate_v2(
    sample: V2Sample,
    ontology: OntologySnapshot,
    *,
    stage: str = "invariant",
) -> None:
    """Validate every invariant required before a state can be committed."""

    node_paths = _v2_node_paths(sample)
    if len(node_paths) != sum(len(doc.nodes) for doc in sample.documents):
        seen: set[str] = set()
        for doc_index, document in enumerate(sample.documents):
            for node_index, node in enumerate(document.nodes):
                if node.id in seen:
                    fail(
                        ErrorCode.DUP_ID,
                        f"duplicate node id {node.id!r}",
                        path=f"$.documents[{doc_index}].nodes[{node_index}].id",
                        stage=stage,
                        version="v2",
                        annotation_id=node.id,
                    )
                seen.add(node.id)

    edge_ids: set[str] = set()
    for doc_index, document in enumerate(sample.documents):
        for node_index, node in enumerate(document.nodes):
            path = f"$.documents[{doc_index}].nodes[{node_index}]"
            if not isinstance(node.id, str) or not node.id:
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    "node id must be a non-empty string",
                    path=f"{path}.id",
                    stage=stage,
                    version="v2",
                )
            if node.kind not in {"entity", "trigger"}:
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    "node kind must be 'entity' or 'trigger'",
                    path=f"{path}.kind",
                    stage=stage,
                    version="v2",
                    annotation_id=node.id,
                )
            _validate_v2_span(node, document, path, stage=stage)
            if node.kind == "entity":
                if not node.canonical_id:
                    fail(
                        ErrorCode.SCHEMA_MISSING,
                        "entity node requires a canonical_id",
                        path=f"{path}.canonical_id",
                        stage=stage,
                        version="v2",
                        annotation_id=node.id,
                    )
                ontology.require_entity_type(
                    node.type, f"{path}.type", node.id
                )
            else:
                if not node.event_id:
                    fail(
                        ErrorCode.TRIGGER_CONFLICT,
                        "trigger must be owned by exactly one event",
                        path=f"{path}.event_id",
                        stage=stage,
                        version="v2",
                        annotation_id=node.id,
                        details={"event_id": node.event_id},
                    )

        doc_nodes = {node.id: node for node in document.nodes}
        for edge_index, edge in enumerate(document.edges):
            path = f"$.documents[{doc_index}].edges[{edge_index}]"
            if edge.id in edge_ids:
                fail(
                    ErrorCode.DUP_ID,
                    f"duplicate edge id {edge.id!r}",
                    path=f"{path}.id",
                    stage=stage,
                    version="v2",
                    annotation_id=edge.id,
                )
            edge_ids.add(edge.id)
            if edge.kind not in {"relation", "argument"}:
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    "edge kind must be 'relation' or 'argument'",
                    path=f"{path}.kind",
                    stage=stage,
                    version="v2",
                    annotation_id=edge.id,
                )
            source = doc_nodes.get(edge.source)
            target = doc_nodes.get(edge.target)
            if source is None:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"edge source {edge.source!r} does not resolve",
                    path=f"{path}.source",
                    stage=stage,
                    version="v2",
                    annotation_id=edge.id,
                )
            if target is None:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"edge target {edge.target!r} does not resolve",
                    path=f"{path}.target",
                    stage=stage,
                    version="v2",
                    annotation_id=edge.id,
                )
            if edge.kind == "relation":
                if source.kind != "entity" or target.kind != "entity":
                    fail(
                        ErrorCode.DANGLING_REF,
                        "relation endpoints must both be entity nodes",
                        path=f"{path}.source",
                        stage=stage,
                        version="v2",
                        annotation_id=edge.id,
                    )
                if not edge.type:
                    fail(
                        ErrorCode.SCHEMA_MISSING,
                        "relation edge requires a type",
                        path=f"{path}.type",
                        stage=stage,
                        version="v2",
                        annotation_id=edge.id,
                    )
                ontology.require_relation_type(
                    edge.type, f"{path}.type", edge.id
                )
            else:
                if source.kind != "entity" or target.kind != "trigger":
                    fail(
                        ErrorCode.DANGLING_REF,
                        "argument edge must go from an entity to a trigger",
                        path=f"{path}.source",
                        stage=stage,
                        version="v2",
                        annotation_id=edge.id,
                    )
                if not edge.role:
                    fail(
                        ErrorCode.SCHEMA_MISSING,
                        "argument edge requires a role",
                        path=f"{path}.role",
                        stage=stage,
                        version="v2",
                        annotation_id=edge.id,
                    )
                ontology.require_role(edge.role, f"{path}.role", edge.id)
                if not edge.event_id or edge.event_id != target.event_id:
                    fail(
                        ErrorCode.INCONSISTENT,
                        "argument is not traceable to its trigger's event",
                        path=f"{path}.event_id",
                        stage=stage,
                        version="v2",
                        annotation_id=edge.id,
                        details={
                            "edge_event_id": edge.event_id,
                            "trigger_event_id": target.event_id,
                        },
                    )

    members_seen: dict[str, str] = {}
    coref_ids: set[str] = set()
    for group_index, group in enumerate(sample.coref):
        if not group.id or group.id in coref_ids:
            fail(
                ErrorCode.DUP_ID,
                f"duplicate or empty coref group id {group.id!r}",
                path=f"$.coref[{group_index}].id",
                stage=stage,
                version="v2",
                annotation_id=group.id,
            )
        coref_ids.add(group.id)
        if not group.canonical_id:
            fail(
                ErrorCode.COREF_UNRESOLVED,
                "coref group requires a canonical_id",
                path=f"$.coref[{group_index}].canonical_id",
                stage=stage,
                version="v2",
                annotation_id=group.id,
            )
        if len(group.members) < 2:
            fail(
                ErrorCode.COREF_UNRESOLVED,
                "coref group must contain at least two members",
                path=f"$.coref[{group_index}].members",
                stage=stage,
                version="v2",
                annotation_id=group.id,
            )
        for member_index, member in enumerate(group.members):
            if member in members_seen:
                fail(
                    ErrorCode.COREF_UNRESOLVED,
                    "entity is already owned by another coref group",
                    path=f"$.coref[{group_index}].members[{member_index}]",
                    stage=stage,
                    version="v2",
                    annotation_id=group.id,
                    details={"other_group": members_seen[member]},
                )
            members_seen[member] = group.id
            found = node_paths.get(member)
            if found is None:
                fail(
                    ErrorCode.COREF_UNRESOLVED,
                    f"coref member {member!r} does not resolve",
                    path=f"$.coref[{group_index}].members[{member_index}]",
                    stage=stage,
                    version="v2",
                    annotation_id=group.id,
                )
            doc_index, node_index, _ = found
            node = sample.documents[doc_index].nodes[node_index]
            if node.kind != "entity":
                fail(
                    ErrorCode.COREF_UNRESOLVED,
                    "coref members must be entity nodes",
                    path=f"$.coref[{group_index}].members[{member_index}]",
                    stage=stage,
                    version="v2",
                    annotation_id=group.id,
                )


def validate_v1(sample: V1Sample, ontology: OntologySnapshot) -> None:
    for doc_index, document in enumerate(sample.documents):
        entity_ids = {entity.id for entity in document.entities}
        trigger_ids = {trigger.id for trigger in document.triggers}
        for relation_index, relation in enumerate(document.relations):
            ontology.require_relation_type(
                relation.type,
                f"$.documents[{doc_index}].relations[{relation_index}].type",
                relation.id,
            )
            if relation.head not in entity_ids:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"relation head {relation.head!r} does not resolve",
                    path=f"$.documents[{doc_index}].relations[{relation_index}].head",
                    stage="invariant",
                    version="v1",
                    annotation_id=relation.id,
                )
            if relation.tail not in entity_ids:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"relation tail {relation.tail!r} does not resolve",
                    path=f"$.documents[{doc_index}].relations[{relation_index}].tail",
                    stage="invariant",
                    version="v1",
                    annotation_id=relation.id,
                )
        for argument_index, argument in enumerate(document.arguments):
            ontology.require_role(
                argument.role,
                f"$.documents[{doc_index}].arguments[{argument_index}].role",
                argument.id,
            )
            if argument.trigger_id not in trigger_ids:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"argument trigger {argument.trigger_id!r} does not resolve",
                    path=f"$.documents[{doc_index}].arguments[{argument_index}].trigger_id",
                    stage="invariant",
                    version="v1",
                    annotation_id=argument.id,
                )
            if argument.entity_id not in entity_ids:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"argument entity {argument.entity_id!r} does not resolve",
                    path=f"$.documents[{doc_index}].arguments[{argument_index}].entity_id",
                    stage="invariant",
                    version="v1",
                    annotation_id=argument.id,
                )


def assert_same_graph(
    expected: CanonicalSample,
    actual: CanonicalSample,
    *,
    code: str,
    stage: str,
    format_name: str | None = None,
) -> None:
    differences = diff_graphs(graph_snapshot(expected), graph_snapshot(actual))
    if differences:
        first = differences[0]
        raise AppError(
            code,
            f"event graph mismatch in {first['category']} at {first['path']}",
            location=ErrorLocation(
                stage=stage,
                format=format_name,
                field_path=first["path"],
            ),
            details={
                "category": first["category"],
                "first_difference": first,
                "difference_count": len(differences),
            },
        )


def _full_semantics(sample: V2Sample) -> JsonDict:
    return {
        "graph": graph_snapshot(sample),
        "coref": sorted(
            [
                {
                    "id": group.id,
                    "canonical_id": group.canonical_id,
                    "members": sorted(group.members),
                }
                for group in sample.coref
            ],
            key=lambda row: row["id"],
        ),
        "node_confidences": sorted(
            [
                {
                    "id": node.id,
                    "low": node.confidence.low,
                    "high": node.confidence.high,
                }
                for _, node in sample.iter_nodes()
            ],
            key=lambda row: row["id"],
        ),
        "edge_confidences": sorted(
            [
                {
                    "id": edge.id,
                    "low": edge.confidence.low,
                    "high": edge.confidence.high,
                }
                for _, edge in sample.iter_edges()
            ],
            key=lambda row: row["id"],
        ),
    }


def assert_same_full_semantics(
    expected: V2Sample,
    actual: V2Sample,
    *,
    stage: str,
) -> None:
    expected_value = _full_semantics(expected)
    actual_value = _full_semantics(actual)
    for section in (
        "graph",
        "coref",
        "node_confidences",
        "edge_confidences",
    ):
        if expected_value[section] == actual_value[section]:
            continue
        if section == "graph":
            first = diff_graphs(expected_value[section], actual_value[section])[0]
            path = first["path"]
            detail = first
        else:
            expected_rows = {
                stable_json(row): row for row in expected_value[section]
            }
            actual_rows = {
                stable_json(row): row for row in actual_value[section]
            }
            missing = sorted(
                (row for key, row in expected_rows.items() if key not in actual_rows),
                key=stable_json,
            )
            extra = sorted(
                (row for key, row in actual_rows.items() if key not in expected_rows),
                key=stable_json,
            )
            detail = {
                "category": section,
                "kind": "missing" if missing else "unexpected",
                "expected": missing[0] if missing else None,
                "actual": extra[0] if extra else None,
            }
            identifier = (
                (missing[0] if missing else extra[0]).get("id", "<unknown>")
            )
            path = f"$.{section}[id={identifier}]"
        fail(
            ErrorCode.ROUNDTRIP_MISMATCH,
            f"roundtrip mismatch at {path}",
            path=path,
            stage=stage,
            details={
                "category": detail.get("category", section),
                "first_difference": detail,
            },
        )


def argument_trace(sample: V2Sample) -> list[JsonDict]:
    nodes = {node.id: node for _, node in sample.iter_nodes()}
    trace: list[JsonDict] = []
    for doc, edge in sample.iter_edges():
        if edge.kind != "argument":
            continue
        trigger = nodes.get(edge.target)
        trace.append(
            {
                "argument_id": edge.id,
                "doc_id": doc.doc_id,
                "entity_id": edge.source,
                "trigger_id": edge.target,
                "trigger_event_id": trigger.event_id if trigger else None,
                "argument_event_id": edge.event_id,
                "role": edge.role,
                "traceable": bool(
                    trigger
                    and trigger.kind == "trigger"
                    and trigger.event_id == edge.event_id
                ),
            }
        )
    return sorted(trace, key=lambda row: row["argument_id"])


def cross_check(
    reference: V2Sample,
    candidates: dict[str, V2Sample],
    *,
    ontology: OntologySnapshot,
) -> JsonDict:
    """Compare migration, line, and edited graphs against one reference."""

    validate_v2(reference, ontology, stage="consistency")
    checks: list[JsonDict] = []
    for name, candidate in candidates.items():
        validate_v2(candidate, ontology, stage="consistency")
        differences = diff_graphs(
            graph_snapshot(reference), graph_snapshot(candidate)
        )
        if differences:
            first = differences[0]
            fail(
                ErrorCode.INCONSISTENT,
                f"cross-module graph mismatch in {name} at {first['path']}",
                path=first["path"],
                stage="consistency",
                details={
                    "category": first["category"],
                    "path_name": name,
                    "first_difference": first,
                },
            )
        checks.append(
            {
                "path": name,
                "status": "equal",
                "category": "event_graph",
            }
        )
    trace = argument_trace(reference)
    broken = [row for row in trace if not row["traceable"]]
    if broken:
        fail(
            ErrorCode.INCONSISTENT,
            "an argument cannot be traced to its event trigger",
            path=f"$.arguments[id={broken[0]['argument_id']}]",
            stage="consistency",
            details={"category": "argument_trace", "first": broken[0]},
        )
    return {
        "status": "ok",
        "checks": checks,
        "argument_trace": trace,
    }

