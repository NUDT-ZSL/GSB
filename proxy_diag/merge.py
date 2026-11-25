"""Cross-version parsing, adjudication and unified session view."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .clock import ClockSpec
from .jsonio import canonical_hash, read_json
from .packageio import PackageBundle

SOURCE_PRIORITY = {"v1": 1, "v2": 2}


def _semantic_entry_hash(entry: dict[str, Any]) -> str:
    return canonical_hash(
        {key: value for key, value in entry.items() if key not in {"entry_digest", "table_version"}}
    )


def table_diff(old_table: dict[str, Any] | None, new_table: dict[str, Any] | None) -> dict[str, Any]:
    old_entries = {entry["route_key"]: entry for entry in (old_table or {}).get("entries", [])}
    new_entries = {entry["route_key"]: entry for entry in (new_table or {}).get("entries", [])}
    added = sorted(set(new_entries) - set(old_entries))
    removed = sorted(set(old_entries) - set(new_entries))
    modified = sorted(
        key
        for key in set(old_entries) & set(new_entries)
        if _semantic_entry_hash(old_entries[key]) != _semantic_entry_hash(new_entries[key])
    )
    tombstoned = sorted(
        key
        for key in new_entries
        if new_entries[key].get("tombstone") and not old_entries.get(key, {}).get("tombstone", False)
    )
    return {
        "old_version": (old_table or {}).get("version"),
        "new_version": (new_table or {}).get("version"),
        "added": added,
        "removed": removed,
        "modified": modified,
        "newly_tombstoned": tombstoned,
        "unchanged_count": len(set(old_entries) & set(new_entries) - set(modified)),
    }


def _v2_sessions(bundle: PackageBundle) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    manifest_dir = bundle.v2_manifest_path.parent
    for shard in bundle.v2_manifest["shards"]:
        payload = read_json((manifest_dir / shard["path"]).resolve())
        for session in payload.get("sessions", []):
            result[session["connection_id"]] = session
    return result


def _v1_candidate(session: dict[str, Any], clock: ClockSpec) -> dict[str, Any]:
    normalized_records: list[dict[str, Any]] = []
    conversions: list[dict[str, Any]] = []
    for record in session["records"]:
        conversion = clock.convert_v1_second(record["record_second"])
        conversions.append(conversion)
        normalized_records.append(
            {
                "seq": record["seq"],
                "direction": record["direction"],
                "hop": record["hop"],
                "logical_clock_ms": conversion["logical_clock_ms"],
                "absolute_ms": conversion["absolute_ms"],
                "request_status_code": record.get("request_status_code"),
                "response_status_code": record.get("response_status_code"),
                "request_headers": record.get("request_headers", {}),
                "time_representation": {
                    "source": "v1_relative_record_seconds",
                    "record_second": record["record_second"],
                    "conversion": conversion,
                },
            }
        )
    logical_clock = max(item["logical_clock_ms"] for item in normalized_records)
    return {
        "source_format": "v1",
        "generation": int(session.get("session_generation", 1)),
        "declared_logical_clock_ms": logical_clock,
        "table_version": session.get("table_version"),
        "table_refs": session.get("table_refs", []),
        "original_time_representation": "relative_record_seconds_non_negative_integer",
        "time_conversion": {
            "scale_ms_per_second": clock.scale_numerator / clock.scale_denominator,
            "rounding_direction": clock.rounding,
            "clock_id": clock.clock_id,
            "records": conversions,
        },
        "records": normalized_records,
    }


def _v2_candidate(session: dict[str, Any]) -> dict[str, Any]:
    normalized_records: list[dict[str, Any]] = []
    for record in session["records"]:
        normalized_records.append(
            {
                "seq": record["seq"],
                "direction": record["direction"],
                "hop": record["hop"],
                "logical_clock_ms": int(record["logical_clock_ms"]),
                "absolute_ms": int(record["absolute_ms"]),
                "request_status_code": record.get("request_status_code"),
                "response_status_code": record.get("response_status_code"),
                "request_headers": record.get("request_headers", {}),
                "time_representation": {
                    "source": "v2_absolute_milliseconds",
                    "absolute_ms": int(record["absolute_ms"]),
                    "logical_clock_ms": int(record["logical_clock_ms"]),
                },
            }
        )
    declared_clock = int(session.get("logical_clock_ms") or max(r["logical_clock_ms"] for r in normalized_records))
    return {
        "source_format": "v2",
        "generation": int(session["generation"]),
        "declared_logical_clock_ms": declared_clock,
        "table_version": session.get("table_version"),
        "table_refs": session.get("table_refs", []),
        "original_time_representation": "absolute_milliseconds_integer",
        "records": normalized_records,
    }


def adjudicate(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(candidates) == 1:
        winner = candidates[0]
        basis = {
            "rule": "only_one_source",
            "winner_source": winner["source_format"],
            "source_priority": SOURCE_PRIORITY,
        }
        return winner, basis

    ordered = sorted(
        candidates,
        key=lambda item: (
            item["declared_logical_clock_ms"],
            item["generation"],
            SOURCE_PRIORITY[item["source_format"]],
        ),
        reverse=True,
    )
    winner = ordered[0]
    loser = ordered[1]
    if winner["declared_logical_clock_ms"] != loser["declared_logical_clock_ms"]:
        rule = "higher_declared_logical_clock_ms"
        reason = "logical_clock"
    elif winner["generation"] != loser["generation"]:
        rule = "equal_logical_clock_higher_generation"
        reason = "session_generation"
    else:
        rule = "equal_logical_clock_and_generation_source_format_priority_v2_over_v1"
        reason = "source_format_priority"
    basis = {
        "rule": rule,
        "decisive_dimension": reason,
        "winner_source": winner["source_format"],
        "source_priority": SOURCE_PRIORITY,
        "compared": [
            {
                "source_format": item["source_format"],
                "declared_logical_clock_ms": item["declared_logical_clock_ms"],
                "generation": item["generation"],
                "priority": SOURCE_PRIORITY[item["source_format"]],
            }
            for item in ordered
        ],
    }
    return winner, basis


def initial_stage(records: list[dict[str, Any]], tombstoned: bool) -> dict[str, Any]:
    forwards = [r for r in records if r["direction"] == "forward"]
    reverses = [r for r in records if r["direction"] == "reverse"]
    timestamps = [r["logical_clock_ms"] for r in records]
    if tombstoned:
        stage = "tombstone_applied"
    elif reverses:
        stage = "response_returned"
    elif forwards:
        stage = "request_arrived"
    else:
        stage = "declared"
    return {
        "stage": stage,
        "first_clock_ms": min(timestamps) if timestamps else None,
        "last_clock_ms": max(timestamps) if timestamps else None,
        "forward_hops": sorted({int(r["hop"]) for r in forwards}),
        "reverse_hops": sorted({int(r["hop"]) for r in reverses}),
    }


def build_unified_view(bundle: PackageBundle, validation_report: dict[str, Any] | None = None) -> dict[str, Any]:
    v1_sessions = {session["connection_id"]: session for session in (bundle.v1 or {}).get("sessions", [])}
    v2_sessions = _v2_sessions(bundle)
    connection_ids = sorted(set(v1_sessions) | set(v2_sessions))
    global_diff = table_diff(bundle.v1_table, bundle.v2_table)
    entries_by_version: dict[str, dict[str, dict[str, Any]]] = {}
    for table in (bundle.v1_table, bundle.v2_table):
        if table:
            entries_by_version[str(table.get("version"))] = {e["route_key"]: e for e in table.get("entries", [])}

    sessions: list[dict[str, Any]] = []
    for connection_id in connection_ids:
        candidates: list[dict[str, Any]] = []
        if connection_id in v1_sessions:
            candidates.append(_v1_candidate(v1_sessions[connection_id], bundle.clock))
        if connection_id in v2_sessions:
            candidates.append(_v2_candidate(v2_sessions[connection_id]))
        winner, adjudication = adjudicate(candidates)
        route_keys = sorted(set(winner.get("table_refs", [])))
        selected_table_version = winner.get("table_version")
        table_entries = entries_by_version.get(str(selected_table_version), {})
        tombstoned = any(table_entries.get(route, {}).get("tombstone") for route in route_keys)
        stage_hint = initial_stage(winner["records"], tombstoned)
        relevant_diff = {
            **global_diff,
            "routes": [
                route
                for route in sorted(
                    set(global_diff["added"])
                    | set(global_diff["removed"])
                    | set(global_diff["modified"])
                    | set(global_diff["newly_tombstoned"])
                )
                if route in route_keys or any(
                    route in table_entries.get(ref, {}).get("depends_on", []) for ref in route_keys
                )
            ],
        }
        sessions.append(
            {
                "connection_id": connection_id,
                "selected_source_format": winner["source_format"],
                "source_versions": sorted({candidate["source_format"] for candidate in candidates}),
                "generation": winner["generation"],
                "declared_logical_clock_ms": winner["declared_logical_clock_ms"],
                "table_version": selected_table_version,
                "route_keys": route_keys,
                "tombstoned_in_selected_table": tombstoned,
                "stage_hint": stage_hint,
                "adjudication": adjudication,
                "table_version_comparison": {
                    "v1_version": (bundle.v1_table or {}).get("version"),
                    "v2_version": (bundle.v2_table or {}).get("version"),
                    "selected_version": selected_table_version,
                    "diff": relevant_diff,
                },
                "candidates": {candidate["source_format"]: candidate for candidate in candidates},
                "records": winner["records"],
            }
        )

    view = {
        "format": "proxy-unified-session-view",
        "package_id": bundle.package_id,
        "manifest_version": bundle.manifest_version,
        "clock_id": bundle.clock.clock_id,
        "table_version_diff": global_diff,
        "session_count": len(sessions),
        "sessions": sessions,
    }
    if validation_report is not None:
        view["validation_summary"] = {key: value for key, value in validation_report.items() if key != "ok"}
    view["view_hash"] = canonical_hash({k: v for k, v in view.items() if k != "view_hash"})
    return view
