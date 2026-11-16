"""Semantics of the five migration rule types.

Every mutator works on a detached :class:`~lineage_migrator.graph.GraphState`
copy and appends :class:`ChangeRecord` entries. Nothing is written to SQLite
until the whole version validates, so a failure leaves no partial state.
"""

from __future__ import annotations

from dataclasses import replace

from .errors import BusinessCode, MigrationError
from .graph import GraphState, weighted_bucket
from .models import Edge, Field, Rule, split_ref


def _rename_ref(ref: str, old: str, new: str) -> str:
    return new if ref == old else ref


def apply_rename(state: GraphState, rule: Rule, version: int) -> None:
    source = state.require_live_field(rule.source_ref, rule.rule_id)
    if not rule.target_ref:
        raise MigrationError(
            BusinessCode.E_RULE_ORDER,
            "rename rule requires target_ref",
            rule_id=rule.rule_id,
        )
    if rule.target_ref in state.fields:
        raise MigrationError(
            BusinessCode.E_RULE_ORDER,
            f"rename target {rule.target_ref} already exists",
            rule_id=rule.rule_id,
        )
    old_ref = source.ref
    table, _ = split_ref(old_ref)
    _, new_name = split_ref(rule.target_ref)
    state.fields.pop(old_ref)
    state.fields[rule.target_ref] = replace(source, table=table, name=new_name)
    rewritten: list[str] = []
    for edge_id, edge in list(state.edges.items()):
        new_source = _rename_ref(edge.source_ref, old_ref, rule.target_ref)
        new_target = _rename_ref(edge.target_ref, old_ref, rule.target_ref)
        if new_source != edge.source_ref or new_target != edge.target_ref:
            state.edges[edge_id] = replace(edge, source_ref=new_source, target_ref=new_target)
            rewritten.append(edge_id)
    state.record(
        rule.rule_id, rule.rule_type, version, "rename",
        {"from": old_ref, "to": rule.target_ref, "rewritten_edges": sorted(rewritten)},
    )


def apply_type_change(state: GraphState, rule: Rule, version: int) -> None:
    source = state.require_live_field(rule.source_ref, rule.rule_id)
    if not rule.new_type:
        raise MigrationError(
            BusinessCode.E_RULE_ORDER,
            "type_change rule requires new_type",
            rule_id=rule.rule_id,
        )
    state.fields[source.ref] = replace(source, data_type=rule.new_type)
    state.record(
        rule.rule_id, rule.rule_type, version, "type_change",
        {"field": source.ref, "from": source.data_type, "to": rule.new_type},
    )


def apply_deprecate(state: GraphState, rule: Rule, version: int) -> list[dict]:
    """Delete every incident edge, mark the field deprecated and audit edges."""
    source = state.require_live_field(rule.source_ref, rule.rule_id)
    deleted = [
        dict(edge_id=edge.edge_id, source_ref=edge.source_ref, target_ref=edge.target_ref)
        for edge in state.ordered_edges()
        if edge.source_ref == source.ref or edge.target_ref == source.ref
    ]
    for edge in deleted:
        state.edges.pop(edge["edge_id"])
    state.fields[source.ref] = replace(source, deprecated=True)
    state.record(
        rule.rule_id, rule.rule_type, version, "deprecate",
        {"field": source.ref, "deleted_edge_ids": [edge["edge_id"] for edge in deleted]},
    )
    audit = [{
        "version": version,
        "rule_id": rule.rule_id,
        "event_type": "deprecated_edges_removed",
        "payload": {"field": source.ref, "deleted_edges": deleted},
    }]
    return audit


def _ensure_target_field(state: GraphState, ref: str, data_type: str, version: int) -> None:
    if ref not in state.fields:
        table, name = split_ref(ref)
        state.fields[ref] = Field(table=table, name=name, data_type=data_type)


def apply_split(state: GraphState, rule: Rule, version: int) -> None:
    """Split one field into several weighted targets.

    Incoming edges of the source are copied to every target; outgoing edges
    are distributed deterministically by :func:`weighted_bucket`. The source
    field is removed at the end.
    """
    source = state.require_live_field(rule.source_ref, rule.rule_id)
    if not rule.targets:
        raise MigrationError(
            BusinessCode.E_RULE_ORDER,
            "split rule requires non-empty targets",
            rule_id=rule.rule_id,
        )
    weights = rule.weights or tuple(1 for _ in rule.targets)
    if len(weights) != len(rule.targets) or any(weight <= 0 for weight in weights):
        raise MigrationError(
            BusinessCode.E_RULE_ORDER,
            "split weights must match targets and be positive",
            rule_id=rule.rule_id,
        )
    for target in rule.targets:
        if target == source.ref:
            raise MigrationError(
                BusinessCode.E_RULE_ORDER,
                f"split target {target} must differ from the source field",
                rule_id=rule.rule_id,
            )
        if rule.targets.count(target) > 1:
            raise MigrationError(
                BusinessCode.E_RULE_ORDER,
                f"split target {target} listed more than once",
                rule_id=rule.rule_id,
            )
        _ensure_target_field(state, target, source.data_type, version)

    incoming = [edge for edge in state.ordered_edges() if edge.target_ref == source.ref]
    outgoing = [edge for edge in state.ordered_edges() if edge.source_ref == source.ref]
    new_edges: list[Edge] = []
    for edge in incoming:
        for target in rule.targets:
            new_edges.append(
                Edge(state.next_edge_id(version), edge.source_ref, target,
                     origin=f"split:{rule.rule_id}:{edge.edge_id}")
            )
    distribution: dict[str, list[str]] = {target: [] for target in rule.targets}
    for edge in outgoing:
        bucket = weighted_bucket(edge.edge_id, weights)
        target = rule.targets[bucket]
        new_edges.append(
            Edge(state.next_edge_id(version), target, edge.target_ref,
                 origin=f"split:{rule.rule_id}:{edge.edge_id}")
        )
        distribution[target].append(edge.edge_id)

    incident_ids = [edge.edge_id for edge in (*incoming, *outgoing)]
    for edge_id in incident_ids:
        state.edges.pop(edge_id, None)
    for edge in new_edges:
        state.edges[edge.edge_id] = edge
    state.fields.pop(source.ref, None)
    state.record(
        rule.rule_id, rule.rule_type, version, "split",
        {
            "source": source.ref,
            "targets": list(rule.targets),
            "weights": list(weights),
            "replaced_edges": sorted(incident_ids),
            "new_edges": sorted(edge.edge_id for edge in new_edges),
            "outgoing_distribution": {key: sorted(value) for key, value in distribution.items()},
        },
    )


def apply_merge(state: GraphState, rule: Rule, version: int) -> None:
    """Converge several source fields into one target, keeping provenance."""
    if not rule.sources or not rule.target_ref:
        raise MigrationError(
            BusinessCode.E_RULE_ORDER,
            "merge rule requires sources and target_ref",
            rule_id=rule.rule_id,
        )
    sources = [state.require_live_field(ref, rule.rule_id) for ref in rule.sources]
    target_type = sources[0].data_type
    _ensure_target_field(state, rule.target_ref, target_type, version)

    source_refs = {field.ref for field in sources}
    replacements: dict[str, Edge] = {}
    for edge in state.ordered_edges():
        if edge.source_ref not in source_refs and edge.target_ref not in source_refs:
            continue
        new_source = rule.target_ref if edge.source_ref in source_refs else edge.target_ref
        new_target = rule.target_ref if edge.target_ref in source_refs else edge.target_ref
        if new_source == new_target:
            key = (new_source, new_target)
            replacement = Edge(
                state.next_edge_id(version), new_source, new_target,
                origin=f"merge:{rule.rule_id}:{edge.edge_id}",
            )
            replacements[key] = replacement
            continue
        key = (new_source, new_target)
        if key in replacements:
            continue
        replacements[key] = Edge(
            state.next_edge_id(version), new_source, new_target,
            origin=f"merge:{rule.rule_id}:{edge.edge_id}",
        )
    removed = sorted(
        edge.edge_id for edge in state.ordered_edges()
        if edge.source_ref in source_refs or edge.target_ref in source_refs
    )
    for edge_id in removed:
        state.edges.pop(edge_id, None)
    for edge in replacements.values():
        state.edges[edge.edge_id] = edge
    for ref in source_refs:
        if ref != rule.target_ref:
            state.fields.pop(ref, None)
    state.record(
        rule.rule_id, rule.rule_type, version, "merge",
        {
            "sources": sorted(source_refs),
            "target": rule.target_ref,
            "replaced_edges": removed,
            "new_edges": sorted(edge.edge_id for edge in replacements.values()),
        },
    )


RULE_HANDLERS = {
    "rename": apply_rename,
    "type_change": apply_type_change,
    "deprecate": apply_deprecate,
    "split": apply_split,
    "merge": apply_merge,
}
