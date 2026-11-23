from __future__ import annotations

from collections import deque
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .models import PlanRecord, ViewDef
from .util import canonical_json, digest_object
from .validation import downstream_map


def _dedupe_strings(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def compare_columns(
    old_columns: Sequence[Mapping[str, Any]],
    new_columns: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    old_by_name = {column["name"]: column for column in old_columns}
    new_by_name = {column["name"]: column for column in new_columns}
    removed = sorted(set(old_by_name) - set(new_by_name))
    added = sorted(set(new_by_name) - set(old_by_name))
    changed = sorted(
        name
        for name in set(old_by_name) & set(new_by_name)
        if old_by_name[name].get("expression_digest")
        != new_by_name[name].get("expression_digest")
    )
    renamed: List[Dict[str, Any]] = []
    old_by_ordinal = {
        column.get("ordinal", index + 1): column
        for index, column in enumerate(old_columns)
    }
    for index, column in enumerate(new_columns):
        ordinal = column.get("ordinal", index + 1)
        old = old_by_ordinal.get(ordinal)
        if (
            old is not None
            and old["name"] != column["name"]
            and old.get("expression_digest") == column.get("expression_digest")
        ):
            renamed.append(
                {
                    "from": old["name"],
                    "to": column["name"],
                    "ordinal": ordinal,
                    "expression_digest": column.get("expression_digest"),
                }
            )
    return {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "changed": changed,
        "star_sensitive": bool(added or removed or renamed or changed),
    }


def _node_references(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for node in plan.get("nodes", []):
        refs: List[Dict[str, Any]] = list(node.get("predicate_columns", []))
        for column in node.get("output_columns", []):
            refs.extend(column.get("references", []))
        for ref in refs:
            key = canonical_json(
                {
                    "node": node.get("id"),
                    "upstream": ref.get("upstream_view"),
                    "column": ref.get("resolved_column"),
                    "context": ref.get("context"),
                    "star": ref.get("star"),
                }
            )
            if key in seen:
                continue
            seen.add(key)
            item = dict(ref)
            item["node_id"] = node.get("id")
            item["node_kind"] = node.get("kind")
            result.append(item)
    return result


def _combined_downstream(
    old_views: Mapping[str, ViewDef],
    new_views: Mapping[str, ViewDef],
) -> Dict[str, List[str]]:
    combined: Dict[str, Set[str]] = {}
    for views in (old_views, new_views):
        for parent, children in downstream_map(views).items():
            combined.setdefault(parent, set()).update(children)
    return {parent: sorted(children) for parent, children in combined.items()}


def _shortest_path(
    downstream: Mapping[str, Sequence[str]], start: str, target: str
) -> List[str]:
    if start == target:
        return [start]
    queue = deque([[start]])
    seen = {start}
    while queue:
        path = queue.popleft()
        for child in downstream.get(path[-1], []):
            if child == target:
                return path + [child]
            if child not in seen:
                seen.add(child)
                queue.append(path + [child])
    return [start, target]


def _origin_paths(
    downstream: Mapping[str, Sequence[str]],
    origins: Iterable[str],
) -> Dict[str, Tuple[str, List[str]]]:
    result: Dict[str, Tuple[str, List[str]]] = {}
    queue = deque((origin, [origin]) for origin in sorted(origins))
    while queue:
        origin, path = queue.popleft()
        current = path[-1]
        existing = result.get(current)
        if existing is not None and len(existing[1]) <= len(path):
            continue
        result[current] = (origin, path)
        for child in downstream.get(current, []):
            queue.append((origin, path + [child]))
    return result


def _event(
    *,
    patch_id: str,
    edit_round: int,
    kind: str,
    view: str,
    node_id: Optional[str],
    trigger_source: Mapping[str, Any],
    propagation_path: Sequence[str],
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    event_material = {
        "patch_id": patch_id,
        "edit_round": edit_round,
        "kind": kind,
        "view": view,
        "node_id": node_id,
        "trigger_source": trigger_source,
        "propagation_path": list(propagation_path),
        "details": details or {},
    }
    event_id = "evt_" + digest_object(event_material)[:24]
    return {
        "event_id": event_id,
        **event_material,
        "status": "pending",
    }


def compute_invalidation(
    *,
    old_views: Mapping[str, ViewDef],
    new_views: Mapping[str, ViewDef],
    old_records: Mapping[str, PlanRecord],
    new_records: Mapping[str, PlanRecord],
    triggers: Mapping[str, Mapping[str, Any]],
    patch_id: str,
    edit_round: int,
) -> Dict[str, Any]:
    """Compute plan/column/SQL invalidation closure without mutating cached records."""
    downstream = _combined_downstream(old_views, new_views)
    changed_views = sorted(
        name
        for name in new_records
        if name not in old_records
        or old_records[name].plan_digest != new_records[name].plan_digest
    )
    deleted_views = sorted(set(old_records) - set(new_records))
    reused_views = sorted(
        name
        for name in new_records
        if name in old_records
        and old_records[name].plan_digest == new_records[name].plan_digest
    )
    origins = set(triggers)
    origin_paths = _origin_paths(downstream, origins)
    events: List[Dict[str, Any]] = []

    for view_name in changed_views:
        record = new_records[view_name]
        direct_trigger = triggers.get(view_name)
        if direct_trigger is not None:
            trigger = dict(direct_trigger)
            path = [view_name]
        else:
            origin, path = origin_paths.get(view_name, ("unknown", [view_name]))
            trigger = dict(
                triggers.get(origin, {"type": "dependency_changed", "view": origin})
            )
        details = {
            "plan_digest_before": (
                old_records[view_name].plan_digest if view_name in old_records else None
            ),
            "plan_digest_after": record.plan_digest,
            "new_view": view_name not in old_records,
            "executable_sql_recomputed": True,
        }
        mapping = _event(
            patch_id=patch_id,
            edit_round=edit_round,
            kind="plan_invalidated",
            view=view_name,
            node_id=record.plan.get("root", {}).get("id"),
            trigger_source=trigger,
            propagation_path=path,
            details=details,
        )
        events.append(mapping)
        events.append(
            _event(
                patch_id=patch_id,
                edit_round=edit_round,
                kind="executable_sql_invalidated",
                view=view_name,
                node_id=record.plan.get("root", {}).get("id"),
                trigger_source=trigger,
                propagation_path=path,
                details={
                    "sql_digest_before": (
                        old_records[view_name].sql_digest
                        if view_name in old_records
                        else None
                    ),
                    "sql_digest_after": record.sql_digest,
                },
            )
        )

    for view_name in deleted_views:
        trigger = dict(
            triggers.get(view_name, {"type": "view_deleted", "view": view_name})
        )
        events.append(
            _event(
                patch_id=patch_id,
                edit_round=edit_round,
                kind="plan_invalidated",
                view=view_name,
                node_id=old_records[view_name].plan.get("root", {}).get("id"),
                trigger_source=trigger,
                propagation_path=[view_name],
                details={
                    "deleted": True,
                    "plan_digest_before": old_records[view_name].plan_digest,
                    "plan_digest_after": None,
                },
            )
        )

    column_events: List[Dict[str, Any]] = []
    changed_column_map: Dict[str, Dict[str, Any]] = {}
    for trigger_view in sorted(triggers):
        if trigger_view not in old_records or trigger_view not in new_records:
            continue
        changes = compare_columns(
            old_records[trigger_view].columns,
            new_records[trigger_view].columns,
        )
        changed_column_map[trigger_view] = changes
        impacted_names = set(changes["removed"]) | set(changes["changed"])
        impacted_names.update(item["from"] for item in changes["renamed"])
        if not changes["star_sensitive"]:
            continue
        for downstream_view in sorted(old_records):
            if downstream_view == trigger_view:
                continue
            if trigger_view not in old_views or downstream_view not in new_records:
                continue
            for ref in _node_references(old_records[downstream_view].plan):
                if ref.get("upstream_view") != trigger_view:
                    continue
                is_star = bool(ref.get("star"))
                resolved = ref.get("resolved_column")
                if not is_star and resolved not in impacted_names:
                    continue
                trigger = dict(triggers[trigger_view])
                path = _shortest_path(downstream, trigger_view, downstream_view)
                column_events.append(
                    _event(
                        patch_id=patch_id,
                        edit_round=edit_round,
                        kind="column_resolution_invalidated",
                        view=downstream_view,
                        node_id=ref.get("node_id"),
                        trigger_source=trigger,
                        propagation_path=path,
                        details={
                            "upstream_view": trigger_view,
                            "column": resolved,
                            "star": is_star,
                            "context": ref.get("context"),
                            "reference_position": ref.get("reference_position"),
                            "column_changes": changes,
                        },
                    )
                )

    events.extend(column_events)
    deduped: List[Dict[str, Any]] = []
    seen_events: Set[str] = set()
    for event in events:
        if event["event_id"] not in seen_events:
            seen_events.add(event["event_id"])
            deduped.append(event)
    deduped.sort(key=lambda item: (item["kind"], item["view"], item["event_id"]))

    propagation_paths: Dict[str, List[str]] = {}
    for event in deduped:
        key = " -> ".join(event["propagation_path"])
        propagation_paths.setdefault(key, []).append(event["view"])
    for key in propagation_paths:
        propagation_paths[key] = _dedupe_strings(propagation_paths[key])

    return {
        "patch_id": patch_id,
        "edit_round": edit_round,
        "triggers": [dict(triggers[name]) for name in sorted(triggers)],
        "column_changes": changed_column_map,
        "recomputed_views": changed_views,
        "reused_views": reused_views,
        "deleted_views": deleted_views,
        "recomputed_executable_sql": changed_views,
        "reused_executable_sql": reused_views,
        "events": deduped,
        "propagation_paths": propagation_paths,
        "summary": {
            "event_count": len(deduped),
            "plan_invalidated_count": sum(
                1 for event in deduped if event["kind"] == "plan_invalidated"
            ),
            "column_resolution_invalidated_count": len(column_events),
            "executable_sql_invalidated_count": sum(
                1 for event in deduped if event["kind"] == "executable_sql_invalidated"
            ),
            "reused_plan_count": len(reused_views),
            "recomputed_plan_count": len(changed_views),
        },
    }
