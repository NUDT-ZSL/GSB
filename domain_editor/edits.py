"""Transactional relation/event edit operations."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from .config import (
    all_roles,
    document_duration_seconds,
    event_roles,
    role_key,
    units_per_second,
)
from .degradation import recompute_losses
from .errors import EditInvalidError
from .model import Argument, Document, Event, ProjectState, Relation, RelationComponent, Span, decimal_str
from .parallel import Task, execute_ordered


EDIT_TYPES = {
    "adjust_trigger",
    "merge_relations",
    "split_relation",
    "shift_offsets",
    "replace_argument_role",
}


def _invalid(message: str, detail_code: str | None = None) -> EditInvalidError:
    return EditInvalidError(message, detail_code=detail_code)


def _command_string(command: dict[str, Any], key: str) -> str:
    if key not in command or not isinstance(command[key], str) or not command[key]:
        raise _invalid(f"command missing non-empty string field {key!r}")
    return command[key]


def _duration(doc: Document, config: dict[str, Any]) -> Decimal:
    return document_duration_seconds(doc, config)


def _validate_document(doc: Document, config: dict[str, Any]) -> None:
    known_roles = all_roles(config)
    doc_id = doc.doc_id
    duration = _duration(doc, config)
    relation_ids: set[str] = set()
    for relation in doc.relations:
        if relation.entry_id in relation_ids:
            raise _invalid(f"duplicate relation id {relation.entry_id!r} in {doc_id}")
        relation_ids.add(relation.entry_id)
        if relation.head.start < 0 or relation.tail.start < 0:
            raise _invalid("relation position is negative", "E_SPAN_ORDER")
        if relation.head.start > relation.head.end or relation.tail.start > relation.tail.end:
            raise _invalid("relation span start is greater than end", "E_SPAN_ORDER")
        if relation.head.end > duration or relation.tail.end > duration:
            raise _invalid("relation lies outside document bounds", "E_POSITION_BOUNDS")
        if relation.confidence < 0 or relation.confidence > 1:
            raise _invalid("relation confidence is outside 0..1", "E_CONF_RANGE")
    event_ids: set[str] = set()
    for event in doc.events:
        if event.entry_id in event_ids:
            raise _invalid(f"duplicate event id {event.entry_id!r} in {doc_id}")
        event_ids.add(event.entry_id)
        if event.span.start < 0:
            raise _invalid("event position is negative", "E_SPAN_ORDER")
        if event.span.start > event.span.end:
            raise _invalid("event span start is greater than end", "E_SPAN_ORDER")
        if event.span.end > duration:
            raise _invalid("event lies outside document bounds", "E_POSITION_BOUNDS")
        if event.confidence < 0 or event.confidence > 1:
            raise _invalid("event confidence is outside 0..1", "E_CONF_RANGE")
        valid_roles = set(event_roles(config, event.event_type))
        for index, argument in enumerate(event.arguments):
            if argument.role not in known_roles or argument.role not in valid_roles:
                raise _invalid(
                    f"argument {index} role {argument.role!r} is not in the event role table",
                    "E_ROLE_UNKNOWN",
                )
            if argument.span.start > argument.span.end:
                raise _invalid(f"argument {index} span is reversed", "E_SPAN_ORDER")
            if not event.span.contains(argument.span):
                raise _invalid(f"argument {index} lies outside its event span", "E_SPAN_ORDER")
            if argument.source_relation and argument.source_relation not in relation_ids:
                raise _invalid(
                    f"argument {index} references missing relation {argument.source_relation!r}",
                    "E_ARG_DANGLING",
                )


def validate_documents(
    documents: dict[str, Document],
    config: dict[str, Any],
    max_workers: int = 1,
) -> None:
    """Validate each document independently, then fail in stable document order."""

    tasks = [
        Task((doc_id, 0, "edit-impact"), lambda doc=doc: _validate_document(doc, config), "validation")
        for doc_id, doc in documents.items()
    ]
    results, _ = execute_ordered(tasks, max_workers)
    for result in results:
        if result.exception is not None:
            if isinstance(result.exception, EditInvalidError):
                raise result.exception
            raise _invalid(f"post-edit validation failed: {result.exception}") from result.exception


def _relation_from_component(component: RelationComponent) -> Relation:
    return Relation(
        entry_id=component.entry_id,
        relation_type=component.relation_type,
        head=component.head,
        tail=component.tail,
        confidence=component.confidence,
        trigger=component.trigger,
        source_version=component.source_version,
        source_index=component.source_index,
        original_confidence=copy.deepcopy(component.original_confidence),
        raw_fields=copy.deepcopy(component.raw_fields),
    )


def _component_for(relation: Relation) -> RelationComponent:
    return relation.component()


def _synthesize_relation(
    components: list[RelationComponent],
    preferred_id: str,
) -> Relation:
    ordered = sorted(components, key=lambda item: (item.head.start, item.tail.start, item.entry_id))
    first = ordered[0]
    return Relation(
        entry_id=preferred_id,
        relation_type=first.relation_type,
        head=ordered[0].head.union(ordered[-1].head),
        tail=ordered[0].tail.union(ordered[-1].tail),
        confidence=min(component.confidence for component in ordered),
        trigger=first.trigger,
        source_version=first.source_version,
        source_index=min(component.source_index for component in ordered),
        original_confidence={
            "representation": "merged",
            "components": [component.original_confidence for component in ordered],
        },
        raw_fields={"merged_component_ids": [component.entry_id for component in ordered]},
        components=copy.deepcopy(ordered),
    )


def _unique_entry_id(existing: set[str], base: str) -> str:
    if base not in existing:
        return base
    suffix = 2
    while f"{base}__{suffix}" in existing:
        suffix += 1
    return f"{base}__{suffix}"


def _split_span(span: Span, boundary: Decimal) -> tuple[Span | None, Span | None]:
    if span.end <= boundary:
        return span, None
    if span.start >= boundary:
        return None, span
    return Span(span.start, boundary), Span(boundary, span.end)


def _fill_endpoint(span: Span | None, boundary: Decimal) -> Span:
    return span if span is not None else Span(boundary, boundary)


def _affected(doc_id: str, entries: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {"doc_id": doc_id, "kind": kind, "entry_id": entry_id}
        for kind, entry_id in sorted(set(entries), key=lambda item: (item[0], item[1]))
    ]


def _adjust_trigger(state: ProjectState, command: dict[str, Any]) -> list[dict[str, str]]:
    scope = _command_string(command, "scope")
    if scope not in ("relation", "event"):
        raise _invalid("adjust_trigger.scope must be relation or event")
    name = _command_string(command, "name")
    trigger = _command_string(command, "trigger")
    action = _command_string(command, "action")
    table_key = "relation_triggers" if scope == "relation" else "event_triggers"
    table = state.rule_config.get(table_key)
    if not isinstance(table, dict) or name not in table or not isinstance(table[name], list):
        raise _invalid(f"unknown {scope} trigger group {name!r}")
    values = [str(item) for item in table[name]]
    if action == "add":
        if trigger not in values:
            values.append(trigger)
    elif action == "remove":
        if trigger not in values:
            raise _invalid(f"trigger {trigger!r} is absent from {name!r}")
        values.remove(trigger)
    else:
        raise _invalid("adjust_trigger.action must be add or remove")
    table[name] = sorted(values)
    return [{"doc_id": "", "kind": "rule_config", "entry_id": f"{scope}:{name}:{trigger}"}]


def _merge_relations(
    state: ProjectState,
    command: dict[str, Any],
) -> tuple[list[dict[str, str]], str]:
    doc_id = _command_string(command, "doc_id")
    entry_ids = command.get("entry_ids")
    if not isinstance(entry_ids, list) or len(entry_ids) != 2 or len(set(entry_ids)) != 2:
        raise _invalid("merge_relations.entry_ids must contain two distinct ids")
    if doc_id not in state.documents:
        raise _invalid(f"unknown document {doc_id!r}")
    doc = state.documents[doc_id]
    by_id = {relation.entry_id: relation for relation in doc.relations}
    left_id, right_id = str(entry_ids[0]), str(entry_ids[1])
    if left_id not in by_id or right_id not in by_id:
        raise _invalid("merge_relations references a missing relation")
    left = by_id[left_id]
    right = by_id[right_id]
    if left.relation_type != right.relation_type:
        raise _invalid("merge_relations requires relations of the same type")
    components: list[RelationComponent] = []
    for relation in (left, right):
        if relation.components:
            components.extend(copy.deepcopy(relation.components))
        else:
            components.append(_component_for(relation))
    merged = _synthesize_relation(components, left_id)
    doc.relations = [relation for relation in doc.relations if relation.entry_id not in (left_id, right_id)]
    doc.relations.append(merged)
    affected: list[tuple[str, str]] = [("relation", left_id), ("relation", right_id)]
    for event in doc.events:
        for argument in event.arguments:
            if argument.source_relation in (left_id, right_id):
                argument.source_relation = left_id
                affected.append(("event", event.entry_id))
    return _affected(doc_id, affected), f"merged {right_id} into {left_id}"


def _split_relation(
    state: ProjectState,
    command: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, str]], str]:
    doc_id = _command_string(command, "doc_id")
    entry_id = _command_string(command, "entry_id")
    if doc_id not in state.documents:
        raise _invalid(f"unknown document {doc_id!r}")
    boundary_index = command.get("boundary_token_index")
    if isinstance(boundary_index, bool) or not isinstance(boundary_index, int):
        raise _invalid("split_relation.boundary_token_index must be an integer")
    doc = state.documents[doc_id]
    if boundary_index <= 0 or boundary_index >= len(doc.token_positions):
        raise _invalid("split boundary is not an interior token boundary")
    boundary = doc.token_positions[boundary_index].start / units_per_second(config)
    relation = next((item for item in doc.relations if item.entry_id == entry_id), None)
    if relation is None:
        raise _invalid(f"unknown relation {entry_id!r}")
    overall = relation.overall_span
    if not (overall.start < boundary < overall.end):
        raise _invalid("split boundary is outside the relation span")
    existing_ids = {item.entry_id for item in doc.relations if item.entry_id != entry_id}
    results: list[Relation]
    component_map: list[tuple[set[str], str]] = []
    if relation.components:
        left_components: list[RelationComponent] = []
        right_components: list[RelationComponent] = []
        for component in relation.components:
            component_span = component.head.union(component.tail)
            if component_span.start < boundary < component_span.end:
                raise _invalid("split boundary crosses a merged component", "E_COMPONENT_BOUNDARY")
            if component_span.end <= boundary:
                left_components.append(copy.deepcopy(component))
            else:
                right_components.append(copy.deepcopy(component))
        if not left_components or not right_components:
            raise _invalid("split would produce an empty relation side")
        if len(left_components) == 1:
            left_relation = _relation_from_component(left_components[0])
        else:
            left_relation = _synthesize_relation(
                left_components, _unique_entry_id(existing_ids, f"{entry_id}__part1")
            )
        right_preferred = (
            right_components[0].entry_id
            if len(right_components) == 1
            else _unique_entry_id(existing_ids | {left_relation.entry_id}, f"{entry_id}__part2")
        )
        if len(right_components) == 1:
            right_relation = _relation_from_component(right_components[0])
        else:
            right_relation = _synthesize_relation(right_components, right_preferred)
        if left_relation.entry_id == right_relation.entry_id:
            right_relation.entry_id = _unique_entry_id(
                existing_ids | {left_relation.entry_id}, f"{entry_id}__part2"
            )
        results = [left_relation, right_relation]
        component_map = [
            ({component.entry_id for component in left_components}, left_relation.entry_id),
            ({component.entry_id for component in right_components}, right_relation.entry_id),
        ]
    else:
        left_head, right_head = _split_span(relation.head, boundary)
        left_tail, right_tail = _split_span(relation.tail, boundary)
        left_relation = Relation(
            entry_id=relation.entry_id,
            relation_type=relation.relation_type,
            head=_fill_endpoint(left_head, boundary),
            tail=_fill_endpoint(left_tail, boundary),
            confidence=relation.confidence,
            trigger=relation.trigger,
            source_version=relation.source_version,
            source_index=relation.source_index,
            original_confidence=copy.deepcopy(relation.original_confidence),
            raw_fields=copy.deepcopy(relation.raw_fields),
        )
        right_id = _unique_entry_id(existing_ids | {relation.entry_id}, f"{entry_id}__split2")
        right_relation = Relation(
            entry_id=right_id,
            relation_type=relation.relation_type,
            head=_fill_endpoint(right_head, boundary),
            tail=_fill_endpoint(right_tail, boundary),
            confidence=relation.confidence,
            trigger=relation.trigger,
            source_version=relation.source_version,
            source_index=relation.source_index,
            original_confidence=copy.deepcopy(relation.original_confidence),
            raw_fields=copy.deepcopy(relation.raw_fields),
        )
        results = [left_relation, right_relation]
        component_map = [({entry_id}, left_relation.entry_id), (set(), right_relation.entry_id)]
    doc.relations = [item for item in doc.relations if item.entry_id != entry_id]
    doc.relations.extend(results)
    affected: list[tuple[str, str]] = [("relation", item.entry_id) for item in results]
    by_old_component = {component_id: target for ids, target in component_map for component_id in ids}
    for event in doc.events:
        for argument in event.arguments:
            if argument.source_relation == entry_id:
                target = results[0].entry_id if event.span.start < boundary else results[-1].entry_id
                argument.source_relation = target
                affected.append(("event", event.entry_id))
            elif argument.source_relation in by_old_component:
                argument.source_relation = by_old_component[argument.source_relation]
                affected.append(("event", event.entry_id))
    return _affected(doc_id, affected), f"split {entry_id} at token boundary {boundary_index}"


def _shift_offsets(
    state: ProjectState,
    command: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, str]], str]:
    doc_id = _command_string(command, "doc_id")
    if doc_id not in state.documents:
        raise _invalid(f"unknown document {doc_id!r}")
    if "amount_seconds" not in command:
        raise _invalid("shift_offsets requires amount_seconds")
    try:
        amount = Decimal(str(command["amount_seconds"]))
    except Exception as exc:
        raise _invalid("shift_offsets.amount_seconds must be numeric") from exc
    if not amount.is_finite():
        raise _invalid("shift_offsets.amount_seconds must be finite")
    doc = state.documents[doc_id]
    affected: list[tuple[str, str]] = []
    for relation in doc.relations:
        relation.head = relation.head.shifted(amount)
        relation.tail = relation.tail.shifted(amount)
        for component in relation.components:
            component.head = component.head.shifted(amount)
            component.tail = component.tail.shifted(amount)
        affected.append(("relation", relation.entry_id))
    for event in doc.events:
        event.span = event.span.shifted(amount)
        for argument in event.arguments:
            argument.span = argument.span.shifted(amount)
        affected.append(("event", event.entry_id))
    return _affected(doc_id, affected), f"shifted {doc_id} by {decimal_str(amount)} seconds"


def _replace_argument_role(
    state: ProjectState,
    command: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[dict[str, str]], str]:
    doc_id = _command_string(command, "doc_id")
    event_id = _command_string(command, "event_id")
    if doc_id not in state.documents:
        raise _invalid(f"unknown document {doc_id!r}")
    doc = state.documents[doc_id]
    event = next((item for item in doc.events if item.entry_id == event_id), None)
    if event is None:
        raise _invalid(f"unknown event {event_id!r}")
    index = command.get("argument_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(event.arguments):
        raise _invalid("replace_argument_role.argument_index is out of range")
    requested = _command_string(command, "role")
    role_name = state.role_names.get(requested, requested)
    if role_name not in set(event_roles(config, event.event_type)) or role_name not in all_roles(config):
        raise _invalid(
            f"role {role_name!r} is not in the event role table", "E_ROLE_UNKNOWN"
        )
    argument = event.arguments[index]
    if not event.span.contains(argument.span):
        raise _invalid("argument lies outside its event span", "E_SPAN_ORDER")
    argument.role = role_name
    argument.role_key = role_key(config, role_name)
    return _affected(doc_id, [("event", event_id)]), f"replaced argument {index} role with {role_name}"


def apply_edit(
    state: ProjectState,
    command: dict[str, Any],
    config: dict[str, Any],
    max_workers: int,
) -> tuple[ProjectState, list[dict[str, str]], str]:
    """Apply one edit transactionally.

    The input state is never mutated. A validation or loss-recalculation error
    therefore leaves the committed project unchanged.
    """

    command_type = _command_string(command, "type")
    if command_type not in EDIT_TYPES:
        raise _invalid(f"unknown edit type {command_type!r}")
    candidate = state.clone()
    detail = ""
    if command_type == "adjust_trigger":
        affected = _adjust_trigger(candidate, command)
    elif command_type == "merge_relations":
        affected, detail = _merge_relations(candidate, command)
    elif command_type == "split_relation":
        affected, detail = _split_relation(candidate, command, config)
    elif command_type == "shift_offsets":
        affected, detail = _shift_offsets(candidate, command, config)
    else:
        affected, detail = _replace_argument_role(candidate, command, config)
    try:
        validate_documents(candidate.documents, config, max_workers)
    except EditInvalidError:
        raise
    except BaseException as exc:
        raise _invalid(f"post-edit validation failed: {exc}") from exc
    recompute_losses(candidate, config, max_workers)
    candidate.canonicalize()
    return candidate, affected, detail
