from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .models import Rule

SOURCE_PRIORITY = {"v1": 1, "v2": 2, "patch": 3}


def _priority(rule: Rule) -> int:
    return SOURCE_PRIORITY.get(rule.source, 0)


def _candidate_sort_key(rule: Rule) -> tuple[int, int, int, str]:
    return (
        rule.generation,
        rule.effective_at_ms,
        _priority(rule),
        rule.source_version,
    )


def _decision(chosen: Rule, candidates: list[Rule]) -> str:
    if len(candidates) == 1:
        return f"only-candidate:{chosen.source}"
    ranked = sorted(candidates, key=_candidate_sort_key, reverse=True)
    runner_up = ranked[1]
    if chosen.generation != runner_up.generation:
        return (
            f"generation:{chosen.source}({chosen.generation})>"
            f"{runner_up.source}({runner_up.generation})"
        )
    if chosen.effective_at_ms != runner_up.effective_at_ms:
        return (
            f"clock:{chosen.source}({chosen.effective_at_ms})>"
            f"{runner_up.source}({runner_up.effective_at_ms})"
        )
    if _priority(chosen) != _priority(runner_up):
        return (
            f"tie:source-priority:{chosen.source}>{runner_up.source}"
        )
    return (
        f"tie:source-version:{chosen.source}:{chosen.source_version}>"
        f"{runner_up.source}:{runner_up.source_version}"
    )


def merge_rule_candidates(
    candidate_groups: Iterable[Iterable[Rule]],
    *,
    tombstones: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge rule candidates without relying on input iteration order."""

    tombstones = tombstones or set()
    grouped: dict[str, list[Rule]] = defaultdict(list)
    for group in candidate_groups:
        for rule in group:
            grouped[rule.rule_key].append(rule)

    merged: dict[str, dict[str, Any]] = {}
    for rule_key in sorted(grouped):
        if rule_key in tombstones:
            continue
        candidates = grouped[rule_key]
        chosen = max(candidates, key=_candidate_sort_key)
        merged[rule_key] = {
            "rule_key": rule_key,
            "quota_bytes": chosen.quota_bytes,
            "effective_at_ms": chosen.effective_at_ms,
            "generation": chosen.generation,
            "source": chosen.source,
            "source_version": chosen.source_version,
            "decision": _decision(chosen, candidates),
            "conversion": chosen.conversion,
        }
    return merged


def changed_rule_keys(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[str]:
    keys = set(before) | set(after)
    changed: list[str] = []
    for key in sorted(keys):
        left = before.get(key)
        right = after.get(key)
        if left is None or right is None:
            changed.append(key)
            continue
        fields = ("quota_bytes", "effective_at_ms", "generation")
        if any(left.get(field) != right.get(field) for field in fields):
            changed.append(key)
    return changed


def rule_dir_id(rule_key: str) -> str | None:
    if rule_key.startswith("dir:") and len(rule_key) > 4:
        return rule_key[4:]
    return None
