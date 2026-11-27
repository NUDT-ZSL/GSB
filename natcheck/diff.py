"""Cross-version and primary/reference discrepancy reporting."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Iterable

from .model import canonical_json
from .normalize import canonical_for_comparison


MISSING = "<missing>"


def deep_differences(
    left: Any,
    right: Any,
    *,
    path: str = "$",
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    if type(left) is not type(right):
        return [
            {
                "path": path,
                "left": copy.deepcopy(left),
                "right": copy.deepcopy(right),
                "kind": "type",
            }
        ]
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}"
            if key not in left:
                differences.append(
                    {"path": child, "left": MISSING, "right": copy.deepcopy(right[key]), "kind": "missing"}
                )
            elif key not in right:
                differences.append(
                    {"path": child, "left": copy.deepcopy(left[key]), "right": MISSING, "kind": "missing"}
                )
            else:
                differences.extend(
                    deep_differences(left[key], right[key], path=child)
                )
        return differences
    if isinstance(left, list):
        if len(left) != len(right):
            differences.append(
                {
                    "path": path,
                    "left": len(left),
                    "right": len(right),
                    "kind": "length",
                }
            )
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(
                deep_differences(
                    left_item, right_item, path=f"{path}[{index}]"
                )
            )
        return differences
    if left != right:
        return [
            {
                "path": path,
                "left": copy.deepcopy(left),
                "right": copy.deepcopy(right),
                "kind": "value",
            }
        ]
    return []


def representation_differences(
    models: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for version, model in models.items():
        for entry in model.get("provenance", []):
            key = (
                entry["entity_type"],
                entry["entity_id"],
                entry["field"],
            )
            groups[key][version] = entry

    differences: list[dict[str, Any]] = []
    for (entity_type, entity_id, field), variants in sorted(groups.items()):
        signatures = {
            version: (
                entry.get("raw_path"),
                canonical_json(entry.get("raw_value")),
            )
            for version, entry in variants.items()
        }
        if len(set(signatures.values())) <= 1:
            continue
        normalized_values = {
            canonical_json(entry.get("normalized")) for entry in variants.values()
        }
        differences.append(
            {
                "scope": "normalization",
                "entity_type": entity_type,
                "entity_id": entity_id,
                "field": field,
                "variants": [
                    {
                        "schema": version,
                        "raw_path": entry.get("raw_path"),
                        "raw_value": copy.deepcopy(entry.get("raw_value")),
                        "normalized": copy.deepcopy(entry.get("normalized")),
                    }
                    for version, entry in sorted(variants.items())
                ],
                "normalized_equal": len(normalized_values) == 1,
                "kind": "representation",
            }
        )
    return differences


def compare_normalized_versions(
    models: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    versions = sorted(models)
    if not versions:
        return {
            "equal": True,
            "differences": [],
            "semantic_differences": [],
        }
    reference_version = versions[0]
    semantic_differences: list[dict[str, Any]] = []
    for version in versions[1:]:
        for item in deep_differences(
            canonical_for_comparison(models[reference_version]),
            canonical_for_comparison(models[version]),
        ):
            item = copy.deepcopy(item)
            item["scope"] = "semantic"
            item["left_schema"] = reference_version
            item["right_schema"] = version
            semantic_differences.append(item)

    representation = representation_differences(models)
    equal = not semantic_differences and all(
        item["normalized_equal"] for item in representation
    )
    return {
        "equal": equal,
        "differences": representation + semantic_differences,
        "representation_differences": representation,
        "semantic_differences": semantic_differences,
    }


def _trace_mismatch(
    primary: dict[str, Any],
    reference: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any] | None:
    primary_trace = primary.get("trace") or []
    reference_trace = reference.get("trace") or []
    for index, (left_item, right_item) in enumerate(
        zip(primary_trace, reference_trace)
    ):
        differences = deep_differences(
            left_item, right_item, path=f"trace[{index}]"
        )
        if differences:
            difference = differences[0]
            event = right_item
            return {
                "session_id": session["session_id"],
                "connection_id": session["connection_id"],
                "event_id": event.get("event_id"),
                "time": event.get("time"),
                "path": difference["path"],
                "basis": (
                    "state trace differs at every-event checkpoint "
                    f"{difference['path']}"
                ),
                "primary": difference["left"],
                "reference": difference["right"],
            }
    if len(primary_trace) != len(reference_trace):
        event = (
            reference_trace[min(len(primary_trace), len(reference_trace))]
            if reference_trace
            else {}
        )
        return {
            "session_id": session["session_id"],
            "connection_id": session["connection_id"],
            "event_id": event.get("event_id"),
            "time": event.get("time"),
            "path": "trace.length",
            "basis": "state trace length differs",
            "primary": len(primary_trace),
            "reference": len(reference_trace),
        }
    return None


def compare_with_reference(
    primary_result: dict[str, Any],
    reference_result: dict[str, Any],
) -> dict[str, Any]:
    primary_sessions = {
        item["session_id"]: item for item in primary_result["sessions"]
    }
    reference_sessions = {
        item["session_id"]: item for item in reference_result["sessions"]
    }
    session_ids = sorted(set(primary_sessions) | set(reference_sessions))
    checked_trace_entries = 0
    checked_mappings = 0
    first_mismatch: dict[str, Any] | None = None

    for session_id in session_ids:
        primary = primary_sessions.get(session_id)
        reference = reference_sessions.get(session_id)
        if primary is None or reference is None:
            available = primary or reference or {}
            first_mismatch = {
                "session_id": session_id,
                "connection_id": available.get("connection_id"),
                "event_id": available.get("created_event_id"),
                "time": available.get("created_at"),
                "path": "sessions.membership",
                "basis": "session exists only on one side",
                "primary": primary is not None,
                "reference": reference is not None,
            }
            break

        checked_trace_entries += max(
            len(primary.get("trace") or []),
            len(reference.get("trace") or []),
        )
        trace_mismatch = _trace_mismatch(primary, reference, reference)
        if trace_mismatch is not None:
            first_mismatch = trace_mismatch
            break

        mapping_differences = deep_differences(
            primary.get("mapping"),
            reference.get("mapping"),
            path="mapping",
        )
        checked_mappings += 1
        if mapping_differences:
            difference = mapping_differences[0]
            first_mismatch = {
                "session_id": session_id,
                "connection_id": reference.get("connection_id"),
                "event_id": (
                    (reference.get("mapping") or {}).get("assigned_event_id")
                ),
                "time": (
                    (reference.get("mapping") or {}).get("assigned_at")
                ),
                "path": difference["path"],
                "basis": (
                    "mapping endpoint/rule differs at "
                    f"{difference['path']}"
                ),
                "primary": difference["left"],
                "reference": difference["right"],
            }
            break

        for field in (
            "state",
            "require_rebuild",
            "expires_at",
            "terminal_at",
            "mapping_history",
            "rebuild_reasons",
        ):
            differences = deep_differences(
                primary.get(field),
                reference.get(field),
                path=field,
            )
            if differences:
                difference = differences[0]
                first_mismatch = {
                    "session_id": session_id,
                    "connection_id": reference.get("connection_id"),
                    "event_id": (
                        (reference.get("trace") or [{}])[-1].get("event_id")
                    ),
                    "time": reference.get("terminal_at"),
                    "path": difference["path"],
                    "basis": f"session {field} differs",
                    "primary": difference["left"],
                    "reference": difference["right"],
                }
                break
        if first_mismatch is not None:
            break

    if first_mismatch is None:
        primary_impacts = {
            item["change_event_id"]: item for item in primary_result["impacts"]
        }
        reference_impacts = {
            item["change_event_id"]: item for item in reference_result["impacts"]
        }
        for event_id in sorted(set(primary_impacts) | set(reference_impacts)):
            if first_mismatch is not None:
                break
            primary = primary_impacts.get(event_id)
            reference = reference_impacts.get(event_id)
            if primary is None or reference is None:
                first_mismatch = {
                    "session_id": None,
                    "connection_id": None,
                    "event_id": event_id,
                    "time": (primary or reference or {}).get("time"),
                    "path": "impacts.membership",
                    "basis": "impact event exists only on one side",
                    "primary": primary is not None,
                    "reference": reference is not None,
                }
                break
            for field in ("affected", "unaffected_sessions", "target", "applied"):
                differences = deep_differences(
                    primary.get(field),
                    reference.get(field),
                    path=f"impact[{event_id}].{field}",
                )
                if differences:
                    difference = differences[0]
                    affected_ids = [
                        item["session_id"]
                        for item in (reference.get("affected") or [])
                    ]
                    first_mismatch = {
                        "session_id": (
                            affected_ids[0] if affected_ids else None
                        ),
                        "connection_id": (
                            (reference.get("affected") or [{}])[0].get(
                                "connection_id"
                            )
                            if affected_ids
                            else None
                        ),
                        "event_id": event_id,
                        "time": reference.get("time"),
                        "path": difference["path"],
                        "basis": (
                            "policy-change impact list differs at "
                            f"{difference['path']}"
                        ),
                        "primary": difference["left"],
                        "reference": difference["right"],
                    }
                    break

    if first_mismatch is None:
        for field in ("residual_cleanups", "ignored_events"):
            differences = deep_differences(
                primary_result.get(field),
                reference_result.get(field),
                path=field,
            )
            if differences:
                difference = differences[0]
                first_mismatch = {
                    "session_id": None,
                    "connection_id": None,
                    "event_id": None,
                    "time": None,
                    "path": difference["path"],
                    "basis": f"{field} differs",
                    "primary": difference["left"],
                    "reference": difference["right"],
                }
                break

    return {
        "ok": first_mismatch is None,
        "checked_sessions": len(session_ids),
        "checked_trace_entries": checked_trace_entries,
        "checked_mappings": checked_mappings,
        "checked_impacts": len(primary_result["impacts"]),
        "first_mismatch": first_mismatch,
    }


def compare_result_versions(
    results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    versions = sorted(results)
    if not versions:
        return {"equal": True, "differences": [], "first_mismatch": None}
    left_version = versions[0]
    differences: list[dict[str, Any]] = []
    first_mismatch: dict[str, Any] | None = None

    for right_version in versions[1:]:
        left_sessions = {
            item["session_id"]: item for item in results[left_version]["sessions"]
        }
        right_sessions = {
            item["session_id"]: item for item in results[right_version]["sessions"]
        }
        for session_id in sorted(set(left_sessions) | set(right_sessions)):
            left = left_sessions.get(session_id)
            right = right_sessions.get(session_id)
            if left is None or right is None:
                item = {
                    "scope": "result",
                    "left_schema": left_version,
                    "right_schema": right_version,
                    "session_id": session_id,
                    "connection_id": (left or right or {}).get("connection_id"),
                    "event_id": None,
                    "path": "sessions.membership",
                    "left": left is not None,
                    "right": right is not None,
                }
                differences.append(item)
                if first_mismatch is None:
                    first_mismatch = item
                continue
            for field in (
                "trace",
                "mapping",
                "state",
                "require_rebuild",
                "mapping_history",
            ):
                for diff in deep_differences(
                    left.get(field), right.get(field), path=field
                ):
                    event_id = None
                    time = right.get("created_at")
                    if field == "trace" and "[" in diff["path"]:
                        try:
                            index_text = diff["path"].split("[", 1)[1].split("]", 1)[0]
                            index = int(index_text)
                            trace_item = (right.get("trace") or [])[index]
                            event_id = trace_item.get("event_id")
                            time = trace_item.get("time")
                        except (ValueError, IndexError):
                            pass
                    item = {
                        "scope": "result",
                        "left_schema": left_version,
                        "right_schema": right_version,
                        "session_id": session_id,
                        "connection_id": right.get("connection_id"),
                        "event_id": event_id,
                        "time": time,
                        "path": f"sessions[{session_id}].{diff['path']}",
                        "left": diff["left"],
                        "right": diff["right"],
                    }
                    differences.append(item)
                    if first_mismatch is None:
                        first_mismatch = item

        for field in ("impacts", "residual_cleanups", "ignored_events"):
            for diff in deep_differences(
                results[left_version].get(field),
                results[right_version].get(field),
                path=field,
            ):
                item = {
                    "scope": "result",
                    "left_schema": left_version,
                    "right_schema": right_version,
                    "session_id": None,
                    "connection_id": None,
                    "event_id": None,
                    "time": None,
                    "path": f"{field}{diff['path'][1:]}",
                    "left": diff["left"],
                    "right": diff["right"],
                }
                differences.append(item)
                if first_mismatch is None:
                    first_mismatch = item

    return {
        "equal": not differences,
        "differences": differences,
        "first_mismatch": first_mismatch,
    }

