"""Deterministic merge of v1/v2 grants and immutable patch facts."""

from __future__ import annotations

from typing import Any

from .capabilities import names_from_mask
from .codes import E_DEP_CYCLE, E_OBS_CLOCK
from .errors import GuardError
from .graphs import find_cycles


def authority_key(item: dict[str, Any]) -> tuple[int, int, str, str]:
    return (
        int(item["generation"]),
        int(item["effective_at_ms"]),
        str(item["canonical_key"]),
        str(item["fact_id"]),
    )


def _valid(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get("valid", True)]


def _conflict(
    canonical_key: str,
    field: str,
    left: dict[str, Any],
    right: dict[str, Any],
    selected: dict[str, Any],
) -> dict[str, Any]:
    if field == "capabilities":
        left_value: Any = {
            "names": names_from_mask(left["capability_mask"]),
            "mask": left["capability_mask"],
        }
        right_value = {
            "names": names_from_mask(right["capability_mask"]),
            "mask": right["capability_mask"],
        }
    else:
        left_value = left.get(field)
        right_value = right.get(field)
    return {
        "canonical_key": canonical_key,
        "field": field,
        "sources": [
            {
                "source": left["source"],
                "fact_id": left["fact_id"],
                "source_index": left.get("source_index"),
                "generation": left["generation"],
                "effective_at_ms": left["effective_at_ms"],
                "value": left_value,
            },
            {
                "source": right["source"],
                "fact_id": right["fact_id"],
                "source_index": right.get("source_index"),
                "generation": right["generation"],
                "effective_at_ms": right["effective_at_ms"],
                "value": right_value,
            },
        ],
        "authoritative_fact_id": selected["fact_id"],
        "resolution": "generation_then_effective_time_then_canonical_fact",
    }


def build_unified_view(
    policy: dict[str, Any],
    operations: list[dict[str, Any]] | None = None,
    at_ms: int | None = None,
) -> dict[str, Any]:
    operations = list(operations or [])
    clock = at_ms if at_ms is not None else policy.get("observation_clock_ms")
    if not isinstance(clock, int) or isinstance(clock, bool) or clock < 0:
        raise GuardError(
            E_OBS_CLOCK,
            "evaluation requires an explicit nonnegative at_ms or declared observation_clock_ms",
        )

    baseline_grants = _valid(policy.get("grants", []))
    profiles = {
        item["canonical_key"]: item
        for item in _valid(policy.get("profiles", []))
    }
    processes = _valid(policy.get("processes", []))

    by_key: dict[str, list[dict[str, Any]]] = {}
    for grant in baseline_grants:
        by_key.setdefault(grant["canonical_key"], []).append(grant)

    add_facts: list[dict[str, Any]] = []
    tombstone_facts: list[dict[str, Any]] = []
    dependency_facts: list[dict[str, Any]] = []
    reduction_facts: list[dict[str, Any]] = []

    for op in operations:
        if not op.get("valid", True):
            continue
        kind = op["op_kind"]
        by_key.setdefault(op["canonical_key"], []).append(op)
        if kind == "add_grant":
            add_facts.append(op)
            dependency_facts.append(op)
        elif kind == "tombstone":
            tombstone_facts.append(op)
        elif kind == "set_dependencies":
            dependency_facts.append(op)
        elif kind == "reduce_capabilities":
            reduction_facts.append(op)

    lifecycle_candidates: dict[str, list[dict[str, Any]]] = {}
    for grant in baseline_grants:
        lifecycle_candidates.setdefault(grant["canonical_key"], []).append(grant)
    for op in add_facts + tombstone_facts:
        lifecycle_candidates.setdefault(op["canonical_key"], []).append(op)

    conflicts: list[dict[str, Any]] = []
    grant_views: dict[str, dict[str, Any]] = {}

    for canonical_key in sorted(by_key):
        records = by_key[canonical_key]
        candidates = lifecycle_candidates.get(canonical_key, [])
        if candidates:
            effective_candidates = [
                item for item in candidates if int(item["effective_at_ms"]) <= clock
            ]
            if effective_candidates:
                selected = max(effective_candidates, key=authority_key)
            else:
                selected = min(candidates, key=authority_key)
        else:
            selected = max(records, key=authority_key)

        profile_key = selected.get("profile_key")
        if not profile_key:
            profile_candidates = [item for item in records if item.get("profile_key")]
            if profile_candidates:
                profile_key = max(profile_candidates, key=authority_key)["profile_key"]

        v1 = [item for item in records if item.get("source") == "v1"]
        v2 = [item for item in records if item.get("source") == "v2"]
        if v1 and v2:
            v1_selected = max(v1, key=authority_key)
            v2_selected = max(v2, key=authority_key)
            baseline_selected = max([v1_selected, v2_selected], key=authority_key)
            if v1_selected["capability_mask"] != v2_selected["capability_mask"]:
                conflicts.append(
                    _conflict(
                        canonical_key,
                        "capabilities",
                        v1_selected,
                        v2_selected,
                        baseline_selected,
                    )
                )
            if v1_selected.get("expires_at_ms") != v2_selected.get("expires_at_ms"):
                conflicts.append(
                    _conflict(
                        canonical_key,
                        "expires_at_ms",
                        v1_selected,
                        v2_selected,
                        baseline_selected,
                    )
                )

        adjudicated_mask = 0 if selected.get("tombstone") else int(selected.get("capability_mask", 0))
        applied_reductions: list[str] = []
        ignored_reductions: list[str] = []
        selected_key = authority_key(selected)
        for reduction in sorted(
            (
                item
                for item in reduction_facts
                if item["canonical_key"] == canonical_key
                and int(item["effective_at_ms"]) <= clock
            ),
            key=authority_key,
        ):
            if authority_key(reduction) > selected_key:
                adjudicated_mask &= ~int(reduction["remove_mask"])
                applied_reductions.append(reduction["fact_id"])
            else:
                ignored_reductions.append(reduction["fact_id"])

        dep_candidates = [
            item
            for item in dependency_facts
            if item["canonical_key"] == canonical_key
            and int(item["effective_at_ms"]) <= clock
        ] + [
            item
            for item in v2
            if int(item["effective_at_ms"]) <= clock
        ]
        if dep_candidates:
            dependencies = list(max(dep_candidates, key=authority_key).get("dependencies", []))
        else:
            dependencies = []
        dependencies = sorted(set(dependencies))

        grant_views[canonical_key] = {
            "canonical_key": canonical_key,
            "profile_key": profile_key,
            "source": selected.get("source", selected.get("op_kind", "patch")),
            "authoritative_fact_id": selected["fact_id"],
            "authority_key": list(selected_key),
            "generation": selected["generation"],
            "effective_at_ms": selected["effective_at_ms"],
            "expires_at_ms": selected.get("expires_at_ms"),
            "tombstone": bool(selected.get("tombstone", False)),
            "adjudicated_mask": adjudicated_mask,
            "adjudicated_names": names_from_mask(adjudicated_mask),
            "dependencies": dependencies,
            "applied_reduction_fact_ids": sorted(applied_reductions),
            "ignored_reduction_fact_ids": sorted(ignored_reductions),
            "active": False,
            "usable": False,
            "effective_mask": 0,
            "effective_names": [],
            "validity_reasons": [],
        }

    dependency_graph = {
        key: view["dependencies"] for key, view in grant_views.items()
    }
    cycles = find_cycles(dependency_graph)

    if cycles:
        for view in grant_views.values():
            view["validity_reasons"] = [E_DEP_CYCLE]
        evaluation_complete = False
    else:
        evaluation_complete = True
        validity_memo: dict[str, bool] = {}

        def valid_grant(key: str) -> bool:
            if key in validity_memo:
                return validity_memo[key]
            view = grant_views.get(key)
            if view is None:
                validity_memo[key] = False
                return False
            reasons: list[str] = []
            own_valid = True
            if clock < view["effective_at_ms"]:
                reasons.append("E_GRANT_NOT_EFFECTIVE")
                own_valid = False
            elif view["tombstone"]:
                reasons.append("E_GRANT_TOMBSTONED")
                own_valid = False
            expires = view.get("expires_at_ms")
            if own_valid and expires is not None and clock >= int(expires):
                reasons.append("E_GRANT_EXPIRED")
                own_valid = False
            if not own_valid:
                view["validity_reasons"] = sorted(set(reasons))
                validity_memo[key] = False
                return False

            dependency_valid = True
            for dependency in view["dependencies"]:
                if not valid_grant(dependency):
                    dependency_valid = False
                    reasons.append("E_DEPENDENCY_UNUSABLE")
            if not dependency_valid:
                view["validity_reasons"] = sorted(set(reasons))
                validity_memo[key] = False
                return False

            view["active"] = True
            view["usable"] = True
            view["effective_mask"] = view["adjudicated_mask"]
            view["effective_names"] = names_from_mask(view["effective_mask"])
            view["validity_reasons"] = []
            validity_memo[key] = True
            return True

        for key in sorted(grant_views):
            valid_grant(key)

    grants_by_profile: dict[str, list[str]] = {}
    for key, view in grant_views.items():
        if view.get("profile_key"):
            grants_by_profile.setdefault(view["profile_key"], []).append(key)

    process_views: list[dict[str, Any]] = []
    for process in sorted(processes, key=lambda item: int(item["pid"])):
        profile_key = process["profile_key"]
        profile = profiles.get(profile_key)
        required_mask = int(profile["capability_mask"]) if profile else 0
        granted_mask = 0
        grant_keys: list[str] = []
        for grant_key_value in sorted(grants_by_profile.get(profile_key, [])):
            view = grant_views[grant_key_value]
            grant_keys.append(grant_key_value)
            granted_mask |= int(view["effective_mask"])
        effective_mask = required_mask & granted_mask
        missing_mask = required_mask & ~granted_mask
        missing_names = names_from_mask(missing_mask)
        alive = evaluation_complete and missing_mask == 0 and profile is not None
        process_views.append(
            {
                "canonical_key": process["canonical_key"],
                "pid": process["pid"],
                "profile_key": profile_key,
                "status": process["status"],
                "in_flight": process["in_flight"],
                "parent_key": process.get("parent_key"),
                "required_mask": required_mask,
                "required_names": names_from_mask(required_mask),
                "granted_mask": granted_mask,
                "granted_names": names_from_mask(granted_mask),
                "effective_mask": effective_mask,
                "effective_names": names_from_mask(effective_mask),
                "missing_mask": missing_mask,
                "missing_names": missing_names,
                "alive": alive if evaluation_complete else None,
                "decision": (
                    "allowed"
                    if alive
                    else "denied"
                    if evaluation_complete
                    else "cycle_blocked"
                ),
                "grant_keys": grant_keys,
            }
        )

    return {
        "evaluation_clock_ms": clock,
        "evaluation_complete": evaluation_complete,
        "dependency_cycles": cycles,
        "conflicts": sorted(
            conflicts,
            key=lambda item: (item["canonical_key"], item["field"]),
        ),
        "grants": [grant_views[key] for key in sorted(grant_views)],
        "grant_map": grant_views,
        "processes": process_views,
        "process_map": {item["canonical_key"]: item for item in process_views},
        "versions": policy.get("versions", {}),
        "markers": policy.get("markers", {}),
    }
