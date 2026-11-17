"""Fixed-width v1 relation/event exchange parser and serializer."""

from __future__ import annotations

import copy
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .artifacts import SerializationResult
from .config import (
    all_roles,
    event_roles,
    seconds_to_v1_units_exact,
    v1_units_to_seconds,
)
from .errors import (
    ArgumentDanglingError,
    ConfidenceRangeError,
    DocumentUnknownError,
    DuplicateIdError,
    FormatOverlongError,
    FormatTruncatedError,
    RoleUnknownError,
    SpanOrderError,
)
from .jsonutil import dumps_canonical
from .model import (
    Argument,
    Document,
    Event,
    Failure,
    LossItem,
    ProjectState,
    Relation,
    Span,
    decimal_str,
)
from .parallel import Task, execute_ordered


V1_FIELDS: tuple[tuple[str, int], ...] = (
    ("kind", 1),
    ("doc_id", 16),
    ("entry_id", 20),
    ("relation_type", 20),
    ("head_start", 8),
    ("head_end", 8),
    ("tail_start", 8),
    ("tail_end", 8),
    ("confidence_permille", 4),
    ("trigger", 20),
    ("event_type", 20),
    ("arg_count", 2),
)
V1_ARG_FIELDS: tuple[tuple[str, int], ...] = (
    ("role", 24),
    ("start", 8),
    ("end", 8),
    ("source_relation", 20),
    ("entity_type", 8),
)

V1_BASE_WIDTH = sum(width for _, width in V1_FIELDS)
V1_ARG_WIDTH = sum(width for _, width in V1_ARG_FIELDS)


def _offsets(fields: tuple[tuple[str, int], ...]) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    cursor = 0
    for name, width in fields:
        result[name] = (cursor, width)
        cursor += width
    return result


V1_OFFSETS = _offsets(V1_FIELDS)
V1_ARG_OFFSETS = _offsets(V1_ARG_FIELDS)


def _field(raw: bytes, offsets: dict[str, tuple[int, int]], name: str) -> bytes:
    offset, width = offsets[name]
    return raw[offset : offset + width]


def _text(raw: bytes, offsets: dict[str, tuple[int, int]], name: str) -> str:
    try:
        return _field(raw, offsets, name).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise FormatTruncatedError(f"field {name} is not valid UTF-8") from exc


def _integer(raw: bytes, offsets: dict[str, tuple[int, int]], name: str) -> int:
    try:
        text = _field(raw, offsets, name).decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise FormatTruncatedError(f"field {name} is not an ASCII integer") from exc
    if not text or not text.isdigit():
        raise FormatTruncatedError(f"field {name} is not an unsigned integer")
    return int(text)


def _attempt_ids(raw: bytes) -> tuple[str, str | None]:
    doc_id = ""
    entry_id: str | None = None
    if len(raw) >= V1_OFFSETS["doc_id"][0] + V1_OFFSETS["doc_id"][1]:
        doc_id = _field(raw, V1_OFFSETS, "doc_id").decode("utf-8", errors="replace").strip()
    if len(raw) >= V1_OFFSETS["entry_id"][0] + V1_OFFSETS["entry_id"][1]:
        entry_id = _field(raw, V1_OFFSETS, "entry_id").decode("utf-8", errors="replace").strip() or None
    return doc_id, entry_id


def _parse_line(raw: bytes, source_index: int, config: dict[str, Any]) -> dict[str, Any]:
    if len(raw) < V1_BASE_WIDTH:
        raise FormatTruncatedError(
            f"line is shorter than fixed-width header ({len(raw)} < {V1_BASE_WIDTH})"
        )
    try:
        kind = _field(raw, V1_OFFSETS, "kind").decode("ascii")
    except UnicodeDecodeError as exc:
        raise FormatTruncatedError("kind field is not ASCII") from exc
    if kind not in ("R", "E"):
        raise FormatTruncatedError(f"unknown record kind {kind!r}")
    arg_count = _integer(raw, V1_OFFSETS, "arg_count")
    expected = V1_BASE_WIDTH + arg_count * V1_ARG_WIDTH
    if len(raw) < expected:
        raise FormatTruncatedError(f"record is truncated ({len(raw)} < {expected})")
    if len(raw) > expected:
        raise FormatOverlongError(f"record has trailing bytes ({len(raw)} > {expected})")

    values = {name: _field(raw, V1_OFFSETS, name) for name, _ in V1_FIELDS}
    text_values = {name: value.decode("utf-8", errors="replace").strip() for name, value in values.items()}
    head_start = _integer(raw, V1_OFFSETS, "head_start")
    head_end = _integer(raw, V1_OFFSETS, "head_end")
    tail_start = _integer(raw, V1_OFFSETS, "tail_start")
    tail_end = _integer(raw, V1_OFFSETS, "tail_end")
    confidence_permille = _integer(raw, V1_OFFSETS, "confidence_permille")
    if confidence_permille > 1000:
        raise ConfidenceRangeError(f"v1 confidence permille {confidence_permille} is outside 0..1000")
    if head_start > head_end or tail_start > tail_end:
        raise SpanOrderError("v1 span start is greater than its end")

    arguments: list[dict[str, Any]] = []
    for arg_index in range(arg_count):
        start = V1_BASE_WIDTH + arg_index * V1_ARG_WIDTH
        segment = raw[start : start + V1_ARG_WIDTH]
        role = _text(segment, V1_ARG_OFFSETS, "role")
        arg_start = _integer(segment, V1_ARG_OFFSETS, "start")
        arg_end = _integer(segment, V1_ARG_OFFSETS, "end")
        source_relation = _text(segment, V1_ARG_OFFSETS, "source_relation") or None
        entity_type = _text(segment, V1_ARG_OFFSETS, "entity_type") or None
        if arg_start > arg_end:
            raise SpanOrderError(f"argument {arg_index} start is greater than its end")
        arguments.append(
            {
                "role": role,
                "start": arg_start,
                "end": arg_end,
                "source_relation": source_relation,
                "entity_type": entity_type,
                "raw_fields": {
                    name: segment[offset : offset + width].decode("utf-8", errors="replace")
                    for name, (offset, width) in V1_ARG_OFFSETS.items()
                },
            }
        )
    return {
        "kind": kind,
        "source_index": source_index,
        "doc_id": text_values["doc_id"],
        "entry_id": text_values["entry_id"],
        "relation_type": text_values["relation_type"],
        "event_type": text_values["event_type"],
        "trigger": text_values["trigger"],
        "head_start": head_start,
        "head_end": head_end,
        "tail_start": tail_start,
        "tail_end": tail_end,
        "confidence_permille": confidence_permille,
        "arguments": arguments,
        "raw_fields": {
            **{name: value.decode("utf-8", errors="replace") for name, value in values.items()},
            "arguments": [argument["raw_fields"] for argument in arguments],
        },
    }


def _parse_document_lines(
    doc: Document,
    lines: list[tuple[int, bytes]],
    config: dict[str, Any],
) -> tuple[Document, list[Failure]]:
    result = copy.deepcopy(doc)
    result.relations = []
    result.events = []
    failures: list[Failure] = []
    candidates: list[dict[str, Any]] = []
    for source_index, raw in lines:
        try:
            candidate = _parse_line(raw, source_index, config)
            candidates.append(candidate)
        except BaseException as exc:
            _, entry_id = _attempt_ids(raw)
            failures.append(
                Failure.from_exception(
                    exc,
                    stage="parse:v1",
                    doc_id=doc.doc_id,
                    entry_index=source_index,
                    entry_id=entry_id,
                )
            )

    valid_relation_ids: set[str] = set()
    seen_relation_ids: set[str] = set()
    seen_event_ids: set[str] = set()
    for candidate in candidates:
        if candidate["kind"] != "R":
            continue
        entry_id = candidate["entry_id"]
        if entry_id in seen_relation_ids:
            failures.append(
                Failure(
                    business_code=DuplicateIdError.code,
                    stage="parse:v1",
                    exception_type=DuplicateIdError.__name__,
                    message=f"duplicate relation entry id {entry_id!r}",
                    doc_id=doc.doc_id,
                    entry_index=candidate["source_index"],
                    entry_id=entry_id,
                )
            )
            continue
        seen_relation_ids.add(entry_id)
        valid_relation_ids.add(entry_id)
        try:
            head = Span(
                v1_units_to_seconds(candidate["head_start"], config),
                v1_units_to_seconds(candidate["head_end"], config),
            )
            tail = Span(
                v1_units_to_seconds(candidate["tail_start"], config),
                v1_units_to_seconds(candidate["tail_end"], config),
            )
            if head.start < 0 or tail.start < 0:
                raise SpanOrderError("v1 relation contains a negative converted offset")
            if not candidate["entry_id"] or not candidate["relation_type"]:
                raise FormatTruncatedError("v1 relation requires entry_id and relation_type")
            result.relations.append(
                Relation(
                    entry_id=candidate["entry_id"],
                    relation_type=candidate["relation_type"],
                    head=head,
                    tail=tail,
                    confidence=Decimal(candidate["confidence_permille"]) / Decimal(1000),
                    trigger=candidate["trigger"],
                    source_version="v1",
                    source_index=candidate["source_index"],
                    original_confidence={
                        "representation": "permille",
                        "value": candidate["confidence_permille"],
                    },
                    raw_fields=candidate["raw_fields"],
                )
            )
        except BaseException as exc:
            failures.append(
                Failure.from_exception(
                    exc,
                    stage="parse:v1",
                    doc_id=doc.doc_id,
                    entry_index=candidate["source_index"],
                    entry_id=candidate["entry_id"] or None,
                )
            )

    known_roles = all_roles(config)
    for candidate in candidates:
        if candidate["kind"] != "E":
            continue
        entry_id = candidate["entry_id"]
        if entry_id in seen_event_ids:
            failures.append(
                Failure(
                    business_code=DuplicateIdError.code,
                    stage="parse:v1",
                    exception_type=DuplicateIdError.__name__,
                    message=f"duplicate event entry id {entry_id!r}",
                    doc_id=doc.doc_id,
                    entry_index=candidate["source_index"],
                    entry_id=entry_id,
                )
            )
            continue
        seen_event_ids.add(entry_id)
        try:
            event_type = candidate["event_type"] or candidate["relation_type"]
            if not event_type:
                raise FormatTruncatedError("v1 event requires an event_type")
            span = Span(
                v1_units_to_seconds(candidate["head_start"], config),
                v1_units_to_seconds(candidate["head_end"], config),
            )
            if span.start < 0:
                raise SpanOrderError("v1 event contains a negative converted offset")
            if not candidate["entry_id"]:
                raise FormatTruncatedError("v1 event requires an entry_id")
            role_table = set(event_roles(config, event_type))
            arguments: list[Argument] = []
            for arg_index, item in enumerate(candidate["arguments"]):
                arg_span = Span(
                    v1_units_to_seconds(item["start"], config),
                    v1_units_to_seconds(item["end"], config),
                )
                if not span.contains(arg_span):
                    raise SpanOrderError(
                        f"argument {arg_index} lies outside its event span"
                    )
                role = item["role"]
                if not role or role not in role_table or role not in known_roles:
                    raise RoleUnknownError(
                        f"role {role!r} is not valid for event type {event_type!r}"
                    )
                source_relation = item["source_relation"]
                if source_relation and source_relation not in valid_relation_ids:
                    raise ArgumentDanglingError(
                        f"argument {arg_index} references missing relation {source_relation!r}"
                    )
                arguments.append(
                    Argument(
                        role=role,
                        role_key=None,
                        span=arg_span,
                        source_relation=source_relation,
                        entity_type=item["entity_type"],
                        source_index=arg_index,
                        raw_fields=item["raw_fields"],
                    )
                )
            result.events.append(
                Event(
                    entry_id=candidate["entry_id"],
                    event_type=event_type,
                    span=span,
                    confidence=Decimal(candidate["confidence_permille"]) / Decimal(1000),
                    trigger=candidate["trigger"],
                    arguments=arguments,
                    source_version="v1",
                    source_index=candidate["source_index"],
                    original_confidence={
                        "representation": "permille",
                        "value": candidate["confidence_permille"],
                    },
                    raw_fields=candidate["raw_fields"],
                )
            )
        except BaseException as exc:
            failures.append(
                Failure.from_exception(
                    exc,
                    stage="parse:v1",
                    doc_id=doc.doc_id,
                    entry_index=candidate["source_index"],
                    entry_id=candidate["entry_id"] or None,
                )
            )
    result.relations.sort(key=lambda item: (item.head.start, item.tail.start, item.entry_id))
    result.events.sort(key=lambda item: (item.span.start, item.entry_id))
    failures.sort(key=Failure.sort_key)
    return result, failures


def parse_v1(
    path: str | Path,
    documents: dict[str, Document],
    config: dict[str, Any],
    max_workers: int,
) -> tuple[dict[str, Document], list[Failure]]:
    try:
        raw_lines = Path(path).read_bytes().splitlines()
    except OSError as exc:
        return {}, [Failure.from_exception(exc, stage="parse:v1")]
    groups: dict[str, list[tuple[int, bytes]]] = {}
    unassigned: list[tuple[int, bytes]] = []
    for index, original in enumerate(raw_lines):
        raw = original[:-1] if original.endswith(b"\r") else original
        if not raw:
            unassigned.append((index, raw))
            continue
        doc_id, _ = _attempt_ids(raw)
        if not doc_id:
            unassigned.append((index, raw))
        else:
            groups.setdefault(doc_id, []).append((index, raw))

    failures: list[Failure] = []
    for index, raw in unassigned:
        doc_id, entry_id = _attempt_ids(raw)
        try:
            _parse_line(raw, index, config)
        except BaseException as exc:
            failures.append(
                Failure.from_exception(
                    exc,
                    stage="parse:v1",
                    doc_id=doc_id,
                    entry_index=index,
                    entry_id=entry_id,
                )
            )

    tasks: list[Task] = []
    for doc_id, lines in sorted(groups.items()):
        if doc_id not in documents:
            for index, raw in lines:
                _, entry_id = _attempt_ids(raw)
                exc = DocumentUnknownError(f"v1 record references unknown document {doc_id!r}")
                failures.append(
                    Failure.from_exception(
                        exc,
                        stage="parse:v1",
                        doc_id=doc_id,
                        entry_index=index,
                        entry_id=entry_id,
                    )
                )
            continue

        def task_for(doc: Document = documents[doc_id], doc_lines=lines):
            return _parse_document_lines(doc, doc_lines, config)

        tasks.append(Task((doc_id, 0, "parse:v1"), task_for, "v1-document"))

    results, _ = execute_ordered(tasks, max_workers)
    parsed_docs: dict[str, Document] = {}
    for result in results:
        if result.exception is not None:
            doc_id = str(result.key[0])
            failures.append(
                Failure.from_exception(result.exception, stage="parse:v1", doc_id=doc_id)
            )
            continue
        document, item_failures = result.value
        parsed_docs[document.doc_id] = document
        failures.extend(item_failures)
    failures.sort(key=Failure.sort_key)
    return parsed_docs, failures


def _fit_bytes(value: Any, width: int) -> tuple[bytes, bool, str]:
    original = str(value)
    raw = original.encode("utf-8")
    if len(raw) <= width:
        return raw + b" " * (width - len(raw)), False, original
    cut = raw[:width]
    stored = cut.decode("utf-8", errors="ignore")
    encoded = stored.encode("utf-8")
    if len(encoded) > width:
        encoded = encoded[:width]
        stored = encoded.decode("utf-8", errors="ignore")
        encoded = stored.encode("utf-8")
    return encoded + b" " * (width - len(encoded)), True, stored


def _number_bytes(value: int, width: int, field_name: str) -> bytes:
    text = str(int(value))
    if len(text) > width or value < 0:
        raise FormatOverlongError(f"{field_name}={value} does not fit width {width}")
    return text.zfill(width).encode("ascii")


def _loss(
    doc_id: str,
    kind: str,
    entry_index: int,
    entry_id: str,
    field_name: str,
    category: str,
    detail: dict[str, Any],
    reversible: bool = False,
) -> LossItem:
    return LossItem(
        doc_id=doc_id,
        kind=kind,
        entry_index=entry_index,
        entry_id=entry_id,
        field_name=field_name,
        category=category,
        reversible=reversible,
        detail=detail,
    )


def _position_fields(span: Span, prefix: str, config: dict[str, Any]):
    start_units, start_delta = seconds_to_v1_units_exact(span.start, config)
    end_units, end_delta = seconds_to_v1_units_exact(span.end, config)
    return start_units, end_units, {f"{prefix}.start": start_delta, f"{prefix}.end": end_delta}


def _serialize_document(
    doc: Document,
    config: dict[str, Any],
) -> tuple[list[bytes], list[LossItem], list[Failure]]:
    lines: list[bytes] = []
    losses: list[LossItem] = []
    failures: list[Failure] = []

    def add_fit_loss(kind: str, index: int, entry_id: str, field: str, original: Any, stored: str, category: str):
        losses.append(
            _loss(
                doc.doc_id,
                kind,
                index,
                entry_id,
                field,
                category,
                {"original": str(original), "stored": stored, "width": dict(V1_FIELDS + V1_ARG_FIELDS)[field] if field in dict(V1_FIELDS + V1_ARG_FIELDS) else None},
            )
        )

    if doc.metadata:
        losses.append(
            _loss(
                doc.doc_id,
                "document",
                0,
                doc.doc_id,
                "metadata",
                "DOCUMENT_METADATA_DROPPED",
                {"metadata": doc.metadata},
            )
        )

    for index, relation in enumerate(doc.relations):
        try:
            if relation.confidence < 0 or relation.confidence > 1:
                raise ConfidenceRangeError("relation confidence is outside 0..1")
            head_start, head_end, head_delta = _position_fields(relation.head, "head", config)
            tail_start, tail_end, tail_delta = _position_fields(relation.tail, "tail", config)
            for field_name, delta in {**head_delta, **tail_delta}.items():
                if delta != 0:
                    losses.append(
                        _loss(
                            doc.doc_id,
                            "relation",
                            index,
                            relation.entry_id,
                            field_name,
                            "POSITION_QUANTIZED",
                            {"deviation_seconds": decimal_str(delta)},
                        )
                    )
            permille = int((relation.confidence * 1000).to_integral_value(rounding=ROUND_HALF_UP))
            restored_confidence = Decimal(permille) / Decimal(1000)
            if restored_confidence != relation.confidence:
                losses.append(
                    _loss(
                        doc.doc_id,
                        "relation",
                        index,
                        relation.entry_id,
                        "confidence",
                        "CONFIDENCE_QUANTIZED",
                        {
                            "original": decimal_str(relation.confidence),
                            "stored": decimal_str(restored_confidence),
                            "deviation": decimal_str(abs(restored_confidence - relation.confidence)),
                        },
                    )
                )

            encoded: dict[str, bytes] = {}
            for field_name, width in V1_FIELDS:
                if field_name == "kind":
                    encoded[field_name] = b"R"
                elif field_name == "doc_id":
                    encoded[field_name], truncated, stored = _fit_bytes(doc.doc_id, width)
                    if truncated:
                        add_fit_loss("relation", index, relation.entry_id, field_name, doc.doc_id, stored, "FIELD_TRUNCATED")
                elif field_name == "entry_id":
                    encoded[field_name], truncated, stored = _fit_bytes(relation.entry_id, width)
                    if truncated:
                        add_fit_loss("relation", index, relation.entry_id, field_name, relation.entry_id, stored, "FIELD_TRUNCATED")
                elif field_name == "relation_type":
                    encoded[field_name], truncated, stored = _fit_bytes(relation.relation_type, width)
                    if truncated:
                        add_fit_loss("relation", index, relation.entry_id, field_name, relation.relation_type, stored, "FIELD_TRUNCATED")
                elif field_name == "head_start":
                    encoded[field_name] = _number_bytes(head_start, width, field_name)
                elif field_name == "head_end":
                    encoded[field_name] = _number_bytes(head_end, width, field_name)
                elif field_name == "tail_start":
                    encoded[field_name] = _number_bytes(tail_start, width, field_name)
                elif field_name == "tail_end":
                    encoded[field_name] = _number_bytes(tail_end, width, field_name)
                elif field_name == "confidence_permille":
                    encoded[field_name] = _number_bytes(permille, width, field_name)
                elif field_name == "trigger":
                    encoded[field_name], truncated, stored = _fit_bytes(relation.trigger, width)
                    if truncated:
                        add_fit_loss("relation", index, relation.entry_id, field_name, relation.trigger, stored, "FIELD_TRUNCATED")
                elif field_name == "event_type":
                    encoded[field_name] = b" " * width
                elif field_name == "arg_count":
                    encoded[field_name] = _number_bytes(0, width, field_name)
            lines.append(b"".join(encoded[name] for name, _ in V1_FIELDS))
            if relation.components:
                losses.append(
                    _loss(
                        doc.doc_id,
                        "relation",
                        index,
                        relation.entry_id,
                        "components",
                        "MERGE_COMPONENTS_DROPPED",
                        {"component_ids": [component.entry_id for component in relation.components]},
                    )
                )
        except BaseException as exc:
            failures.append(
                Failure.from_exception(
                    exc, stage="convert:v1", doc_id=doc.doc_id, entry_index=index, entry_id=relation.entry_id
                )
            )

    for index, event in enumerate(doc.events):
        try:
            if event.confidence < 0 or event.confidence > 1:
                raise ConfidenceRangeError("event confidence is outside 0..1")
            start_units, end_units, deltas = _position_fields(event.span, "span", config)
            for field_name, delta in deltas.items():
                if delta != 0:
                    losses.append(
                        _loss(
                            doc.doc_id,
                            "event",
                            index,
                            event.entry_id,
                            field_name,
                            "POSITION_QUANTIZED",
                            {"deviation_seconds": decimal_str(delta)},
                        )
                    )
            permille = int((event.confidence * 1000).to_integral_value(rounding=ROUND_HALF_UP))
            restored_confidence = Decimal(permille) / Decimal(1000)
            if restored_confidence != event.confidence:
                losses.append(
                    _loss(
                        doc.doc_id,
                        "event",
                        index,
                        event.entry_id,
                        "confidence",
                        "CONFIDENCE_QUANTIZED",
                        {
                            "original": decimal_str(event.confidence),
                            "stored": decimal_str(restored_confidence),
                            "deviation": decimal_str(abs(restored_confidence - event.confidence)),
                        },
                    )
                )
            encoded: dict[str, bytes] = {}
            for field_name, width in V1_FIELDS:
                if field_name == "kind":
                    encoded[field_name] = b"E"
                elif field_name == "doc_id":
                    encoded[field_name], truncated, stored = _fit_bytes(doc.doc_id, width)
                    if truncated:
                        add_fit_loss("event", index, event.entry_id, field_name, doc.doc_id, stored, "FIELD_TRUNCATED")
                elif field_name == "entry_id":
                    encoded[field_name], truncated, stored = _fit_bytes(event.entry_id, width)
                    if truncated:
                        add_fit_loss("event", index, event.entry_id, field_name, event.entry_id, stored, "FIELD_TRUNCATED")
                elif field_name == "relation_type":
                    encoded[field_name], truncated, stored = _fit_bytes(event.event_type, width)
                    if truncated:
                        add_fit_loss("event", index, event.entry_id, field_name, event.event_type, stored, "FIELD_TRUNCATED")
                elif field_name == "head_start":
                    encoded[field_name] = _number_bytes(start_units, width, field_name)
                elif field_name == "head_end":
                    encoded[field_name] = _number_bytes(end_units, width, field_name)
                elif field_name in ("tail_start", "tail_end"):
                    encoded[field_name] = _number_bytes(0, width, field_name)
                elif field_name == "confidence_permille":
                    encoded[field_name] = _number_bytes(permille, width, field_name)
                elif field_name == "trigger":
                    encoded[field_name], truncated, stored = _fit_bytes(event.trigger, width)
                    if truncated:
                        add_fit_loss("event", index, event.entry_id, field_name, event.trigger, stored, "FIELD_TRUNCATED")
                elif field_name == "event_type":
                    encoded[field_name], truncated, stored = _fit_bytes(event.event_type, width)
                    if truncated:
                        add_fit_loss("event", index, event.entry_id, field_name, event.event_type, stored, "FIELD_TRUNCATED")
                elif field_name == "arg_count":
                    encoded[field_name] = _number_bytes(len(event.arguments), width, field_name)
            segments: list[bytes] = []
            for arg_index, argument in enumerate(event.arguments):
                arg_start, arg_end, arg_deltas = _position_fields(argument.span, f"arguments[{arg_index}]", config)
                for field_name, delta in arg_deltas.items():
                    if delta != 0:
                        losses.append(
                            _loss(
                                doc.doc_id,
                                "event",
                                index,
                                event.entry_id,
                                field_name,
                                "POSITION_QUANTIZED",
                                {"deviation_seconds": decimal_str(delta)},
                            )
                        )
                arg_encoded: dict[str, bytes] = {}
                for field_name, width in V1_ARG_FIELDS:
                    if field_name == "role":
                        arg_encoded[field_name], truncated, stored = _fit_bytes(argument.role, width)
                        if truncated:
                            add_fit_loss("event", index, event.entry_id, field_name, argument.role, stored, "ROLE_NAME_TRUNCATED")
                    elif field_name == "start":
                        arg_encoded[field_name] = _number_bytes(arg_start, width, field_name)
                    elif field_name == "end":
                        arg_encoded[field_name] = _number_bytes(arg_end, width, field_name)
                    elif field_name == "source_relation":
                        source = argument.source_relation or ""
                        arg_encoded[field_name], truncated, stored = _fit_bytes(source, width)
                        if truncated:
                            add_fit_loss("event", index, event.entry_id, field_name, source, stored, "FIELD_TRUNCATED")
                    elif field_name == "entity_type":
                        entity_type = argument.entity_type or ""
                        arg_encoded[field_name], truncated, stored = _fit_bytes(entity_type, width)
                        if truncated:
                            add_fit_loss("event", index, event.entry_id, field_name, entity_type, stored, "FIELD_TRUNCATED")
                segments.append(b"".join(arg_encoded[name] for name, _ in V1_ARG_FIELDS))
            lines.append(b"".join(encoded[name] for name, _ in V1_FIELDS) + b"".join(segments))
        except BaseException as exc:
            failures.append(
                Failure.from_exception(
                    exc, stage="convert:v1", doc_id=doc.doc_id, entry_index=index, entry_id=event.entry_id
                )
            )
    return lines, losses, failures


def serialize_v1(
    state: ProjectState,
    config: dict[str, Any],
    max_workers: int,
) -> SerializationResult:
    tasks = [
        Task((doc_id, 0, "convert:v1"), lambda doc=doc: _serialize_document(doc, config), "v1-document")
        for doc_id, doc in state.documents.items()
    ]
    results, _ = execute_ordered(tasks, max_workers)
    lines: list[bytes] = []
    losses: list[LossItem] = []
    failures: list[Failure] = []
    for result in results:
        if result.exception is not None:
            failures.append(
                Failure.from_exception(result.exception, stage="convert:v1", doc_id=str(result.key[0]))
            )
            continue
        doc_lines, doc_losses, doc_failures = result.value
        lines.extend(doc_lines)
        losses.extend(doc_losses)
        failures.extend(doc_failures)
    if state.role_names:
        losses.append(
            _loss(
                "",
                "project",
                0,
                "",
                "role_names",
                "ROLE_NAMING_TABLE_FLATTENED",
                {"role_names": state.role_names},
                reversible=True,
            )
        )
    if state.source_metadata:
        losses.append(
            _loss(
                "",
                "project",
                0,
                "",
                "source_metadata",
                "PROJECT_METADATA_DROPPED",
                {"metadata": state.source_metadata},
            )
        )
    losses.sort(key=LossItem.sort_key)
    failures.sort(key=Failure.sort_key)
    text = b"\n".join(lines) + (b"\n" if lines else b"")
    return SerializationResult(
        target_version="v1",
        text=text.decode("utf-8"),
        losses=losses,
        failures=failures,
    )
