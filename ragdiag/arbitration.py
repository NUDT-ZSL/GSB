"""Explicit quality, memory, and latency conflict arbitration."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from .canonical import format_float, sha256_value
from .formats import Repository
from .planning import apply_profile, sorted_requests


def _content_bytes(entry: dict[str, Any]) -> int:
    content = entry["content"]
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    from .canonical import canonical_bytes

    return len(canonical_bytes(content))


def estimate_request(
    repository: Repository,
    request: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    scheduling = config["scheduling"]
    retrieval = config["retrieval"]
    merge = config.get("merge", {})
    costs = scheduling.get("cost_model", {})
    calls = request["tool_calls"]
    total_content_bytes = 0
    unique_refs = sorted({call["result_ref"] for call in calls})
    for result_ref in unique_refs:
        entry = repository.entries.get(result_ref)
        if entry is not None:
            total_content_bytes += _content_bytes(entry)
    candidate_limit = int(retrieval.get("candidate_limit", 8))
    expansion = int(merge.get("tool_expansion", 0))
    time_ms = (
        float(costs.get("read_ms_per_call", 0.0)) * len(calls)
        + float(costs.get("retrieve_ms", 0.0))
        + float(costs.get("merge_ms", 0.0))
        + float(costs.get("candidate_compare_ms", 0.0)) * candidate_limit
        + float(costs.get("kib_read_ms", 0.0))
        * (total_content_bytes / 1024.0)
        + float(costs.get("tool_expand_ms", 0.5)) * expansion
    )
    memory_bytes = sum(
        _content_bytes(repository.entries[result_ref])
        for result_ref in unique_refs
        if result_ref in repository.entries
    )
    return {
        "time_ms": format_float(time_ms, config["precision"]),
        "memory_bytes": memory_bytes,
        "unique_result_refs": unique_refs,
        "content_bytes": total_content_bytes,
    }


def _per_request_quality(
    request: dict[str, Any], config: dict[str, Any]
) -> float:
    retrieval = config["retrieval"]
    merge = config.get("merge", {})
    top_k = max(1, int(retrieval.get("top_k", 8)))
    candidate_limit = int(retrieval.get("candidate_limit", 8))
    expansion = int(merge.get("tool_expansion", 0))
    calls = len(request["tool_calls"])
    candidate_quality = min(1.0, candidate_limit / top_k)
    expansion_quality = min(1.0, expansion / 3.0)
    fanout_quality = min(1.0, calls / 4.0)
    return (
        0.55 * candidate_quality
        + 0.3 * expansion_quality
        + 0.15 * fanout_quality
    )


def simulate_profile(
    repository: Repository,
    requests: list[dict[str, Any]],
    base_config: dict[str, Any],
    profile: dict[str, Any],
    selectable: bool,
) -> dict[str, Any]:
    config = apply_profile(base_config, profile)
    scheduling = config["scheduling"]
    hard_concurrency = int(scheduling["concurrency_limit"])
    profile_concurrency = int(profile["concurrency"])
    time_budget = float(scheduling["time_budget_ms"])
    memory_budget = int(scheduling["memory_budget_bytes"])
    ordered = sorted_requests(requests)
    resident_refs: set[str] = set()
    admitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    elapsed_ms = 0.0
    for request in ordered:
        estimate = estimate_request(repository, request, config)
        violations: list[str] = []
        if (
            profile_concurrency > hard_concurrency
            or request["required_lanes"] > hard_concurrency
        ):
            violations.append("concurrency")
        new_refs = [
            result_ref
            for result_ref in estimate["unique_result_refs"]
            if result_ref not in resident_refs
        ]
        new_bytes = sum(
            _content_bytes(repository.entries[result_ref])
            for result_ref in new_refs
            if result_ref in repository.entries
        )
        resident_bytes = sum(
            _content_bytes(repository.entries[result_ref])
            for result_ref in resident_refs
            if result_ref in repository.entries
        )
        if resident_bytes + new_bytes > memory_budget:
            violations.append("memory")
        if elapsed_ms + estimate["time_ms"] > time_budget:
            violations.append("time")
        if violations:
            rejected.append(
                {
                    "unit_id": request["unit_id"],
                    "violations": violations,
                }
            )
            continue
        elapsed_ms += estimate["time_ms"]
        resident_refs.update(estimate["unique_result_refs"])
        admitted.append(
            {
                "unit_id": request["unit_id"],
                "time_ms": estimate["time_ms"],
                "memory_bytes": estimate["memory_bytes"],
                "unique_result_refs": estimate["unique_result_refs"],
            }
        )
    concurrency = max(1, min(profile_concurrency, hard_concurrency))
    chunks = [
        admitted[index : index + concurrency]
        for index in range(0, len(admitted), concurrency)
    ]
    makespan = (
        math.fsum(item["time_ms"] for item in admitted) / concurrency
        if admitted
        else 0.0
    )
    memory_peak = 0
    for chunk in chunks:
        chunk_refs: set[str] = set()
        for item in chunk:
            chunk_refs.update(item["unique_result_refs"])
        chunk_bytes = sum(
            _content_bytes(repository.entries[result_ref])
            for result_ref in chunk_refs
            if result_ref in repository.entries
        ) + len(chunk) * int(
            config["retrieval"].get("candidate_limit", 8)
        ) * 256
        memory_peak = max(memory_peak, chunk_bytes)
    quality = (
        math.fsum(
            _per_request_quality(
                next(
                    request
                    for request in ordered
                    if request["unit_id"] == item["unit_id"]
                ),
                config,
            )
            for item in admitted
        )
        / len(admitted)
        if admitted
        else 0.0
    )
    coverage = len(admitted) / len(ordered) if ordered else 0.0
    quality *= coverage
    precision = config["precision"]
    result = {
        "name": profile["name"],
        "selectable": selectable,
        "candidate_limit": int(profile["candidate_limit"]),
        "tool_expansion": int(profile.get("tool_expansion", 0)),
        "concurrency": profile_concurrency,
        "admitted_requests": [item["unit_id"] for item in admitted],
        "rejected_requests": [
            {
                "unit_id": item["unit_id"],
                "violations": sorted(item["violations"]),
            }
            for item in rejected
        ],
        "admitted_time_ms": format_float(elapsed_ms, precision),
        "makespan_ms": format_float(makespan, precision),
        "memory_peak_bytes": memory_peak,
        "quality": format_float(quality, precision),
        "coverage": format_float(coverage, precision),
        "within_hard_budgets": (
            profile_concurrency <= hard_concurrency
            and elapsed_ms <= time_budget
            and memory_peak <= memory_budget
        ),
        "chunks": [
            [item["unit_id"] for item in chunk] for chunk in chunks
        ],
    }
    result["metric_digest"] = sha256_value(result)
    return result


def _select_profile(
    profiles: list[dict[str, Any]],
    priority: list[str],
    quality_tolerance: float,
) -> dict[str, Any]:
    remaining = list(profiles)
    for axis in priority:
        if axis == "quality":
            best = max(profile["quality"] for profile in remaining)
            remaining = [
                profile
                for profile in remaining
                if best - profile["quality"] <= quality_tolerance
            ]
        elif axis == "memory":
            best = min(profile["memory_peak_bytes"] for profile in remaining)
            remaining = [
                profile
                for profile in remaining
                if profile["memory_peak_bytes"] == best
            ]
        else:
            best = min(profile["makespan_ms"] for profile in remaining)
            remaining = [
                profile
                for profile in remaining
                if profile["makespan_ms"] == best
            ]
    return sorted(remaining, key=lambda profile: profile["name"])[0]


def arbitrate(
    repository: Repository,
    requests: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Choose a selectable profile and emit a machine-readable decision log."""

    arbitration = config.get("arbitration", {})
    priority = list(
        arbitration.get("priority_order", ["quality", "memory", "latency"])
    )
    profiles = arbitration.get("profiles", [])
    diagnostic_profiles = arbitration.get("diagnostic_profiles", [])
    if not isinstance(profiles, list) or not profiles:
        profiles = [
            {
                "name": "base",
                "candidate_limit": int(
                    config["retrieval"].get("candidate_limit", 8)
                ),
                "tool_expansion": int(
                    config.get("merge", {}).get("tool_expansion", 0)
                ),
                "concurrency": int(
                    config["scheduling"].get("concurrency_limit", 1)
                ),
            }
        ]
    matrix = [
        simulate_profile(repository, requests, config, profile, True)
        for profile in profiles
    ]
    selectable = [
        profile
        for profile in matrix
        if profile["concurrency"]
        <= int(config["scheduling"]["concurrency_limit"])
    ]
    if not selectable:
        selectable = matrix
    feasible = [
        profile for profile in selectable if profile["within_hard_budgets"]
    ]
    if feasible:
        selectable = feasible
    quality_tolerance = float(
        arbitration.get("quality_gain_threshold", 0.0)
    )
    selected = _select_profile(
        selectable, priority, quality_tolerance
    )
    diagnostics = [
        simulate_profile(repository, requests, config, profile, False)
        for profile in diagnostic_profiles
    ]
    selected_profile = next(
        profile for profile in profiles if profile["name"] == selected["name"]
    )
    accepted = next(
        profile for profile in matrix if profile["name"] == selected["name"]
    )
    selected_admitted = set(accepted["admitted_requests"])
    decisions: list[dict[str, Any]] = []
    compact = next(
        (profile for profile in matrix if profile["name"] == "compact"),
        matrix[0],
    )
    if selected["name"] != compact["name"]:
        decisions.append(
            {
                "conflict": "accuracy_vs_comparison_cost",
                "winner": "accuracy" if priority[0] == "quality" else priority[0],
                "selected": selected["name"],
                "alternative": compact["name"],
                "basis": (
                    "Selected profile offers quality "
                    f"{selected['quality']} over {compact['quality']} while "
                    "honoring the declared hard budgets."
                ),
                "affected_requests": sorted(selected_admitted),
                "metrics": {
                    "selected_quality": selected["quality"],
                    "alternative_quality": compact["quality"],
                    "selected_candidate_limit": selected["candidate_limit"],
                    "alternative_candidate_limit": compact["candidate_limit"],
                },
            }
        )
        decisions.append(
            {
                "conflict": "concurrency_versus_memory",
                "winner": "quality" if priority[0] == "quality" else priority[0],
                "selected": selected["name"],
                "alternative": compact["name"],
                "basis": (
                    "Higher concurrency lowers projected makespan "
                    f"({selected['makespan_ms']}ms vs "
                    f"{compact['makespan_ms']}ms) but raises projected "
                    f"memory peak ({selected['memory_peak_bytes']}B vs "
                    f"{compact['memory_peak_bytes']}B); priority order "
                    f"{priority} resolved the trade-off."
                ),
                "affected_requests": sorted(selected_admitted),
                "metrics": {
                    "selected_makespan_ms": selected["makespan_ms"],
                    "alternative_makespan_ms": compact["makespan_ms"],
                    "selected_memory_peak_bytes": selected[
                        "memory_peak_bytes"
                    ],
                    "alternative_memory_peak_bytes": compact[
                        "memory_peak_bytes"
                    ],
                },
            }
        )
    for diagnostic in diagnostics:
        time_rejected = [
            item["unit_id"]
            for item in diagnostic["rejected_requests"]
            if "time" in item["violations"]
        ]
        expansion_gain = (
            diagnostic["tool_expansion"] - selected["tool_expansion"]
        )
        if diagnostic["quality"] > selected["quality"] or time_rejected:
            decisions.append(
                {
                    "conflict": "tool_expansion_versus_round_time",
                    "winner": "time_budget" if time_rejected else "quality",
                    "selected": selected["name"],
                    "alternative": diagnostic["name"],
                    "basis": (
                        "Diagnostic profile adds "
                        f"{expansion_gain} evidence expansions; it is not "
                        "activated because its projected admission pattern "
                        f"rejects time-budget requests: {time_rejected}."
                    ),
                    "affected_requests": sorted(
                        set(time_rejected)
                        | (
                            selected_admitted
                            if diagnostic["quality"] > selected["quality"]
                            else set()
                        )
                    ),
                    "metrics": {
                        "diagnostic_quality": diagnostic["quality"],
                        "selected_quality": selected["quality"],
                        "diagnostic_admitted_time_ms": diagnostic[
                            "admitted_time_ms"
                        ],
                        "time_budget_ms": config["scheduling"][
                            "time_budget_ms"
                        ],
                    },
                }
            )
    if not decisions:
        decisions.append(
            {
                "conflict": "none",
                "winner": "budget_compliant_profile",
                "selected": selected["name"],
                "alternative": None,
                "basis": "No measurable trade-off exceeded the configured threshold.",
                "affected_requests": sorted(selected_admitted),
                "metrics": {},
            }
        )
    effective = apply_profile(config, selected_profile)
    log = {
        "priority_order": priority,
        "selected_profile": selected["name"],
        "matrix": matrix,
        "diagnostic_matrix": diagnostics,
        "decisions": decisions,
        "selection_rule": (
            "lexicographic over the declared priority_order, with "
            f"quality_gain_threshold={quality_tolerance}"
        ),
    }
    return effective, log

