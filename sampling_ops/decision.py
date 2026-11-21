"""Reproducible scale/resource route decision and conflict adjudication."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import numpy as np

from .errors import fail
from .metrics import dtype_itemsize, effective_chunk_size, route_estimates
from .types import ROUTES, normalize_route, normalize_tie_policy, resolve_dtype


SCHEMA_VERSION = "1.0"

DEFAULT_RESOURCE_PROFILE: dict[str, Any] = {
    "max_compare": 50_000_000,
    "max_memory_bytes": 64 * 1024 * 1024,
    "compare_soft_ratio": 0.80,
    "memory_soft_ratio": 0.80,
    "chunk_size": 4096,
}

NAMED_RESOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "default": dict(DEFAULT_RESOURCE_PROFILE),
    "small": {
        "max_compare": 2_000_000,
        "max_memory_bytes": 8 * 1024 * 1024,
        "compare_soft_ratio": 0.80,
        "memory_soft_ratio": 0.80,
        "chunk_size": 1024,
    },
    "balanced": dict(DEFAULT_RESOURCE_PROFILE),
    "compare_tight": {
        "max_compare": 2_000_000,
        "max_memory_bytes": 64 * 1024 * 1024,
        "compare_soft_ratio": 0.70,
        "memory_soft_ratio": 0.80,
        "chunk_size": 4096,
    },
    "memory_tight": {
        "max_compare": 50_000_000,
        "max_memory_bytes": 2 * 1024 * 1024,
        "compare_soft_ratio": 0.80,
        "memory_soft_ratio": 0.70,
        "chunk_size": 2048,
    },
    "low_memory": {
        "max_compare": 50_000_000,
        "max_memory_bytes": 2 * 1024 * 1024,
        "compare_soft_ratio": 0.80,
        "memory_soft_ratio": 0.70,
        "chunk_size": 2048,
    },
    "low_compare": {
        "max_compare": 2_000_000,
        "max_memory_bytes": 64 * 1024 * 1024,
        "compare_soft_ratio": 0.70,
        "memory_soft_ratio": 0.80,
        "chunk_size": 4096,
    },
}

ROUTE_PRIORITY = [
    "P0: preserve exact candidates and the declared tie policy",
    "P1: reject routes that exceed a hard comparison or memory limit",
    "P2: avoid full_sort when estimated comparisons exceed the comparison soft limit",
    "P3: avoid full_sort when estimated residency exceeds the memory soft limit",
    "P4: for large-k memory pressure prefer chunked bounded blocks over full_sort",
    "P5: use deterministic route-name ordering to break remaining ties",
]

ROUTE_ORDER = {route: index for index, route in enumerate(ROUTES)}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_resource_profile(profile: Any, *, case_id: str | None) -> dict[str, Any]:
    if profile is None:
        profile = {}
    if isinstance(profile, str):
        profile = NAMED_RESOURCE_PROFILES.get(profile)
        if profile is None:
            fail(
                "E_RESOURCE_PROFILE_INVALID",
                "unknown named resource profile",
                field="resource_profile",
                case_id=case_id,
                details={"actual": profile, "known": sorted(NAMED_RESOURCE_PROFILES)},
            )
    if not isinstance(profile, Mapping):
        fail(
            "E_RESOURCE_PROFILE_INVALID",
            "resource_profile must be an object or a known profile name",
            field="resource_profile",
            case_id=case_id,
            details={"actual_type": type(profile).__name__},
        )
    merged = dict(DEFAULT_RESOURCE_PROFILE)
    aliases = {
        "comparison_budget": "max_compare",
        "max_comparisons": "max_compare",
        "comparison_limit": "max_compare",
        "compare_limit": "max_compare",
        "compare_budget": "max_compare",
        "memory_budget_bytes": "max_memory_bytes",
        "max_resident_bytes": "max_memory_bytes",
        "memory_limit_bytes": "max_memory_bytes",
        "memory_limit": "max_memory_bytes",
        "max_memory": "max_memory_bytes",
        "max_bytes": "max_memory_bytes",
        "chunk_elements": "chunk_size",
    }
    for key, value in profile.items():
        merged[aliases.get(key, key)] = value
    for key in ("max_compare", "max_memory_bytes", "chunk_size"):
        try:
            converted = int(merged[key])
        except (TypeError, ValueError, OverflowError):
            fail(
                "E_RESOURCE_PROFILE_INVALID",
                f"resource_profile.{key} must be a positive integer",
                field=f"resource_profile.{key}",
                case_id=case_id,
            )
        if isinstance(merged[key], bool) or converted <= 0:
            fail(
                "E_RESOURCE_PROFILE_INVALID",
                f"resource_profile.{key} must be a positive integer",
                field=f"resource_profile.{key}",
                case_id=case_id,
            )
        merged[key] = converted
    for key in ("compare_soft_ratio", "memory_soft_ratio"):
        try:
            converted_float = float(merged[key])
        except (TypeError, ValueError, OverflowError):
            fail(
                "E_RESOURCE_PROFILE_INVALID",
                f"resource_profile.{key} must be in (0, 1]",
                field=f"resource_profile.{key}",
                case_id=case_id,
            )
        if not np.isfinite(converted_float) or not (0 < converted_float <= 1):
            fail(
                "E_RESOURCE_PROFILE_INVALID",
                f"resource_profile.{key} must be in (0, 1]",
                field=f"resource_profile.{key}",
                case_id=case_id,
            )
        merged[key] = converted_float
    return merged


def _normalized_violation(estimate: dict[str, Any], resources: dict[str, Any]) -> float:
    compare_ratio = estimate["comparisons"] / resources["max_compare"]
    memory_ratio = estimate["peak_memory_bytes"] / resources["max_memory_bytes"]
    return float(max(compare_ratio, memory_ratio))


def _route_would_fit(
    estimate: dict[str, Any],
    resources: dict[str, Any],
) -> bool:
    return (
        estimate["comparisons"] <= resources["max_compare"]
        and estimate["peak_memory_bytes"] <= resources["max_memory_bytes"]
    )


def _conflict_records(
    selected: str,
    estimates: dict[str, dict[str, Any]],
    resources: dict[str, Any],
    *,
    compare_pressure: bool,
    memory_pressure: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if selected == "heap_select":
        records.append(
            {
                "conflict": "compare_budget_vs_full_sort",
                "chosen_route": "heap_select",
                "rejected_routes": ["full_sort"],
                "reason_code": "FULL_SORT_NEAR_COMPARE_BUDGET",
                "comparison_saving": int(
                    estimates["full_sort"]["comparisons"] - estimates["heap_select"]["comparisons"]
                ),
                "reason": "Heap selection keeps a bounded k-candidate set and avoids a full row sort.",
            }
        )
    if selected == "chunked_two_stage":
        records.append(
            {
                "conflict": "memory_bound_vs_chunk_boundary_overhead",
                "chosen_route": "chunked_two_stage",
                "rejected_routes": ["full_sort"],
                "reason_code": "MEMORY_BOUND_WITH_CHUNK_MERGE_OVERHEAD",
                "peak_memory_saving": int(
                    estimates["full_sort"]["peak_memory_bytes"]
                    - estimates["chunked_two_stage"]["peak_memory_bytes"]
                ),
                "boundary_overhead_comparisons": int(
                    estimates["chunked_two_stage"]["boundary_overhead"]
                ),
                "reason": (
                    "Two-stage streaming selection bounds the working set by chunk_size; "
                    "the merge boundary adds comparisons but does not alter candidate correctness."
                ),
            }
        )
    if compare_pressure:
        records.append(
            {
                "conflict": "latency_vs_comparison_budget",
                "chosen_route": selected,
                "rejected_routes": ["full_sort"],
                "reason_code": "COMPARE_SOFT_LIMIT_APPLIED",
                "soft_limit": int(resources["max_compare"] * resources["compare_soft_ratio"]),
                "reason": "The full-sort estimate is too close to the hard comparison budget.",
            }
        )
    if memory_pressure:
        records.append(
            {
                "conflict": "resident_memory_vs_full_sort",
                "chosen_route": selected,
                "rejected_routes": ["full_sort"],
                "reason_code": "MEMORY_SOFT_LIMIT_APPLIED",
                "soft_limit": int(resources["max_memory_bytes"] * resources["memory_soft_ratio"]),
                "reason": "The full-sort working set is too close to the hard residency budget.",
            }
        )
    return records


def decide_route(
    vocab_size: int,
    batch_size: int,
    k_value: int,
    dtype: str | np.dtype,
    *,
    k_mode: str = "auto",
    tie_policy: str = "index_asc",
    resource_profile: Any = None,
    case_id: str | None = None,
) -> dict[str, Any]:
    """Select a route from size observations and resource estimates.

    The decision is recorded before the selected route is executed.  No
    post-execution route substitution is performed.  If every route exceeds a
    hard limit, the least-violating route is still selected and marked as
    degraded so callers receive auditable candidates and samples.
    """

    try:
        n = int(vocab_size)
        b = int(batch_size)
        k = int(k_value)
    except (TypeError, ValueError, OverflowError):
        fail(
            "E_SHAPE_INVALID",
            "vocab_size, batch_size, and k_value must be integers",
            field="vocab_size",
            case_id=case_id,
        )
    for name, raw_value in (
        ("vocab_size", vocab_size),
        ("batch_size", batch_size),
        ("k_value", k_value),
    ):
        if isinstance(raw_value, (float, np.floating)) and not float(raw_value).is_integer():
            fail(
                "E_K_INVALID" if name == "k_value" else "E_SHAPE_INVALID",
                f"{name} must be an integer",
                field=name,
                case_id=case_id,
            )
    if n <= 0 or b <= 0 or k <= 0 or k > n:
        fail(
            "E_K_INVALID" if k <= 0 or k > n else "E_SHAPE_INVALID",
            "invalid size observation",
            field="k_value" if k <= 0 or k > n else "shape",
            case_id=case_id,
            details={"vocab_size": n, "batch_size": b, "k_value": k},
        )
    resolved_dtype = resolve_dtype(dtype, case_id=case_id)
    normalized_tie = normalize_tie_policy(tie_policy, case_id=case_id)
    resources = parse_resource_profile(resource_profile, case_id=case_id)
    requested_route = normalize_route(k_mode, field_name="k_mode", case_id=case_id)
    chunk_size = effective_chunk_size(n, k, resources["chunk_size"])
    estimates = route_estimates(
        n,
        b,
        k,
        dtype_itemsize(resolved_dtype),
        chunk_size,
    )

    compare_soft_limit = int(resources["max_compare"] * resources["compare_soft_ratio"])
    memory_soft_limit = int(resources["max_memory_bytes"] * resources["memory_soft_ratio"])
    compare_pressure = estimates["full_sort"]["comparisons"] > compare_soft_limit
    memory_pressure = estimates["full_sort"]["peak_memory_bytes"] > memory_soft_limit

    feasible_routes = [route for route in ROUTES if _route_would_fit(estimates[route], resources)]
    reasons: list[str] = []
    degraded = False
    degraded_category: str | None = None
    route_source = "decision"

    if requested_route != "auto":
        selected = requested_route
        route_source = "k_mode_forced"
        reasons.append("ROUTE_FORCED_BY_K_MODE")
        if selected in feasible_routes:
            reasons.append("FORCED_ROUTE_WITHIN_HARD_LIMITS")
        else:
            degraded = True
            degraded_category = "RESOURCE_BUDGET_OVERRUN"
            reasons.append("FORCED_ROUTE_EXCEEDS_HARD_LIMITS")
    elif not feasible_routes:
        selected = min(
            ROUTES,
            key=lambda route: (
                _normalized_violation(estimates[route], resources),
                ROUTE_ORDER[route],
            ),
        )
        degraded = True
        degraded_category = "RESOURCE_BUDGET_OVERRUN"
        reasons.extend(["NO_ROUTE_WITHIN_HARD_LIMITS", "LEAST_VIOLATION_ROUTE_SELECTED"])
    elif compare_pressure:
        if memory_pressure and k >= 256 and n >= 8192 and "chunked_two_stage" in feasible_routes:
            selected = "chunked_two_stage"
            degraded = True
            degraded_category = "MEMORY_BUDGET_ADAPTATION"
            reasons.extend(
                [
                    "COMPARE_PRESSURE_DETECTED",
                    "LARGE_K_MEMORY_PRESSURE_PREFERS_BOUNDED_CHUNKS",
                ]
            )
        elif "heap_select" in feasible_routes:
            selected = "heap_select"
            degraded = True
            degraded_category = "COMPARE_BUDGET_ADAPTATION"
            reasons.extend(["COMPARE_PRESSURE_DETECTED", "HEAP_SELECT_WITHIN_BUDGET"])
        elif "chunked_two_stage" in feasible_routes:
            selected = "chunked_two_stage"
            degraded = True
            degraded_category = "COMPARE_BUDGET_ADAPTATION"
            reasons.extend(["COMPARE_PRESSURE_DETECTED", "CHUNKED_TWO_STAGE_WITHIN_BUDGET"])
        elif "full_sort" in feasible_routes:
            selected = "full_sort"
            reasons.append("FULL_SORT_IS_ONLY_HARD_FEASIBLE_ROUTE")
        else:
            raise AssertionError("feasible route list was non-empty")
    elif memory_pressure:
        if k >= 256 and n >= 8192 and "chunked_two_stage" in feasible_routes:
            selected = "chunked_two_stage"
            degraded = True
            degraded_category = "MEMORY_BUDGET_ADAPTATION"
            reasons.extend(
                [
                    "MEMORY_PRESSURE_DETECTED",
                    "LARGE_K_MEMORY_PRESSURE_PREFERS_BOUNDED_CHUNKS",
                ]
            )
        elif "heap_select" in feasible_routes:
            selected = "heap_select"
            degraded = True
            degraded_category = "MEMORY_BUDGET_ADAPTATION"
            reasons.extend(["MEMORY_PRESSURE_DETECTED", "SMALL_K_HEAP_SELECT_WITHIN_BUDGET"])
        elif "chunked_two_stage" in feasible_routes:
            selected = "chunked_two_stage"
            degraded = True
            degraded_category = "MEMORY_BUDGET_ADAPTATION"
            reasons.extend(["MEMORY_PRESSURE_DETECTED", "CHUNKED_TWO_STAGE_WITHIN_BUDGET"])
        elif "full_sort" in feasible_routes:
            selected = "full_sort"
            reasons.append("FULL_SORT_IS_ONLY_HARD_FEASIBLE_ROUTE")
        else:
            raise AssertionError("feasible route list was non-empty")
    elif n <= 8192 and "full_sort" in feasible_routes:
        selected = "full_sort"
        reasons.append("SMALL_OR_MEDIUM_SCALE_FULL_SORT")
    elif "heap_select" in feasible_routes:
        selected = "heap_select"
        reasons.append("LARGE_SCALE_HEAP_SELECT")
    elif "chunked_two_stage" in feasible_routes:
        selected = "chunked_two_stage"
        reasons.append("LARGE_SCALE_CHUNKED_TWO_STAGE")
    elif "full_sort" in feasible_routes:
        selected = "full_sort"
        reasons.append("FULL_SORT_IS_ONLY_HARD_FEASIBLE_ROUTE")
    else:
        raise AssertionError("feasible route list was non-empty")

    selected_estimate = estimates[selected]
    if not _route_would_fit(selected_estimate, resources):
        degraded = True
        if degraded_category is None:
            degraded_category = "RESOURCE_BUDGET_OVERRUN"
        reasons.append("SELECTED_ROUTE_EXCEEDS_HARD_LIMIT_BUT_REMAINS_AUDITABLE")

    conflict_records = _conflict_records(
        selected,
        estimates,
        resources,
        compare_pressure=compare_pressure,
        memory_pressure=memory_pressure,
    )
    selected_within_limits = _route_would_fit(selected_estimate, resources)
    observation = {
        "vocab_size": n,
        "batch_size": b,
        "k_value": k,
        "dtype": str(resolved_dtype),
        "dtype_itemsize": dtype_itemsize(resolved_dtype),
        "tie_policy": normalized_tie,
        "k_mode": requested_route,
        "chunk_size": chunk_size,
    }
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "observation": observation,
        "resource_limits": dict(resources),
        "experimental_soft_limits": {
            "compare": compare_soft_limit,
            "memory_bytes": memory_soft_limit,
        },
        "estimates": estimates,
        "route_priority": ROUTE_PRIORITY,
        "feasible_routes": feasible_routes,
        "selected_route": selected,
        "route_source": route_source,
        "selected_within_limits": selected_within_limits,
        "selected_exceeded_metrics": [
            metric
            for metric, limit in (
                ("comparisons", resources["max_compare"]),
                ("peak_memory_bytes", resources["max_memory_bytes"]),
            )
            if selected_estimate[metric] > limit
        ],
        "degraded": bool(degraded),
        "degraded_category": degraded_category,
        "reasons": reasons,
        "conflict_resolution": conflict_records,
    }
    record["canonical_bytes_sha256"] = sha256_bytes(canonical_bytes(record))
    return record


def decision_bytes(record: Mapping[str, Any]) -> bytes:
    """Return canonical bytes for byte-for-byte decision comparisons."""

    stable = dict(record)
    stable.pop("canonical_bytes_sha256", None)
    return canonical_bytes(stable)

