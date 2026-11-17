"""Unified intermediate representation and deterministic state helpers."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .errors import E_INTERNAL
from .jsonutil import dumps_canonical, stable_hash


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric position or confidence")
    return Decimal(str(value))


def decimal_str(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class Span:
    start: Decimal
    end: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _decimal(self.start))
        object.__setattr__(self, "end", _decimal(self.end))

    def shifted(self, amount: Decimal) -> "Span":
        return Span(self.start + amount, self.end + amount)

    def union(self, other: "Span") -> "Span":
        return Span(min(self.start, other.start), max(self.end, other.end))

    def contains(self, other: "Span") -> bool:
        return self.start <= other.start and other.end <= self.end

    def to_dict(self) -> dict[str, Decimal]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Span":
        return cls(_decimal(value["start"]), _decimal(value["end"]))


@dataclass
class RelationComponent:
    entry_id: str
    relation_type: str
    head: Span
    tail: Span
    confidence: Decimal
    trigger: str
    source_version: str
    source_index: int
    original_confidence: dict[str, Any] = field(default_factory=dict)
    raw_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "relation_type": self.relation_type,
            "head": self.head.to_dict(),
            "tail": self.tail.to_dict(),
            "confidence": self.confidence,
            "trigger": self.trigger,
            "source_version": self.source_version,
            "source_index": self.source_index,
            "original_confidence": self.original_confidence,
            "raw_fields": self.raw_fields,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelationComponent":
        return cls(
            entry_id=str(value["entry_id"]),
            relation_type=str(value["relation_type"]),
            head=Span.from_dict(value["head"]),
            tail=Span.from_dict(value["tail"]),
            confidence=_decimal(value["confidence"]),
            trigger=str(value.get("trigger", "")),
            source_version=str(value.get("source_version", "")),
            source_index=int(value.get("source_index", 0)),
            original_confidence=dict(value.get("original_confidence", {})),
            raw_fields=dict(value.get("raw_fields", {})),
        )


@dataclass
class Relation:
    entry_id: str
    relation_type: str
    head: Span
    tail: Span
    confidence: Decimal
    trigger: str
    source_version: str
    source_index: int
    original_confidence: dict[str, Any] = field(default_factory=dict)
    raw_fields: dict[str, Any] = field(default_factory=dict)
    components: list[RelationComponent] = field(default_factory=list)

    @property
    def overall_span(self) -> Span:
        return self.head.union(self.tail)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "entry_id": self.entry_id,
            "relation_type": self.relation_type,
            "head": self.head.to_dict(),
            "tail": self.tail.to_dict(),
            "confidence": self.confidence,
            "trigger": self.trigger,
            "source_version": self.source_version,
            "source_index": self.source_index,
            "original_confidence": self.original_confidence,
            "raw_fields": self.raw_fields,
            "components": [component.to_dict() for component in self.components],
        }
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Relation":
        return cls(
            entry_id=str(value["entry_id"]),
            relation_type=str(value["relation_type"]),
            head=Span.from_dict(value["head"]),
            tail=Span.from_dict(value["tail"]),
            confidence=_decimal(value["confidence"]),
            trigger=str(value.get("trigger", "")),
            source_version=str(value.get("source_version", "")),
            source_index=int(value.get("source_index", 0)),
            original_confidence=dict(value.get("original_confidence", {})),
            raw_fields=dict(value.get("raw_fields", {})),
            components=[RelationComponent.from_dict(item) for item in value.get("components", [])],
        )

    def component(self) -> RelationComponent:
        return RelationComponent(
            entry_id=self.entry_id,
            relation_type=self.relation_type,
            head=self.head,
            tail=self.tail,
            confidence=self.confidence,
            trigger=self.trigger,
            source_version=self.source_version,
            source_index=self.source_index,
            original_confidence=copy.deepcopy(self.original_confidence),
            raw_fields=copy.deepcopy(self.raw_fields),
        )


@dataclass
class Argument:
    role: str
    role_key: str | None
    span: Span
    source_relation: str | None
    entity_type: str | None
    source_index: int
    raw_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "role_key": self.role_key,
            "span": self.span.to_dict(),
            "source_relation": self.source_relation,
            "entity_type": self.entity_type,
            "source_index": self.source_index,
            "raw_fields": self.raw_fields,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Argument":
        return cls(
            role=str(value["role"]),
            role_key=value.get("role_key"),
            span=Span.from_dict(value["span"]),
            source_relation=value.get("source_relation"),
            entity_type=value.get("entity_type"),
            source_index=int(value.get("source_index", 0)),
            raw_fields=dict(value.get("raw_fields", {})),
        )


@dataclass
class Event:
    entry_id: str
    event_type: str
    span: Span
    confidence: Decimal
    trigger: str
    arguments: list[Argument]
    source_version: str
    source_index: int
    original_confidence: dict[str, Any] = field(default_factory=dict)
    raw_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "event_type": self.event_type,
            "span": self.span.to_dict(),
            "confidence": self.confidence,
            "trigger": self.trigger,
            "arguments": [argument.to_dict() for argument in self.arguments],
            "source_version": self.source_version,
            "source_index": self.source_index,
            "original_confidence": self.original_confidence,
            "raw_fields": self.raw_fields,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        return cls(
            entry_id=str(value["entry_id"]),
            event_type=str(value["event_type"]),
            span=Span.from_dict(value["span"]),
            confidence=_decimal(value["confidence"]),
            trigger=str(value.get("trigger", "")),
            arguments=[Argument.from_dict(item) for item in value.get("arguments", [])],
            source_version=str(value.get("source_version", "")),
            source_index=int(value.get("source_index", 0)),
            original_confidence=dict(value.get("original_confidence", {})),
            raw_fields=dict(value.get("raw_fields", {})),
        )


@dataclass
class Document:
    doc_id: str
    tokens: list[str]
    token_positions: list[Span]
    metadata: dict[str, Any] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "tokens": self.tokens,
            "token_positions": [position.to_dict() for position in self.token_positions],
            "metadata": self.metadata,
            "relations": [relation.to_dict() for relation in self.relations],
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Document":
        return cls(
            doc_id=str(value["doc_id"]),
            tokens=[str(token) for token in value.get("tokens", [])],
            token_positions=[Span.from_dict(item) for item in value.get("token_positions", [])],
            metadata=dict(value.get("metadata", {})),
            relations=[Relation.from_dict(item) for item in value.get("relations", [])],
            events=[Event.from_dict(item) for item in value.get("events", [])],
        )


@dataclass
class LossItem:
    doc_id: str
    kind: str
    entry_index: int
    entry_id: str
    field_name: str
    category: str
    reversible: bool
    detail: dict[str, Any]

    def _payload(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "kind": self.kind,
            "entry_index": self.entry_index,
            "entry_id": self.entry_id,
            "field": self.field_name,
            "category": self.category,
            "reversible": self.reversible,
            "detail": self.detail,
        }

    @property
    def loss_id(self) -> str:
        return stable_hash(self._payload())[:16]

    def to_dict(self) -> dict[str, Any]:
        value = self._payload()
        value["loss_id"] = self.loss_id
        return value

    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.doc_id,
            self.entry_index,
            0 if self.kind == "relation" else 1 if self.kind == "event" else 2,
            self.entry_id,
            self.category,
            self.field_name,
            dumps_canonical(self.detail),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LossItem":
        return cls(
            doc_id=str(value["doc_id"]),
            kind=str(value["kind"]),
            entry_index=int(value["entry_index"]),
            entry_id=str(value["entry_id"]),
            field_name=str(value["field"]),
            category=str(value["category"]),
            reversible=bool(value["reversible"]),
            detail=dict(value.get("detail", {})),
        )


@dataclass
class Failure:
    business_code: str
    stage: str
    exception_type: str
    message: str
    doc_id: str = ""
    entry_index: int = -1
    entry_id: str | None = None
    operation_index: int | None = None
    command_id: str | None = None
    detail_code: str | None = None
    injection_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.business_code,
            "stage": self.stage,
            "exception_type": self.exception_type,
            "message": self.message,
            "doc_id": self.doc_id,
            "entry_index": self.entry_index,
            "entry_id": self.entry_id,
            "operation_index": self.operation_index,
            "command_id": self.command_id,
            "detail_code": self.detail_code,
            "injection_id": self.injection_id,
        }

    def sort_key(self) -> tuple[Any, ...]:
        stage_order = {
            "parse:documents": 0,
            "parse:v1": 1,
            "parse:v2": 1,
            "load": 2,
            "edit": 3,
            "convert:v1": 4,
            "convert:v2": 4,
            "snapshot": 5,
            "report": 6,
        }
        return (
            self.doc_id,
            self.entry_index,
            stage_order.get(self.stage, 99),
            self.stage,
            self.business_code,
            self.operation_index if self.operation_index is not None else -1,
            self.exception_type,
            self.message,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Failure":
        return cls(
            business_code=str(value["business_code"]),
            stage=str(value["stage"]),
            exception_type=str(value["exception_type"]),
            message=str(value["message"]),
            doc_id=str(value.get("doc_id", "")),
            entry_index=int(value.get("entry_index", -1)),
            entry_id=value.get("entry_id"),
            operation_index=value.get("operation_index"),
            command_id=value.get("command_id"),
            detail_code=value.get("detail_code"),
            injection_id=value.get("injection_id"),
        )

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        stage: str,
        doc_id: str = "",
        entry_index: int = -1,
        entry_id: str | None = None,
        operation_index: int | None = None,
        command_id: str | None = None,
        injection_id: str | None = None,
    ) -> "Failure":
        return cls(
            business_code=getattr(exc, "code", E_INTERNAL),
            stage=stage,
            exception_type=type(exc).__name__,
            message=str(exc),
            doc_id=doc_id,
            entry_index=entry_index,
            entry_id=entry_id,
            operation_index=operation_index,
            command_id=command_id,
            detail_code=getattr(exc, "detail_code", None),
            injection_id=injection_id,
        )


@dataclass
class EditLog:
    operation_index: int
    command_id: str
    operation: str
    affected: list[dict[str, Any]]
    target_version: str
    global_version: int
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_index": self.operation_index,
            "command_id": self.command_id,
            "operation": self.operation,
            "affected": self.affected,
            "target_version": self.target_version,
            "global_version": self.global_version,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EditLog":
        return cls(
            operation_index=int(value["operation_index"]),
            command_id=str(value["command_id"]),
            operation=str(value["operation"]),
            affected=list(value.get("affected", [])),
            target_version=str(value["target_version"]),
            global_version=int(value["global_version"]),
            status=str(value.get("status", "ok")),
        )


@dataclass
class ProjectState:
    source_version: str
    source_fingerprint: str
    target_version: str
    global_version: int = 1
    last_operation_index: int = 0
    last_command_id: str | None = None
    documents: dict[str, Document] = field(default_factory=dict)
    rule_config: dict[str, Any] = field(default_factory=dict)
    role_names: dict[str, str] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    losses: list[LossItem] = field(default_factory=list)
    logs: list[EditLog] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def canonicalize(self) -> None:
        self.documents = {doc_id: self.documents[doc_id] for doc_id in sorted(self.documents)}
        for doc in self.documents.values():
            doc.relations.sort(key=lambda item: (item.head.start, item.tail.start, item.entry_id, item.source_index))
            doc.events.sort(key=lambda item: (item.span.start, item.entry_id, item.source_index))
            for event in doc.events:
                event.arguments.sort(key=lambda item: (item.source_index, item.span.start, item.role))
        self.losses = sorted(self.losses, key=LossItem.sort_key)
        self.logs = sorted(self.logs, key=lambda item: (item.operation_index, item.command_id))
        self.failures = sorted(self.failures, key=Failure.sort_key)
        self.role_names = {key: self.role_names[key] for key in sorted(self.role_names)}

    def clone(self) -> "ProjectState":
        return copy.deepcopy(self)

    def ir_payload(self) -> dict[str, Any]:
        return {
            "documents": [self.documents[doc_id].to_dict() for doc_id in sorted(self.documents)],
            "rule_config": self.rule_config,
            "role_names": self.role_names,
            "source_metadata": self.source_metadata,
        }

    @property
    def ir_fingerprint(self) -> str:
        return stable_hash(self.ir_payload())

    @property
    def loss_hash(self) -> str:
        return stable_hash([loss.to_dict() for loss in self.losses])

    @property
    def degradation_fingerprint(self) -> str:
        return stable_hash(
            {
                "target_version": self.target_version,
                "ir_fingerprint": self.ir_fingerprint,
                "loss_hash": self.loss_hash,
            }
        )

    def entry_counts(self) -> dict[str, int]:
        return {
            "documents": len(self.documents),
            "relations": sum(len(doc.relations) for doc in self.documents.values()),
            "events": sum(len(doc.events) for doc in self.documents.values()),
            "arguments": sum(len(event.arguments) for doc in self.documents.values() for event in doc.events),
        }

    def to_dict(self) -> dict[str, Any]:
        self.canonicalize()
        return {
            "source_version": self.source_version,
            "source_fingerprint": self.source_fingerprint,
            "target_version": self.target_version,
            "global_version": self.global_version,
            "last_operation_index": self.last_operation_index,
            "last_command_id": self.last_command_id,
            "documents": [self.documents[doc_id].to_dict() for doc_id in sorted(self.documents)],
            "rule_config": self.rule_config,
            "role_names": self.role_names,
            "source_metadata": self.source_metadata,
            "losses": [loss.to_dict() for loss in self.losses],
            "logs": [log.to_dict() for log in self.logs],
            "failures": [failure.to_dict() for failure in self.failures],
            "warnings": self.warnings,
            "ir_fingerprint": self.ir_fingerprint,
            "loss_hash": self.loss_hash,
            "degradation_fingerprint": self.degradation_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectState":
        state = cls(
            source_version=str(value["source_version"]),
            source_fingerprint=str(value["source_fingerprint"]),
            target_version=str(value["target_version"]),
            global_version=int(value.get("global_version", 1)),
            last_operation_index=int(value.get("last_operation_index", 0)),
            last_command_id=value.get("last_command_id"),
            documents={
                str(doc["doc_id"]): Document.from_dict(doc)
                for doc in value.get("documents", [])
            },
            rule_config=dict(value.get("rule_config", {})),
            role_names=dict(value.get("role_names", {})),
            source_metadata=dict(value.get("source_metadata", {})),
            losses=[LossItem.from_dict(item) for item in value.get("losses", [])],
            logs=[EditLog.from_dict(item) for item in value.get("logs", [])],
            failures=[Failure.from_dict(item) for item in value.get("failures", [])],
            warnings=list(value.get("warnings", [])),
        )
        state.canonicalize()
        return state


def iter_relations(documents: dict[str, Document]) -> Iterable[tuple[str, int, Relation]]:
    for doc_id in sorted(documents):
        for index, relation in enumerate(documents[doc_id].relations):
            yield doc_id, index, relation


def iter_events(documents: dict[str, Document]) -> Iterable[tuple[str, int, Event]]:
    for doc_id in sorted(documents):
        for index, event in enumerate(documents[doc_id].events):
            yield doc_id, index, event
