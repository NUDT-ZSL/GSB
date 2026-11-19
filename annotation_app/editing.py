"""Transactional graph edits with an append-only audit ledger."""

from __future__ import annotations

import copy
import unicodedata
from typing import Any

from .audit import AuditLedger
from .consistency import validate_v2
from .errors import AppError, ErrorCode, fail
from .model import (
    Confidence,
    Edge,
    JsonDict,
    Node,
    V2Sample,
    sample_checksum,
)
from .ontology import OntologySnapshot


EDIT_OPERATIONS = {
    "add_entity",
    "delete_entity",
    "add_argument",
    "delete_argument",
    "change_argument_role",
    "split_event",
    "merge_events",
}


def _payload_str(payload: dict[str, Any], key: str, operation: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        fail(
            ErrorCode.SCHEMA_MISSING if value is None else ErrorCode.SCHEMA_TYPE,
            f"edit payload requires non-empty string {key!r}",
            path=f"$.edit.{operation}.{key}",
            stage="edit",
            details={"operation": operation},
        )
    return value


def _payload_int(payload: dict[str, Any], key: str, operation: str) -> int:
    value = payload.get(key)
    if value is None:
        fail(
            ErrorCode.SCHEMA_MISSING,
            f"edit payload requires integer {key!r}",
            path=f"$.edit.{operation}.{key}",
            stage="edit",
            details={"operation": operation},
        )
    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            ErrorCode.OFFSET_TYPE,
            f"edit payload field {key!r} must be a code-point integer",
            path=f"$.edit.{operation}.{key}",
            stage="edit",
            details={"operation": operation, "actual": value},
        )
    return value


def _confidence_from_payload(payload: dict[str, Any], operation: str) -> Confidence:
    raw = payload.get("confidence", {"low": 1.0, "high": 1.0})
    if isinstance(raw, list) and len(raw) == 2:
        raw = {"low": raw[0], "high": raw[1]}
    if not isinstance(raw, dict) or "low" not in raw or "high" not in raw:
        fail(
            ErrorCode.SCHEMA_TYPE,
            "confidence must be {low, high} or a two-item list",
            path=f"$.edit.{operation}.confidence",
            stage="edit",
        )
    low, high = raw["low"], raw["high"]
    if (
        isinstance(low, bool)
        or isinstance(high, bool)
        or not isinstance(low, (int, float))
        or not isinstance(high, (int, float))
        or not (0.0 <= float(low) <= float(high) <= 1.0)
    ):
        fail(
            ErrorCode.SCHEMA_TYPE,
            "confidence must satisfy 0 <= low <= high <= 1",
            path=f"$.edit.{operation}.confidence",
            stage="edit",
        )
    return Confidence(low=float(low), high=float(high))


def _canonical_id(entity_type: str, text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    return f"{entity_type.lower()}:{normalized}"


class Editor:
    """Apply edits as validate-then-commit transactions."""

    def __init__(
        self,
        sample: V2Sample,
        ontology: OntologySnapshot,
        *,
        limits: Any | None = None,
        record_failures: bool = True,
    ) -> None:
        self.sample = sample.clone()
        self.ontology = ontology
        self.limits = limits
        self.record_failures = record_failures
        self.ledger = AuditLedger()

    def edit(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        record: bool = True,
    ) -> "Editor":
        if operation not in EDIT_OPERATIONS:
            fail(
                ErrorCode.FORMAT_INVALID,
                f"unknown edit operation {operation!r}",
                path="$.edit.operation",
                stage="edit",
                details={"allowed": sorted(EDIT_OPERATIONS)},
            )
        if not isinstance(payload, dict):
            fail(
                ErrorCode.SCHEMA_TYPE,
                "edit payload must be an object",
                path=f"$.edit.{operation}",
                stage="edit",
            )
        before_checksum = sample_checksum(self.sample)
        candidate = self.sample.clone()
        try:
            dispatch = getattr(self, f"_edit_{operation}")
            dispatch(candidate, copy.deepcopy(payload))
            validate_v2(candidate, self.ontology, stage="edit")
            if self.limits is not None:
                from .resources import enforce_limits

                enforce_limits(
                    candidate,
                    self.limits,
                    stage="edit",
                    allow_degrade=False,
                )
        except AppError as exc:
            exc.attributed(stage="edit", version="v2", format="v2")
            exc.details.setdefault("operation", operation)
            if record and self.record_failures:
                self._append_entry(
                    operation,
                    payload,
                    status="rejected",
                    before=before_checksum,
                    after=before_checksum,
                    error=exc.to_dict(),
                )
            raise
        self.sample = candidate
        if record:
            self._append_entry(
                operation,
                payload,
                status="applied",
                before=before_checksum,
                after=sample_checksum(candidate),
                error=None,
            )
        return self

    def apply_many(self, operations: list[dict[str, Any]]) -> "Editor":
        for item in operations:
            self.edit(item["operation"], item["payload"])
        return self

    def _append_entry(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        status: str,
        before: str,
        after: str,
        error: JsonDict | None,
    ) -> None:
        entry: JsonDict = {
            "seq": len(self.ledger.entries) + 1,
            "operation": operation,
            "payload": copy.deepcopy(payload),
            "status": status,
            "before_checksum": before,
            "after_checksum": after,
        }
        if error is not None:
            entry["error"] = error
        self.ledger.append(entry)

    @staticmethod
    def _find_doc(sample: V2Sample, doc_id: str, operation: str) -> Any:
        for document in sample.documents:
            if document.doc_id == doc_id:
                return document
        fail(
            ErrorCode.DANGLING_REF,
            f"document {doc_id!r} does not resolve",
            path=f"$.edit.{operation}.doc_id",
            stage="edit",
            version="v2",
        )

    @staticmethod
    def _find_node(sample: V2Sample, node_id: str, operation: str) -> tuple[Any, Node]:
        for document in sample.documents:
            for node in document.nodes:
                if node.id == node_id:
                    return document, node
        fail(
            ErrorCode.DANGLING_REF,
            f"node {node_id!r} does not resolve",
            path=f"$.edit.{operation}",
            stage="edit",
            version="v2",
            annotation_id=node_id,
        )

    @staticmethod
    def _find_edge(sample: V2Sample, edge_id: str, operation: str) -> tuple[Any, Edge]:
        for document in sample.documents:
            for edge in document.edges:
                if edge.id == edge_id:
                    return document, edge
        fail(
            ErrorCode.DANGLING_REF,
            f"edge {edge_id!r} does not resolve",
            path=f"$.edit.{operation}",
            stage="edit",
            version="v2",
            annotation_id=edge_id,
        )

    def _edit_add_entity(self, sample: V2Sample, payload: dict[str, Any]) -> None:
        doc_id = _payload_str(payload, "doc_id", "add_entity")
        entity_id = _payload_str(payload, "id", "add_entity")
        entity_type = _payload_str(payload, "type", "add_entity")
        start = _payload_int(payload, "start", "add_entity")
        end = _payload_int(payload, "end", "add_entity")
        text = _payload_str(payload, "text", "add_entity")
        document = self._find_doc(sample, doc_id, "add_entity")
        if any(node.id == entity_id for _, node in sample.iter_nodes()):
            fail(
                ErrorCode.DUP_ID,
                f"node id {entity_id!r} already exists",
                path="$.edit.add_entity.id",
                stage="edit",
                version="v2",
                annotation_id=entity_id,
            )
        if start < 0 or end > len(document.text) or start >= end:
            fail(
                ErrorCode.SPAN_INVALID,
                "new entity span is outside the document or empty",
                path="$.edit.add_entity.start",
                stage="edit",
                version="v2",
                annotation_id=entity_id,
                details={"start": start, "end": end, "length": len(document.text)},
            )
        if document.text[start:end] != text:
            fail(
                ErrorCode.SPAN_INVALID,
                "new entity text does not match the code-point slice",
                path="$.edit.add_entity.text",
                stage="edit",
                version="v2",
                annotation_id=entity_id,
            )
        self.ontology.require_entity_type(entity_type, "$.edit.add_entity.type", entity_id)
        canonical_id = payload.get("canonical_id") or _canonical_id(entity_type, text)
        if not isinstance(canonical_id, str) or not canonical_id:
            fail(
                ErrorCode.SCHEMA_TYPE,
                "canonical_id must be a non-empty string",
                path="$.edit.add_entity.canonical_id",
                stage="edit",
                version="v2",
                annotation_id=entity_id,
            )
        document.nodes.append(
            Node(
                id=entity_id,
                kind="entity",
                type=entity_type,
                start=start,
                end=end,
                text=text,
                confidence=_confidence_from_payload(payload, "add_entity"),
                canonical_id=canonical_id,
            )
        )

    def _edit_delete_entity(self, sample: V2Sample, payload: dict[str, Any]) -> None:
        entity_id = _payload_str(payload, "entity_id", "delete_entity")
        document, node = self._find_node(sample, entity_id, "delete_entity")
        if node.kind != "entity":
            fail(
                ErrorCode.DANGLING_REF,
                "delete_entity target is not an entity",
                path="$.edit.delete_entity.entity_id",
                stage="edit",
                version="v2",
                annotation_id=entity_id,
            )
        for edge in document.edges:
            if edge.source == entity_id or edge.target == entity_id:
                fail(
                    ErrorCode.DANGLING_REF,
                    f"entity {entity_id!r} is still referenced by edge {edge.id!r}",
                    path="$.edit.delete_entity.entity_id",
                    stage="edit",
                    version="v2",
                    annotation_id=entity_id,
                    details={"edge_id": edge.id},
                )
        for group in sample.coref:
            if entity_id in group.members:
                fail(
                    ErrorCode.COREF_UNRESOLVED,
                    f"entity {entity_id!r} is still referenced by coref group {group.id!r}",
                    path="$.edit.delete_entity.entity_id",
                    stage="edit",
                    version="v2",
                    annotation_id=entity_id,
                    details={"coref_id": group.id},
                )
        document.nodes.remove(node)

    def _edit_add_argument(self, sample: V2Sample, payload: dict[str, Any]) -> None:
        trigger_id = _payload_str(payload, "trigger_id", "add_argument")
        entity_id = _payload_str(payload, "entity_id", "add_argument")
        role = _payload_str(payload, "role", "add_argument")
        doc, trigger = self._find_node(sample, trigger_id, "add_argument")
        entity_doc, entity = self._find_node(sample, entity_id, "add_argument")
        if trigger.kind != "trigger":
            fail(
                ErrorCode.DANGLING_REF,
                "argument target must be a trigger",
                path="$.edit.add_argument.trigger_id",
                stage="edit",
                version="v2",
                annotation_id=trigger_id,
            )
        if entity.kind != "entity":
            fail(
                ErrorCode.DANGLING_REF,
                "argument source must be an entity",
                path="$.edit.add_argument.entity_id",
                stage="edit",
                version="v2",
                annotation_id=entity_id,
            )
        if entity_doc.doc_id != doc.doc_id:
            fail(
                ErrorCode.DANGLING_REF,
                "argument entity and trigger must belong to the same document",
                path="$.edit.add_argument.entity_id",
                stage="edit",
                version="v2",
                annotation_id=entity_id,
            )
        self.ontology.require_role(role, "$.edit.add_argument.role")
        edge_id = payload.get("id") or f"arg:{doc.doc_id}:{trigger_id}:{entity_id}:{len(doc.edges) + 1}"
        if not isinstance(edge_id, str) or not edge_id:
            fail(
                ErrorCode.SCHEMA_TYPE,
                "argument id must be a non-empty string",
                path="$.edit.add_argument.id",
                stage="edit",
                version="v2",
            )
        if any(edge.id == edge_id for _, edge in sample.iter_edges()):
            fail(
                ErrorCode.DUP_ID,
                f"edge id {edge_id!r} already exists",
                path="$.edit.add_argument.id",
                stage="edit",
                version="v2",
                annotation_id=edge_id,
            )
        doc.edges.append(
            Edge(
                id=edge_id,
                kind="argument",
                source=entity_id,
                target=trigger_id,
                role=role,
                event_id=trigger.event_id,
                confidence=_confidence_from_payload(payload, "add_argument"),
            )
        )

    def _edit_delete_argument(self, sample: V2Sample, payload: dict[str, Any]) -> None:
        edge_id = _payload_str(payload, "argument_id", "delete_argument")
        document, edge = self._find_edge(sample, edge_id, "delete_argument")
        if edge.kind != "argument":
            fail(
                ErrorCode.DANGLING_REF,
                "delete_argument target is not an argument edge",
                path="$.edit.delete_argument.argument_id",
                stage="edit",
                version="v2",
                annotation_id=edge_id,
            )
        document.edges.remove(edge)

    def _edit_change_argument_role(
        self, sample: V2Sample, payload: dict[str, Any]
    ) -> None:
        edge_id = _payload_str(payload, "argument_id", "change_argument_role")
        role = _payload_str(payload, "role", "change_argument_role")
        _, edge = self._find_edge(sample, edge_id, "change_argument_role")
        if edge.kind != "argument":
            fail(
                ErrorCode.DANGLING_REF,
                "role edit target is not an argument edge",
                path="$.edit.change_argument_role.argument_id",
                stage="edit",
                version="v2",
                annotation_id=edge_id,
            )
        self.ontology.require_role(role, "$.edit.change_argument_role.role", edge_id)
        edge.role = role

    def _event_triggers(self, sample: V2Sample, event_id: str) -> list[Node]:
        return [
            node
            for _, node in sample.iter_nodes()
            if node.kind == "trigger" and node.event_id == event_id
        ]

    def _event_ids(self, sample: V2Sample) -> set[str]:
        return {
            node.event_id
            for _, node in sample.iter_nodes()
            if node.kind == "trigger" and node.event_id
        }

    def _reassign_argument_events(
        self, sample: V2Sample, trigger_ids: set[str], event_id: str
    ) -> None:
        for _, edge in sample.iter_edges():
            if edge.kind == "argument" and edge.target in trigger_ids:
                edge.event_id = event_id

    def _edit_split_event(self, sample: V2Sample, payload: dict[str, Any]) -> None:
        event_id = _payload_str(payload, "event_id", "split_event")
        new_event_id = _payload_str(payload, "new_event_id", "split_event")
        raw_trigger_ids = payload.get("trigger_ids")
        if not isinstance(raw_trigger_ids, list) or not raw_trigger_ids:
            fail(
                ErrorCode.SCHEMA_MISSING,
                "split_event requires a non-empty trigger_ids list",
                path="$.edit.split_event.trigger_ids",
                stage="edit",
                version="v2",
            )
        trigger_ids = set()
        for trigger_id in raw_trigger_ids:
            if not isinstance(trigger_id, str) or not trigger_id:
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    "trigger_ids must contain non-empty strings",
                    path="$.edit.split_event.trigger_ids",
                    stage="edit",
                    version="v2",
                )
            trigger_ids.add(trigger_id)
        owned = self._event_triggers(sample, event_id)
        owned_ids = {node.id for node in owned}
        if not trigger_ids.issubset(owned_ids):
            fail(
                ErrorCode.TRIGGER_CONFLICT,
                "split subset contains a trigger not owned by the event",
                path="$.edit.split_event.trigger_ids",
                stage="edit",
                version="v2",
                annotation_id=event_id,
                details={
                    "not_owned": sorted(trigger_ids - owned_ids),
                    "event_triggers": sorted(owned_ids),
                },
            )
        if trigger_ids == owned_ids:
            fail(
                ErrorCode.TRIGGER_CONFLICT,
                "split must leave at least one trigger in the source event",
                path="$.edit.split_event.trigger_ids",
                stage="edit",
                version="v2",
                annotation_id=event_id,
            )
        if new_event_id in self._event_ids(sample):
            fail(
                ErrorCode.TRIGGER_CONFLICT,
                f"event id {new_event_id!r} already exists",
                path="$.edit.split_event.new_event_id",
                stage="edit",
                version="v2",
                annotation_id=new_event_id,
            )
        for node in owned:
            if node.id in trigger_ids:
                node.event_id = new_event_id
        self._reassign_argument_events(sample, trigger_ids, new_event_id)

    def _edit_merge_events(self, sample: V2Sample, payload: dict[str, Any]) -> None:
        raw_event_ids = payload.get("event_ids")
        new_event_id = _payload_str(payload, "new_event_id", "merge_events")
        if not isinstance(raw_event_ids, list) or len(raw_event_ids) < 2:
            fail(
                ErrorCode.SCHEMA_MISSING,
                "merge_events requires at least two event_ids",
                path="$.edit.merge_events.event_ids",
                stage="edit",
                version="v2",
            )
        event_ids: list[str] = []
        for event_id in raw_event_ids:
            if not isinstance(event_id, str) or not event_id:
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    "event_ids must contain non-empty strings",
                    path="$.edit.merge_events.event_ids",
                    stage="edit",
                    version="v2",
                )
            event_ids.append(event_id)
        if len(set(event_ids)) != len(event_ids):
            fail(
                ErrorCode.DUP_ID,
                "merge_events contains duplicate event ids",
                path="$.edit.merge_events.event_ids",
                stage="edit",
                version="v2",
            )
        active_event_ids = self._event_ids(sample)
        missing = sorted(set(event_ids) - active_event_ids)
        if missing:
            fail(
                ErrorCode.TRIGGER_CONFLICT,
                "merge references an event with no owned trigger",
                path="$.edit.merge_events.event_ids",
                stage="edit",
                version="v2",
                details={"missing": missing},
            )
        if new_event_id in active_event_ids:
            fail(
                ErrorCode.TRIGGER_CONFLICT,
                f"event id {new_event_id!r} already exists",
                path="$.edit.merge_events.new_event_id",
                stage="edit",
                version="v2",
                annotation_id=new_event_id,
            )
        trigger_ids: set[str] = set()
        for event_id in event_ids:
            trigger_ids.update(
                node.id for node in self._event_triggers(sample, event_id)
            )
        for node in (node for _, node in sample.iter_nodes() if node.id in trigger_ids):
            node.event_id = new_event_id
        self._reassign_argument_events(sample, trigger_ids, new_event_id)

