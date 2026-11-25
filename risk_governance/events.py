"""Change-event parsing and affected batch closure."""

from __future__ import annotations

import re
from typing import Any

from .canonical import read_jsonl


def load_events(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    rows = read_jsonl(path)
    events = []
    for row in rows:
        event = {key: value for key, value in row.items() if not key.startswith("_")}
        if "change_dimension" not in event:
            for alias in ("dimension", "change_type", "changed_dimension", "变更维度"):
                if alias in event:
                    event["change_dimension"] = event[alias]
                    break
        if "event_seq" not in event:
            for alias in ("seq", "sequence", "event_id"):
                if alias in event:
                    event["event_seq"] = event[alias]
                    break
        if "old_config_summary" not in event and "old_summary" in event:
            event["old_config_summary"] = event["old_summary"]
        if "new_config_summary" not in event and "new_summary" in event:
            event["new_config_summary"] = event["new_summary"]
        events.append(event)
    return sorted(events, key=lambda event: int(event.get("event_seq", 0)))


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _summary(event: dict[str, Any], side: str) -> dict[str, Any]:
    value = event.get(f"{side}_config_summary") or event.get(f"{side}_summary") or {}
    return value if isinstance(value, dict) else {}


def _feature_name(dimension: str) -> str | None:
    match = re.search(r"feature(?:_redefinition|_domain|_schema)?[.:]([A-Za-z0-9_\-]+)", dimension)
    if match:
        return match.group(1)
    parts = re.split(r"[.:]", dimension)
    if "feature" in parts:
        index = parts.index("feature")
        if index + 1 < len(parts):
            return parts[index + 1]
    if dimension.startswith("feature_redefinition_"):
        return dimension.removeprefix("feature_redefinition_")
    return None


def _lineage_graph(config: dict[str, Any]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    lineage = config.get("lineage", {}) or {}
    mapping = lineage.get("downstream_by_batch", lineage)
    if isinstance(mapping, dict):
        for upstream, downstream in mapping.items():
            graph.setdefault(str(upstream), set()).update(_as_list(downstream))
    for relation in config.get("batch_dependencies", []) or []:
        if not isinstance(relation, dict):
            continue
        upstream = str(relation.get("batch_id", ""))
        graph.setdefault(upstream, set()).update(_as_list(relation.get("downstream_batch_ids")))
    return graph


def _transitive_closure(seed_ids: set[str], graph: dict[str, set[str]], known_ids: set[str]) -> set[str]:
    closure = set(seed_ids) & known_ids
    stack = list(closure)
    while stack:
        current = stack.pop()
        for downstream in graph.get(current, set()):
            if downstream in known_ids and downstream not in closure:
                closure.add(downstream)
                stack.append(downstream)
    return closure


def resolve_event_closure(
    event: dict[str, Any],
    batch_ids: list[str],
    batch_cycles: dict[str, str],
    config: dict[str, Any],
) -> list[str]:
    known_ids = set(batch_ids)
    dimension = str(event.get("change_dimension", "")).lower()
    old_summary = _summary(event, "old")
    new_summary = _summary(event, "new")
    scope = str(event.get("scope") or new_summary.get("scope") or old_summary.get("scope") or "").lower()

    explicit = set(_as_list(event.get("affected_batch_ids")))
    explicit.update(_as_list(new_summary.get("affected_batch_ids")))
    explicit.update(_as_list(old_summary.get("affected_batch_ids")))
    seeds = explicit & known_ids

    event_batch = event.get("batch_id")
    if event_batch not in (None, "", "*"):
        batch_id = str(event_batch)
        if batch_id in known_ids:
            seeds.add(batch_id)

    cycle_id = event.get("cycle_id") or new_summary.get("cycle_id") or old_summary.get("cycle_id")
    if scope == "cycle" and cycle_id:
        target_cycle = str(cycle_id)
        seeds.update(batch_id for batch_id, cycle in batch_cycles.items() if cycle == target_cycle)

    if event_batch == "*" or scope == "all" or dimension in {"global", "config", "config.global"}:
        seeds.update(known_ids)

    feature_name = _feature_name(dimension)
    if not explicit and scope not in {"batch", "cycle"}:
        if feature_name and ("feature" in dimension):
            feature_batches = set((config.get("feature_batches", {}) or {}).get(feature_name, []))
            if feature_batches:
                seeds.update(feature_batches & known_ids)
            else:
                seeds.update(known_ids)
        elif dimension.startswith(("calibration", "segment_boundar", "threshold", "cost")):
            item_name = None
            match = re.search(r"(?:calibration|segment_boundaries?|threshold(?:_band)?|cost_weights?)[.:]([A-Za-z0-9_\-]+)", dimension)
            if match:
                item_name = match.group(1)
            item_batches = set((config.get("calibration_batches", {}) or {}).get(item_name or "", []))
            if scope == "item" and item_batches:
                seeds.update(item_batches & known_ids)
            elif scope == "item":
                seeds.update(known_ids)

    seeds.update(_as_list(event.get("downstream_batch_ids")))
    seeds.update(_as_list(new_summary.get("downstream_batch_ids")))
    closure = _transitive_closure(seeds, _lineage_graph(config), known_ids)
    return sorted(closure)


def build_event_plan(
    events: list[dict[str, Any]],
    batch_ids: list[str],
    batch_cycles: dict[str, str],
    config: dict[str, Any],
) -> dict[str, Any]:
    closures: list[dict[str, Any]] = []
    cursor: dict[str, int] = {batch_id: 0 for batch_id in batch_ids}
    affected_union: set[str] = set()
    for event in events:
        seq = int(event.get("event_seq", 0))
        closure = resolve_event_closure(event, batch_ids, batch_cycles, config)
        closures.append(
            {
                "event_seq": seq,
                "batch_id": event.get("batch_id"),
                "change_dimension": event.get("change_dimension"),
                "closure_batch_ids": closure,
                "closure_size": len(closure),
                "old_config_summary": _summary(event, "old"),
                "new_config_summary": _summary(event, "new"),
            }
        )
        affected_union.update(closure)
        for batch_id in closure:
            cursor[batch_id] = max(cursor.get(batch_id, 0), seq)
    return {
        "events": closures,
        "event_cursor_by_batch": dict(sorted(cursor.items())),
        "affected_closure": sorted(affected_union),
    }


def coverage_check(
    universe: list[str], recompute_set: list[str], reused_set: list[str]
) -> dict[str, Any]:
    universe_set = set(universe)
    recompute_set_values = set(recompute_set)
    reused_set_values = set(reused_set)
    union = recompute_set_values | reused_set_values
    overlap = recompute_set_values & reused_set_values
    return {
        "universe": sorted(universe_set),
        "recompute_set": sorted(recompute_set_values),
        "reused_set": sorted(reused_set_values),
        "covered_batch_count": len(union & universe_set),
        "universe_count": len(universe_set),
        "missing_batch_ids": sorted(universe_set - union),
        "extra_batch_ids": sorted(union - universe_set),
        "overlap_batch_ids": sorted(overlap),
        "disjoint": not overlap,
        "full_coverage": union == universe_set and not overlap,
    }
