from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import PolicyRecord


SOURCE_FORMAT_PRIORITY = {"v1": 1, "v2": 2}


@dataclass
class MergedPolicy:
    key: str
    selected: PolicyRecord
    candidates: list[PolicyRecord] = field(default_factory=list)
    decision: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from .models import jsonable

        return {
            "key": self.key,
            "selected": jsonable(self.selected),
            "candidates": jsonable(self.candidates),
            "decision": self.decision,
        }


def _rank(record: PolicyRecord) -> tuple[int, int, int, str]:
    return (
        record.effective_clock_ms,
        record.generation,
        SOURCE_FORMAT_PRIORITY.get(record.format_generation, 0),
        record.source,
    )


def merge_policy_records(records: list[PolicyRecord]) -> dict[str, MergedPolicy]:
    grouped: dict[str, list[PolicyRecord]] = {}
    for record in records:
        grouped.setdefault(record.key, []).append(record)

    merged: dict[str, MergedPolicy] = {}
    for key, candidates in grouped.items():
        ordered = sorted(candidates, key=_rank, reverse=True)
        selected = ordered[0]
        runner_up = ordered[1] if len(ordered) > 1 else None
        tie_reason: list[str] = []
        if runner_up is not None:
            if selected.effective_clock_ms == runner_up.effective_clock_ms:
                tie_reason.append("equal_effective_logical_clock")
                if selected.generation == runner_up.generation:
                    tie_reason.append("equal_generation_use_source_format_priority")
            if selected.format_generation == "v2" and runner_up.format_generation == "v1":
                tie_reason.append("v2_has_higher_source_format_priority")

        decision = {
            "rule": (
                "compare normalized effective logical clock descending, then policy generation "
                "descending, then source format priority v2>v1"
            ),
            "tie_breakers": tie_reason,
            "winner_source": selected.source,
            "winner_format": selected.format_generation,
            "winner_generation": selected.generation,
            "winner_declared_clock_ms": selected.declared_clock_ms,
            "winner_effective_clock_ms": selected.effective_clock_ms,
            "winner_original_time": {
                "kind": selected.original_time_kind,
                "value": selected.original_time_value,
            },
            "ranked_candidates": [
                {
                    "source": item.source,
                    "format": item.format_generation,
                    "source_version": item.source_version,
                    "generation": item.generation,
                    "declared_clock_ms": item.declared_clock_ms,
                    "effective_clock_ms": item.effective_clock_ms,
                    "original_time_kind": item.original_time_kind,
                    "original_time_value": item.original_time_value,
                    "format_priority": SOURCE_FORMAT_PRIORITY.get(item.format_generation, 0),
                }
                for item in ordered
            ],
        }
        merged[key] = MergedPolicy(
            key=key, selected=selected, candidates=ordered, decision=decision
        )
    return merged


def affected_function_closure(
    changed_keys: set[str],
    policy_view: dict[str, MergedPolicy],
    dependencies: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Return changed keys, directly affected functions, and reverse-dependency closure."""

    dependencies = dependencies or {}
    direct_functions: set[str] = set()
    key_to_functions: dict[str, set[str]] = {}

    for key, policy in policy_view.items():
        function_name = policy.selected.function_name
        if function_name:
            key_to_functions.setdefault(key, set()).add(function_name)

    for key in changed_keys:
        direct_functions.update(key_to_functions.get(key, set()))
        policy = policy_view.get(key)
        if policy and policy.selected.function_name:
            direct_functions.add(policy.selected.function_name)

    reverse_dependents: dict[str, set[str]] = {}
    for function_name, deps in dependencies.items():
        for dependency in deps:
            reverse_dependents.setdefault(dependency, set()).add(function_name)

    closure = set(direct_functions)
    queue = list(direct_functions)
    while queue:
        current = queue.pop()
        for dependent in reverse_dependents.get(current, set()):
            if dependent not in closure:
                closure.add(dependent)
                queue.append(dependent)

    cache_keys: set[str] = set()
    for key in changed_keys:
        policy = policy_view.get(key)
        if policy is None:
            continue
        cache_key = policy.selected.pool.get("cache_key")
        if cache_key:
            cache_keys.add(str(cache_key))
        for cache_key in policy.selected.raw.get("cache_keys", []) or []:
            cache_keys.add(str(cache_key))

    return {
        "changed_keys": sorted(changed_keys),
        "direct_functions": sorted(direct_functions),
        "affected_functions": sorted(closure),
        "cache_keys": sorted(cache_keys),
        "reverse_dependency_edges": {
            key: sorted(value) for key, value in sorted(reverse_dependents.items())
        },
    }
