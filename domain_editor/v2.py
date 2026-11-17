"""JSON v2 exchange parser and serializer."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

from .artifacts import SerializationResult
from .config import all_roles, event_roles, role_key
from .errors import (
    ArgumentDanglingError,
    ConfidenceRangeError,
    DocumentUnknownError,
    DuplicateIdError,
    JsonInvalidError,
    RoleUnknownError,
    SpanOrderError,
)
from .jsonutil import dumps_canonical, loads_decimal
from .model import (
    Argument,
    Document,
    Event,
    Failure,
    ProjectState,
    Relation,
    RelationComponent,
    Span,
    decimal_str,
)
from .parallel import Task, execute_ordered


def _require(value: dict[str, Any], key: str, where: str) -> Any:
    if key not in value:
        raise JsonInvalidError(f"{where} missing key {key!r}")
    return value[key]


def _as_time(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, float)):
        raise JsonInvalidError(f"{where} must be a number")
    result = Decimal(str(value))
    if not result.is_finite():
        raise JsonInvalidError(f"{where} must be finite")
    return result


def _as_span(value: Any, where: str) -> Span:
    if not isinstance(value, dict):
        raise JsonInvalidError(f"{where} must be an object")
    start = _as_time(_require(value, "start", where), f"{where}.start")
    end = _as_time(_require(value, "end", where), f"{where}.end")
    if start > end:
        raise SpanOrderError(f"{where} start is greater than end")
    return Span(start, end)


def _as_confidence(value: Any, where: str) -> Decimal:
    confidence = _as_time(value, where)
    if confidence < 0 or confidence > 1:
        raise ConfidenceRangeError(f"{where} is outside 0..1")
    return confidence


def _raw_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_raw_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _raw_scalar(item) for key, item in value.items()}
    return str(value)


def _parse_component(value: Any, where: str) -> RelationComponent:
    if not isinstance(value, dict):
        raise JsonInvalidError(f"{where} must be an object")
    return RelationComponent(
        entry_id=str(_require(value, "entry_id", where)),
        relation_type=str(_require(value, "relation_type", where)),
        head=_as_span(_require(value, "head", where), f"{where}.head"),
        tail=_as_span(_require(value, "tail", where), f"{where}.tail"),
        confidence=_as_confidence(_require(value, "confidence", where), f"{where}.confidence"),
        trigger=str(value.get("trigger", "")),
        source_version=str(value.get("source_version", "v2")),
        source_index=int(value.get("source_index", 0)),
        original_confidence=dict(value.get("original_confidence", {})),
        raw_fields=dict(value.get("raw_fields", {})),
    )


def _parse_document(
    source_doc: Any,
    doc_index: int,
    base_document: Document | None,
    role_names: dict[str, str],
    config: dict[str, Any],
) -> tuple[Document | None, list[Failure]]:
    failures: list[Failure] = []
    if not isinstance(source_doc, dict):
        failures.append(
            Failure(
                business_code=JsonInvalidError.code,
                stage="parse:v2",
                exception_type=JsonInvalidError.__name__,
                message=f"documents[{doc_index}] must be an object",
                entry_index=doc_index,
            )
        )
        return None, failures
    try:
        doc_id = str(_require(source_doc, "doc_id", f"documents[{doc_index}]"))
    except BaseException as exc:
        failures.append(
            Failure.from_exception(exc, stage="parse:v2", entry_index=doc_index)
        )
        return None, failures
    try:
        relations_raw = _require(source_doc, "relations", f"document {doc_id}")
        events_raw = _require(source_doc, "events", f"document {doc_id}")
        if not isinstance(relations_raw, list) or not isinstance(events_raw, list):
            raise JsonInvalidError("relations and events must be arrays")
    except BaseException as exc:
        failures.append(
            Failure.from_exception(exc, stage="parse:v2", doc_id=doc_id, entry_index=doc_index, entry_id=doc_id)
        )
        return None, failures
    if base_document is None:
        failures.append(
            Failure.from_exception(
                DocumentUnknownError(f"v2 project references unknown document {doc_id!r}"),
                stage="parse:v2",
                doc_id=doc_id,
                entry_index=doc_index,
                entry_id=doc_id,
            )
        )
        return None, failures
    metadata = source_doc.get("metadata", base_document.metadata)
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        failures.append(
            Failure.from_exception(
                JsonInvalidError("document metadata must be an object"),
                stage="parse:v2",
                doc_id=doc_id,
                entry_index=doc_index,
                entry_id=doc_id,
            )
        )
        metadata = {}
    document = copy.deepcopy(base_document)
    document.metadata = dict(metadata)
    document.relations = []
    document.events = []
    seen_relations: set[str] = set()
    valid_relation_ids: set[str] = set()
    for relation_index, item in enumerate(relations_raw):
        where = f"{doc_id}.relations[{relation_index}]"
        try:
            if not isinstance(item, dict):
                raise JsonInvalidError(f"{where} must be an object")
            entry_id = str(_require(item, "entry_id", where))
            if not entry_id:
                raise JsonInvalidError(f"{where}.entry_id must not be empty")
            if entry_id in seen_relations:
                raise DuplicateIdError(f"duplicate relation id {entry_id!r}")
            relation_type = str(_require(item, "relation_type", where))
            head = _as_span(_require(item, "head", where), f"{where}.head")
            tail = _as_span(_require(item, "tail", where), f"{where}.tail")
            confidence = _as_confidence(_require(item, "confidence", where), f"{where}.confidence")
            trigger = str(item.get("trigger", ""))
            components = []
            for component_index, component_value in enumerate(item.get("components", [])):
                components.append(
                    _parse_component(component_value, f"{where}.components[{component_index}]")
                )
            document.relations.append(
                Relation(
                    entry_id=entry_id,
                    relation_type=relation_type,
                    head=head,
                    tail=tail,
                    confidence=confidence,
                    trigger=trigger,
                    source_version="v2",
                    source_index=relation_index,
                    original_confidence={
                        "representation": "float",
                        "value": decimal_str(confidence),
                    },
                    raw_fields=_raw_scalar(item),
                    components=components,
                )
            )
            seen_relations.add(entry_id)
            valid_relation_ids.add(entry_id)
        except BaseException as exc:
            entry_id = None
            if isinstance(item, dict):
                entry_id = str(item.get("entry_id", "")) or None
            failures.append(
                Failure.from_exception(
                    exc,
                    stage="parse:v2",
                    doc_id=doc_id,
                    entry_index=relation_index,
                    entry_id=entry_id,
                )
            )

    known_roles = all_roles(config)
    seen_events: set[str] = set()
    for event_index, item in enumerate(events_raw):
        where = f"{doc_id}.events[{event_index}]"
        entry_id: str | None = None
        try:
            if not isinstance(item, dict):
                raise JsonInvalidError(f"{where} must be an object")
            entry_id = str(_require(item, "entry_id", where))
            if not entry_id:
                raise JsonInvalidError(f"{where}.entry_id must not be empty")
            if entry_id in seen_events:
                raise DuplicateIdError(f"duplicate event id {entry_id!r}")
            event_type = str(_require(item, "event_type", where))
            span = _as_span(_require(item, "span", where), f"{where}.span")
            confidence = _as_confidence(_require(item, "confidence", where), f"{where}.confidence")
            trigger = str(item.get("trigger", ""))
            args_raw = _require(item, "arguments", where)
            if not isinstance(args_raw, list):
                raise JsonInvalidError(f"{where}.arguments must be an array")
            role_table = set(event_roles(config, event_type))
            arguments: list[Argument] = []
            for arg_index, arg in enumerate(args_raw):
                arg_where = f"{where}.arguments[{arg_index}]"
                if not isinstance(arg, dict):
                    raise JsonInvalidError(f"{arg_where} must be an object")
                key = str(_require(arg, "role", arg_where))
                if key not in role_names:
                    raise RoleUnknownError(f"role key {key!r} is absent from role_names")
                name = role_names[key]
                if name not in known_roles or name not in role_table:
                    raise RoleUnknownError(
                        f"role {name!r} is not valid for event type {event_type!r}"
                    )
                arg_span = Span(
                    _as_time(_require(arg, "start", arg_where), f"{arg_where}.start"),
                    _as_time(_require(arg, "end", arg_where), f"{arg_where}.end"),
                )
                if arg_span.start > arg_span.end:
                    raise SpanOrderError(f"{arg_where} start is greater than end")
                if not span.contains(arg_span):
                    raise SpanOrderError(f"{arg_where} lies outside event span")
                source_relation = arg.get("source_relation")
                if source_relation is not None:
                    source_relation = str(source_relation)
                    if not source_relation:
                        source_relation = None
                if source_relation and source_relation not in valid_relation_ids:
                    raise ArgumentDanglingError(
                        f"{arg_where} references missing relation {source_relation!r}"
                    )
                entity_type = arg.get("entity_type")
                if entity_type is not None:
                    entity_type = str(entity_type)
                arguments.append(
                    Argument(
                        role=name,
                        role_key=key,
                        span=arg_span,
                        source_relation=source_relation,
                        entity_type=entity_type,
                        source_index=arg_index,
                        raw_fields=_raw_scalar(arg),
                    )
                )
            document.events.append(
                Event(
                    entry_id=entry_id,
                    event_type=event_type,
                    span=span,
                    confidence=confidence,
                    trigger=trigger,
                    arguments=arguments,
                    source_version="v2",
                    source_index=event_index,
                    original_confidence={
                        "representation": "float",
                        "value": decimal_str(confidence),
                    },
                    raw_fields=_raw_scalar(item),
                )
            )
            seen_events.add(entry_id)
        except BaseException as exc:
            if entry_id is None and isinstance(item, dict):
                entry_id = str(item.get("entry_id", "")) or None
            failures.append(
                Failure.from_exception(
                    exc,
                    stage="parse:v2",
                    doc_id=doc_id,
                    entry_index=event_index,
                    entry_id=entry_id,
                )
            )
    document.relations.sort(key=lambda entry: (entry.head.start, entry.tail.start, entry.entry_id))
    document.events.sort(key=lambda entry: (entry.span.start, entry.entry_id))
    failures.sort(key=Failure.sort_key)
    return document, failures


def parse_v2(
    path: str | Path,
    documents: dict[str, Document],
    config: dict[str, Any],
    max_workers: int,
) -> tuple[dict[str, Document], dict[str, str], dict[str, Any], list[Failure]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return {}, {}, {}, [Failure.from_exception(exc, stage="parse:v2")]
    try:
        root = loads_decimal(text)
    except BaseException as exc:
        wrapped = JsonInvalidError(f"invalid v2 JSON: {exc}")
        return {}, {}, {}, [Failure.from_exception(wrapped, stage="parse:v2")]
    if not isinstance(root, dict):
        wrapped = JsonInvalidError("v2 root must be an object")
        return {}, {}, {}, [Failure.from_exception(wrapped, stage="parse:v2")]
    try:
        if str(root.get("schema_version", "")) != "v2":
            raise JsonInvalidError("schema_version must be 'v2'")
        role_names_raw = _require(root, "role_names", "v2 root")
        documents_raw = _require(root, "documents", "v2 root")
        if not isinstance(role_names_raw, dict):
            raise JsonInvalidError("role_names must be an object")
        if not isinstance(documents_raw, list):
            raise JsonInvalidError("documents must be an array")
        role_names = {str(key): str(value) for key, value in role_names_raw.items()}
    except BaseException as exc:
        return {}, {}, {}, [Failure.from_exception(exc, stage="parse:v2")]
    source_metadata = root.get("metadata", {})
    if not isinstance(source_metadata, dict):
        source_metadata = {}
    tasks: list[Task] = []
    for doc_index, source_doc in enumerate(documents_raw):
        doc_id = ""
        if isinstance(source_doc, dict) and "doc_id" in source_doc:
            doc_id = str(source_doc["doc_id"])

        def task_for(value=source_doc, index=doc_index, candidate_doc_id=doc_id):
            return _parse_document(
                value,
                index,
                documents.get(candidate_doc_id),
                role_names,
                config,
            )

        tasks.append(Task((doc_id, doc_index, "parse:v2"), task_for, "v2-document"))
    results, _ = execute_ordered(tasks, max_workers)
    parsed: dict[str, Document] = {}
    failures: list[Failure] = []
    occurrences: dict[str, list[int]] = {}
    for result in results:
        if result.exception is not None:
            failures.append(
                Failure.from_exception(
                    result.exception,
                    stage="parse:v2",
                    doc_id=str(result.key[0]),
                    entry_index=int(result.key[1]),
                )
            )
            continue
        document, item_failures = result.value
        failures.extend(item_failures)
        if document is not None:
            occurrences.setdefault(document.doc_id, []).append(int(result.key[1]))
            if document.doc_id not in parsed:
                parsed[document.doc_id] = document
    for doc_id, indexes in occurrences.items():
        for duplicate_index in sorted(indexes)[1:]:
            failures.append(
                Failure(
                    business_code=DuplicateIdError.code,
                    stage="parse:v2",
                    exception_type=DuplicateIdError.__name__,
                    message=f"duplicate document id {doc_id!r}",
                    doc_id=doc_id,
                    entry_index=duplicate_index,
                    entry_id=doc_id,
                )
            )
    parsed = {doc_id: parsed[doc_id] for doc_id in sorted(parsed)}
    failures.sort(key=Failure.sort_key)
    return parsed, role_names, dict(source_metadata), failures


def _prepare_role_keys(
    state: ProjectState,
    config: dict[str, Any],
) -> tuple[dict[str, str], dict[tuple[str, str, int], str]]:
    table = dict(state.role_names)
    arg_keys: dict[tuple[str, str, int], str] = {}
    for doc_id in sorted(state.documents):
        for event in state.documents[doc_id].events:
            for index, argument in enumerate(event.arguments):
                candidate = argument.role_key
                if not candidate or table.get(candidate) != argument.role:
                    candidate = role_key(config, argument.role)
                existing = table.get(candidate)
                if existing is not None and existing != argument.role:
                    raise JsonInvalidError(
                        f"role key collision for {candidate!r}: {existing!r} vs {argument.role!r}"
                    )
                table[candidate] = argument.role
                arg_keys[(doc_id, event.entry_id, index)] = candidate
    return {key: table[key] for key in sorted(table)}, arg_keys


def _serialize_document(
    doc: Document,
    role_table: dict[str, str],
    arg_keys: dict[tuple[str, str, int], str],
) -> dict[str, Any]:
    relations: list[dict[str, Any]] = []
    for relation in doc.relations:
        value: dict[str, Any] = {
            "entry_id": relation.entry_id,
            "relation_type": relation.relation_type,
            "head": relation.head.to_dict(),
            "tail": relation.tail.to_dict(),
            "confidence": relation.confidence,
            "trigger": relation.trigger,
        }
        if relation.components:
            value["components"] = [component.to_dict() for component in relation.components]
        relations.append(value)
    events: list[dict[str, Any]] = []
    for event in doc.events:
        arguments: list[dict[str, Any]] = []
        for index, argument in enumerate(event.arguments):
            key = arg_keys[(doc.doc_id, event.entry_id, index)]
            value: dict[str, Any] = {
                "role": key,
                "start": argument.span.start,
                "end": argument.span.end,
                "source_relation": argument.source_relation,
            }
            if argument.entity_type is not None:
                value["entity_type"] = argument.entity_type
            arguments.append(value)
        events.append(
            {
                "entry_id": event.entry_id,
                "event_type": event.event_type,
                "span": event.span.to_dict(),
                "confidence": event.confidence,
                "trigger": event.trigger,
                "arguments": arguments,
            }
        )
    return {
        "doc_id": doc.doc_id,
        "metadata": doc.metadata,
        "relations": relations,
        "events": events,
    }


def build_v1_origin_sidecar(state: ProjectState, role_table: dict[str, str]) -> dict[str, Any] | None:
    if state.source_version != "v1":
        return None
    documents: list[dict[str, Any]] = []

    def raw_position(raw_fields: dict[str, Any], key: str) -> str | None:
        value = raw_fields.get(key)
        if value is None:
            return None
        return str(value).strip()

    for doc_id in sorted(state.documents):
        doc = state.documents[doc_id]
        relations = []
        for relation in doc.relations:
            relations.append(
                {
                    "entry_id": relation.entry_id,
                    "original_confidence": relation.original_confidence,
                    "original_positions": {
                        "head_start": raw_position(relation.raw_fields, "head_start"),
                        "head_end": raw_position(relation.raw_fields, "head_end"),
                        "tail_start": raw_position(relation.raw_fields, "tail_start"),
                        "tail_end": raw_position(relation.raw_fields, "tail_end"),
                    },
                    "raw_fields": relation.raw_fields,
                    "components": [component.to_dict() for component in relation.components],
                }
            )
        events = []
        for event in doc.events:
            events.append(
                {
                    "entry_id": event.entry_id,
                    "original_confidence": event.original_confidence,
                    "original_positions": {
                        "start": raw_position(event.raw_fields, "head_start"),
                        "end": raw_position(event.raw_fields, "head_end"),
                    },
                    "raw_fields": event.raw_fields,
                    "arguments": [
                        {
                            "source_index": argument.source_index,
                            "original_positions": {
                                "start": raw_position(argument.raw_fields, "start"),
                                "end": raw_position(argument.raw_fields, "end"),
                            },
                            "raw_fields": argument.raw_fields,
                            "source_relation": argument.source_relation,
                        }
                        for argument in event.arguments
                    ],
                }
            )
        documents.append({"doc_id": doc_id, "relations": relations, "events": events})
    return {
        "schema": "v1-origin-sidecar/1",
        "source_version": "v1",
        "source_fingerprint": state.source_fingerprint,
        "role_names": role_table,
        "documents": documents,
    }


def serialize_v2(
    state: ProjectState,
    config: dict[str, Any],
    max_workers: int,
) -> SerializationResult:
    try:
        role_table, arg_keys = _prepare_role_keys(state, config)
    except BaseException as exc:
        return SerializationResult(
            target_version="v2",
            text="",
            failures=[Failure.from_exception(exc, stage="convert:v2")],
        )
    tasks = [
        Task(
            (doc_id, 0, "convert:v2"),
            lambda doc=doc: _serialize_document(doc, role_table, arg_keys),
            "v2-document",
        )
        for doc_id, doc in state.documents.items()
    ]
    results, _ = execute_ordered(tasks, max_workers)
    documents: list[dict[str, Any]] = []
    failures: list[Failure] = []
    for result in results:
        if result.exception is not None:
            failures.append(
                Failure.from_exception(result.exception, stage="convert:v2", doc_id=str(result.key[0]))
            )
            continue
        documents.append(result.value)
    payload = {
        "schema_version": "v2",
        "role_names": role_table,
        "documents": documents,
        "metadata": state.source_metadata,
    }
    return SerializationResult(
        target_version="v2",
        text=dumps_canonical(payload, newline=True),
        failures=sorted(failures, key=Failure.sort_key),
        sidecar=build_v1_origin_sidecar(state, role_table),
    )
