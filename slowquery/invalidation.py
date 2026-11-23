"""Statistics-dependency invalidation closure."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .aggregate import aggregate_from_row
from .errors import EngineError
from .planner import estimate_signature, profile_signature


def flatten_statistics(snapshot: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for table_name, table in snapshot.get("tables", {}).items():
        flattened[f"table:{table_name}:row_count"] = table.get("row_count")
        for column_name, column in table.get("columns", {}).items():
            flattened[f"table:{table_name}:column:{column_name}:ndv"] = column.get(
                "ndv"
            )
        for index_name, index in table.get("indexes", {}).items():
            flattened[f"table:{table_name}:index:{index_name}:columns"] = index.get(
                "columns"
            )
            flattened[f"table:{table_name}:index:{index_name}:unique"] = index.get(
                "unique", False
            )
            if "selectivity" in index:
                flattened[
                    f"table:{table_name}:index:{index_name}:selectivity"
                ] = index["selectivity"]
            for prefix, selectivity in index.get("prefix_selectivity", {}).items():
                flattened[
                    f"table:{table_name}:index:{index_name}:prefix:{prefix}"
                ] = selectivity
    return flattened


def changed_statistics(
    base: dict[str, Any], new: dict[str, Any]
) -> list[dict[str, Any]]:
    old_items = flatten_statistics(base)
    new_items = flatten_statistics(new)
    changes = []
    for key in sorted(set(old_items) | set(new_items)):
        old_value = old_items.get(key)
        new_value = new_items.get(key)
        if old_value != new_value:
            changes.append(
                {
                    "item": key,
                    "before": old_value,
                    "after": new_value,
                }
            )
    return changes


def compute_invalidation_closure(
    conn: sqlite3.Connection,
    *,
    base_snapshot: dict[str, Any],
    new_snapshot: dict[str, Any],
    classification: str,
    model: dict[str, Any],
) -> dict[str, Any]:
    changes = changed_statistics(base_snapshot, new_snapshot)
    profile_rows = conn.execute(
        "SELECT template_id, snapshot_version, result_json "
        "FROM profiles ORDER BY template_id, snapshot_version"
    ).fetchall()
    profile_map = {
        (row["template_id"], row["snapshot_version"]): row for row in profile_rows
    }
    aggregate_rows = conn.execute(
        "SELECT * FROM template_aggregates ORDER BY template_id, snapshot_version"
    ).fetchall()
    affected_templates: set[str] = set()
    profiles_to_recompute = []
    reusable_profiles = []
    aggregates_to_recompute_map: dict[tuple[str, int], dict[str, Any]] = {}
    handled_profiles: set[tuple[str, int]] = set()
    for aggregate_row in aggregate_rows:
        aggregate = aggregate_from_row(aggregate_row)
        key = (aggregate["template_id"], aggregate["snapshot_version"])
        try:
            new_signature = estimate_signature(
                structure=aggregate["structure"],
                snapshot=new_snapshot,
                model=model,
                measured_rows=float(aggregate["avg_scanned_rows"]),
            )
        except EngineError:
            new_signature = {"error": "cannot_estimate"}
        base_signature = estimate_signature(
            structure=aggregate["structure"],
            snapshot=base_snapshot,
            model=model,
            measured_rows=float(aggregate["avg_scanned_rows"]),
        )
        if new_signature != base_signature:
            affected_templates.add(aggregate["template_id"])
            aggregates_to_recompute_map[(aggregate["template_id"], new_snapshot["version"])] = {
                "template_id": aggregate["template_id"],
                "snapshot_version": new_snapshot["version"],
                "from_snapshot_version": aggregate["snapshot_version"],
                "reason": "statistics_dependency_changed",
            }
        profile = profile_map.get(key)
        if profile is None:
            continue
        handled_profiles.add(key)
        old_signature = profile_signature(json.loads(profile["result_json"]))
        if new_signature != old_signature:
            affected_templates.add(aggregate["template_id"])
            profiles_to_recompute.append(
                {
                    "template_id": aggregate["template_id"],
                    "from_snapshot_version": aggregate["snapshot_version"],
                    "to_snapshot_version": new_snapshot["version"],
                    "reason": "statistics_dependency_changed",
                }
            )
        else:
            reusable_profiles.append(
                {
                    "template_id": aggregate["template_id"],
                    "snapshot_version": aggregate["snapshot_version"],
                }
            )

    for key, profile in profile_map.items():
        if key in handled_profiles:
            continue
        affected_templates.add(key[0])
        profiles_to_recompute.append(
            {
                "template_id": key[0],
                "from_snapshot_version": key[1],
                "to_snapshot_version": new_snapshot["version"],
                "reason": "aggregate_missing",
            }
        )

    affected_sorted = sorted(affected_templates)
    aggregates_to_recompute = [
        aggregates_to_recompute_map[key]
        for key in sorted(aggregates_to_recompute_map)
    ]
    return {
        "base_version": base_snapshot["version"],
        "new_version": new_snapshot["version"],
        "classification": classification,
        "changed_stats": changes,
        "affected_templates": affected_sorted,
        "aggregates_to_recompute": aggregates_to_recompute,
        "profiles_to_recompute": sorted(
            profiles_to_recompute,
            key=lambda item: (
                item["template_id"],
                item["from_snapshot_version"],
                item["to_snapshot_version"],
            ),
        ),
        "reusable_profiles": sorted(
            reusable_profiles,
            key=lambda item: (item["template_id"], item["snapshot_version"]),
        ),
    }
