"""Revocation dependency closure and process derivation mapping."""

from __future__ import annotations

from typing import Any

from .canonical import grant_key
from .codes import E_DEP_CYCLE, E_NOT_FOUND, OK
from .graphs import find_cycles, forward_closure, propagation_paths, reverse_edges


def _cycle_report(view: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    cycles: list[dict[str, Any]] = []
    for cycle in view.get("dependency_cycles", []):
        cycles.append({"kind": "grant_dependency", **cycle})

    profiles = [p for p in policy.get("profiles", []) if p.get("valid", True)]
    profile_graph = {p["canonical_key"]: list(p.get("spawns", [])) for p in profiles}
    for cycle in find_cycles(profile_graph):
        cycles.append({"kind": "profile_spawn", **cycle})

    processes = [p for p in policy.get("processes", []) if p.get("valid", True)]
    process_graph: dict[str, list[str]] = {}
    for process in processes:
        process_graph.setdefault(process["canonical_key"], [])
        if process.get("parent_key"):
            process_graph.setdefault(process["parent_key"], []).append(
                process["canonical_key"]
            )
    for cycle in find_cycles(process_graph):
        cycles.append({"kind": "process_parent", **cycle})

    cycles.sort(key=lambda item: (item["kind"], "|".join(item["keys"])))
    if not cycles:
        return {"ok": True}
    return {
        "ok": False,
        "code": E_DEP_CYCLE,
        "message": "dependency or derivation cycle prevents a complete closure",
        "cycles": cycles,
        "trigger_keys": None,
        "closure_grant_keys": None,
        "chains": [],
        "profile_keys": None,
        "affected_processes": None,
    }


def select_trigger_keys(
    view: dict[str, Any],
    operations: list[dict[str, Any]],
    explicit_keys: list[str] | None = None,
    *,
    include_all_events: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    grant_map = view["grant_map"]
    if explicit_keys:
        keys = sorted({grant_key(key) for key in explicit_keys})
        missing = [key for key in keys if key not in grant_map]
        if missing:
            return [], [
                {
                    "code": E_NOT_FOUND,
                    "source": "closure",
                    "index": -1,
                    "message": "one or more grant keys do not exist in the ledger",
                    "canonical_key": key,
                }
                for key in missing
            ]
        descriptors: dict[str, dict[str, Any]] = {
            key: {
                "canonical_key": key,
                "selection": "explicit",
                "kinds": ["requested"],
                "fact_ids": [],
            }
            for key in keys
        }
    else:
        descriptors = {}
        keys = []

    for op in sorted(operations, key=lambda item: item["fact_id"]):
        kind = op["op_kind"]
        if kind not in {"tombstone", "reduce_capabilities"}:
            continue
        key = op["canonical_key"]
        view_item = grant_map.get(key)
        if view_item is None:
            continue
        active = False
        if kind == "tombstone":
            active = view_item["tombstone"] and view_item["authoritative_fact_id"] == op["fact_id"]
            op_kind = "tombstone"
        else:
            active = op["fact_id"] in view_item["applied_reduction_fact_ids"]
            op_kind = "reduce_capabilities"
        if explicit_keys and key not in descriptors:
            continue
        if not explicit_keys and not include_all_events and not active:
            continue
        if key not in descriptors:
            descriptors[key] = {
                "canonical_key": key,
                "selection": "all_events" if include_all_events else "active_revocation",
                "kinds": [],
                "fact_ids": [],
            }
            keys.append(key)
        descriptor = descriptors[key]
        if op_kind not in descriptor["kinds"]:
            descriptor["kinds"].append(op_kind)
        descriptor["fact_ids"].append(op["fact_id"])
        descriptor.setdefault("active", True)
        if kind == "tombstone":
            descriptor["active"] = active
        elif not active:
            descriptor["active"] = False

    trigger_list = [descriptors[key] for key in sorted(descriptors)]
    for descriptor in trigger_list:
        descriptor["kinds"] = sorted(descriptor["kinds"])
        descriptor["fact_ids"] = sorted(descriptor["fact_ids"])
    return trigger_list, []


def build_closure_report(
    policy: dict[str, Any],
    operations: list[dict[str, Any]],
    view: dict[str, Any],
    *,
    explicit_keys: list[str] | None = None,
    include_all_events: bool = False,
    include_unified_view: bool = True,
) -> dict[str, Any]:
    cycle_error = _cycle_report(view, policy)
    if not cycle_error["ok"]:
        if include_unified_view:
            cycle_error["unified_view"] = _public_view(view)
        return cycle_error

    triggers, missing = select_trigger_keys(
        view,
        operations,
        explicit_keys,
        include_all_events=include_all_events,
    )
    if missing:
        return {
            "ok": False,
            "code": E_NOT_FOUND,
            "message": "closure target not found",
            "rejections": missing,
        }
    trigger_keys = [item["canonical_key"] for item in triggers]

    dependency_graph = {
        key: item["dependencies"] for key, item in view["grant_map"].items()
    }
    reverse_dependency_graph = reverse_edges(dependency_graph)
    closure_keys, grant_paths = propagation_paths(
        reverse_dependency_graph,
        trigger_keys,
    )

    chains = [
        {"canonical_key": key, "path": grant_paths[key]}
        for key in sorted(grant_paths)
        if key not in trigger_keys or len(grant_paths[key]) > 1
    ]
    trigger_chains = [
        {"canonical_key": key, "path": grant_paths[key]}
        for key in trigger_keys
        if key in grant_paths
    ]

    bound_profiles = {
        view["grant_map"][key]["profile_key"]
        for key in closure_keys
        if view["grant_map"][key].get("profile_key")
    }
    profiles = [p for p in policy.get("profiles", []) if p.get("valid", True)]
    profile_graph = {p["canonical_key"]: list(p.get("spawns", [])) for p in profiles}
    profile_closure = forward_closure(profile_graph, bound_profiles)

    processes = [p for p in policy.get("processes", []) if p.get("valid", True)]
    process_by_key = {p["canonical_key"]: p for p in processes}
    seed_processes = {
        p["canonical_key"]
        for p in processes
        if p["profile_key"] in profile_closure
    }
    parent_to_children: dict[str, list[str]] = {}
    for process in processes:
        parent_to_children.setdefault(process["canonical_key"], [])
        if process.get("parent_key"):
            parent_to_children.setdefault(process["parent_key"], []).append(
                process["canonical_key"]
            )
    reached_processes, process_paths = propagation_paths(
        parent_to_children,
        seed_processes,
    )

    affected_processes: list[dict[str, Any]] = []
    process_map = view["process_map"]
    grants_by_profile: dict[str, list[str]] = {}
    for key in closure_keys:
        profile_key = view["grant_map"][key].get("profile_key")
        if profile_key:
            grants_by_profile.setdefault(profile_key, []).append(key)

    for process_key in sorted(reached_processes):
        process = process_by_key.get(process_key)
        if process is None or not process.get("in_flight", False):
            continue
        decision = process_map.get(process_key, {})
        affected_processes.append(
            {
                "canonical_key": process_key,
                "pid": process["pid"],
                "profile_key": process["profile_key"],
                "in_flight": True,
                "derivation_path": process_paths.get(process_key, [process_key]),
                "matched_grant_keys": sorted(
                    set(grants_by_profile.get(process["profile_key"], []))
                ),
                "alive": decision.get("alive"),
                "decision": decision.get("decision"),
                "effective_mask": decision.get("effective_mask"),
                "effective_names": decision.get("effective_names"),
                "missing_names": decision.get("missing_names"),
            }
        )

    affected_processes.sort(key=lambda item: item["pid"])
    final_denied = [
        item["canonical_key"]
        for item in affected_processes
        if item.get("alive") is False
    ]
    revived = [
        trigger["canonical_key"]
        for trigger in triggers
        if trigger.get("active") is False
    ]

    report: dict[str, Any] = {
        "ok": True,
        "code": OK,
        "trigger_keys": trigger_keys,
        "triggers": triggers,
        "trigger_chains": trigger_chains,
        "closure_grant_keys": sorted(closure_keys),
        "closure_chains": chains,
        "profile_keys": sorted(profile_closure),
        "bound_profile_keys": sorted(bound_profiles),
        "affected_processes": affected_processes,
        "affected_pids": [item["pid"] for item in affected_processes],
        "final_denied_process_keys": sorted(final_denied),
        "revived_trigger_keys": sorted(revived),
        "conflicts": view.get("conflicts", []),
    }
    if include_unified_view:
        report["unified_view"] = _public_view(view)
    return report


def _public_view(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluation_clock_ms": view["evaluation_clock_ms"],
        "evaluation_complete": view["evaluation_complete"],
        "dependency_cycles": view["dependency_cycles"],
        "conflicts": view["conflicts"],
        "grants": view["grants"],
        "processes": view["processes"],
        "versions": view["versions"],
        "markers": view["markers"],
    }
