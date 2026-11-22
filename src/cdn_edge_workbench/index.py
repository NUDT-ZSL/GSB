from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from typing import Any, Iterable

from .models import CacheEntry, FreshnessResult, NodeConfig, Policy
from .errors import BusinessError
from .validation import canonical_json, parse_etag, sha256_hex


_FORMAT_PRIORITY = {"v1": 1, "v2": 2, "origin-200": 3, "origin-304": 2}
_DIFF_FIELDS = (
    "last_verified_clock",
    "content_version",
    "cache_generation",
    "source_version",
    "content_digest",
    "etag",
    "expires_at_clock",
)


def format_priority(source_version: str) -> int:
    return _FORMAT_PRIORITY.get(source_version, 0)


def candidate_rank_key(entry: CacheEntry) -> tuple[Any, ...]:
    return (
        -entry.last_verified_clock,
        -entry.content_version,
        -entry.cache_generation,
        -format_priority(entry.source_version),
        entry.node,
        entry.path,
        entry.content_digest,
        entry.etag,
        canonical_json(entry.raw_time),
    )


def candidate_stable_key(entry: CacheEntry) -> str:
    return canonical_json(
        {
            "candidate": entry.candidate_dict(),
            "rank": list(candidate_rank_key(entry)[:6]),
        }
    )


def _comparison_signature(entry: CacheEntry) -> tuple[Any, ...]:
    return (
        entry.last_verified_clock,
        entry.content_version,
        entry.cache_generation,
        format_priority(entry.source_version),
        entry.content_digest,
        entry.etag,
        entry.expires_at_clock,
    )


def _first_basis(winner: CacheEntry, others: list[CacheEntry]) -> list[dict[str, Any]]:
    priorities = (
        "last_verified_clock",
        "content_version",
        "cache_generation",
        "source_format_priority",
        "content_digest",
        "etag",
        "expires_at_clock",
    )
    basis: list[dict[str, Any]] = []
    for field in priorities:
        if field == "source_format_priority":
            winner_value = format_priority(winner.source_version)
            differing = any(format_priority(item.source_version) != winner_value for item in others)
        else:
            winner_value = getattr(winner, field)
            differing = any(getattr(item, field) != winner_value for item in others)
        if differing:
            values = sorted(
                {
                    format_priority(item.source_version)
                    if field == "source_format_priority"
                    else getattr(item, field)
                    for item in [winner, *others]
                },
                key=lambda value: (str(type(value)), value),
            )
            basis.append(
                {
                    "criterion": field,
                    "winner_value": winner_value,
                    "observed_values": values,
                    "direction": "highest_wins",
                }
            )
            break
    return basis


def _difference_summary(winner: CacheEntry, loser: CacheEntry) -> dict[str, Any]:
    differences: dict[str, Any] = {}
    for field in _DIFF_FIELDS:
        winner_value = getattr(winner, field)
        loser_value = getattr(loser, field)
        if winner_value != loser_value:
            differences[field] = {"winner": winner_value, "loser": loser_value}
    return differences


def arbitrate_candidates(
    scope: str,
    target: str,
    candidates: Iterable[CacheEntry],
) -> dict[str, Any]:
    ordered = sorted(candidates, key=candidate_rank_key)
    stable_ordered = sorted(ordered, key=candidate_stable_key)
    winner = ordered[0]
    others = ordered[1:]
    signatures = {_comparison_signature(item) for item in ordered}
    if len(ordered) > 1 and len(signatures) == 1:
        status = "consistent"
        basis = [
            {
                "criterion": "all_candidates_equal",
                "winner_value": _comparison_signature(winner),
                "observed_values": sorted({_comparison_signature(item) for item in ordered}),
                "direction": "no_difference",
            }
        ]
    elif len(ordered) == 1:
        status = "consistent"
        basis = [
            {
                "criterion": "single_candidate",
                "winner_value": winner.key_text,
                "observed_values": [winner.key_text],
                "direction": "no_comparison",
            }
        ]
    else:
        versions = {item.content_version for item in ordered}
        digests = {item.content_digest for item in ordered}
        etags = {item.etag for item in ordered}
        if len(versions) > 1:
            status = "version_divergence"
        elif len(digests) > 1:
            status = "digest_conflict"
        elif len(etags) > 1:
            status = "etag_conflict"
        else:
            status = "metadata_divergence"
        basis = _first_basis(winner, others)
    differences = {
        item.key_text: _difference_summary(winner, item)
        for item in others
        if _difference_summary(winner, item)
    }
    payload = {
        "scope": scope,
        "target": target,
        "status": status,
        "winner": winner.candidate_dict(),
        "candidates": [item.candidate_dict() for item in stable_ordered],
        "basis": basis,
        "differences": differences,
    }
    decision_hash = sha256_hex(canonical_json(payload))
    payload["decision_hash"] = decision_hash
    payload["input_count"] = len(ordered)
    return payload


def resolve_key_winners(entries: Iterable[CacheEntry]) -> dict[tuple[str, str], CacheEntry]:
    groups: dict[tuple[str, str], list[CacheEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.key].append(entry)
    return {
        key: sorted(values, key=candidate_rank_key)[0]
        for key, values in sorted(groups.items())
    }


def arbitrate_snapshot_entries(entries: Iterable[CacheEntry]) -> list[dict[str, Any]]:
    all_entries = list(entries)
    key_groups: dict[tuple[str, str], list[CacheEntry]] = defaultdict(list)
    for entry in all_entries:
        key_groups[entry.key].append(entry)
    decisions: list[dict[str, Any]] = []
    key_winners: dict[tuple[str, str], CacheEntry] = {}
    for key, candidates in sorted(key_groups.items()):
        ordered = sorted(candidates, key=candidate_rank_key)
        key_winners[key] = ordered[0]
        if len(ordered) > 1:
            decisions.append(
                arbitrate_candidates("key", f"{key[0]}:{key[1]}", ordered)
            )
    path_groups: dict[str, list[CacheEntry]] = defaultdict(list)
    for entry in key_winners.values():
        path_groups[entry.path].append(entry)
    for path, candidates in sorted(path_groups.items()):
        if len(candidates) > 1:
            decisions.append(arbitrate_candidates("path", path, candidates))
    return sorted(decisions, key=lambda item: (item["scope"], item["target"], item["decision_hash"]))


def freshness_at(entry: CacheEntry, logical_clock: int, tolerance: int) -> FreshnessResult:
    remaining = entry.expires_at_clock - logical_clock
    if remaining > tolerance:
        status = "fresh"
    elif remaining < -tolerance:
        status = "expired"
    else:
        status = "boundary"
    return FreshnessResult(
        status=status,
        remaining_ticks=remaining,
        tolerance_ticks=tolerance,
        evaluated_at_clock=logical_clock,
        expires_at_clock=entry.expires_at_clock,
        conversion=entry.raw_time,
    )


def _bucket_rank(seed: str, digest: str) -> str:
    return hashlib.sha256(f"{seed}|{digest}".encode("utf-8")).hexdigest()


def sampled_buckets(
    entries: Iterable[CacheEntry],
    *,
    fraction: float,
    seed: str,
) -> tuple[list[str], list[str], list[CacheEntry]]:
    buckets: dict[str, list[CacheEntry]] = defaultdict(list)
    for entry in entries:
        buckets[entry.content_digest].append(entry)
    bucket_keys = sorted(buckets)
    if not bucket_keys:
        return [], [], []
    sampled_count = max(1, math.ceil(len(bucket_keys) * fraction))
    ranked = sorted(bucket_keys, key=lambda digest: (_bucket_rank(seed, digest), digest))
    selected = sorted(ranked[:sampled_count])
    sampled_entries = sorted(
        [entry for digest in selected for entry in buckets[digest]],
        key=candidate_rank_key,
    )
    return bucket_keys, selected, sampled_entries


def choose_validation_mode(
    *,
    scope: str,
    target: str,
    item_count: int,
    candidate_count: int,
    threshold: int,
    fraction: float,
    seed: str,
    entries: list[CacheEntry],
) -> tuple[dict[str, Any], list[CacheEntry]]:
    triggered = item_count > threshold or candidate_count > threshold
    if triggered:
        mode = "sampled_digest_buckets"
        bucket_keys, selected, selected_entries = sampled_buckets(
            entries,
            fraction=fraction,
            seed=seed,
        )
        buckets_total = len(bucket_keys)
        buckets_sampled = len(selected)
        bound = 0.0 if buckets_total == 0 else 1.0 - (buckets_sampled / buckets_total)
        reason = (
            "node entry count or path candidate count exceeds comparison_threshold; "
            "a complete per-entry pass exceeds the configured validation budget"
        )
        rejected_mode = "exact_per_entry"
        rejected_reason = "exact comparison rejected because the configured scale threshold was exceeded"
        observation_source = (
            "content_digest buckets observed in the loaded v1/v2 candidates and active "
            "index; deterministic SHA-256 bucket ranking, no wall-clock or random source"
        )
    else:
        mode = "exact_per_entry"
        selected_entries = sorted(entries, key=candidate_rank_key)
        bucket_keys = sorted({entry.content_digest for entry in entries})
        buckets_total = len(bucket_keys)
        buckets_sampled = buckets_total
        bound = 0.0
        reason = "item and candidate counts are within comparison_threshold"
        rejected_mode = "sampled_digest_buckets"
        rejected_reason = (
            "sampling rejected because exact comparison is affordable at this scale and "
            "introduces avoidable false negatives"
        )
        observation_source = "complete loaded v1/v2 candidate and active index observation"
    record = {
        "scope": scope,
        "target": target,
        "mode": mode,
        "triggered": triggered,
        "threshold": threshold,
        "item_count": item_count,
        "candidate_count": candidate_count,
        "trigger_condition": (
            "item_count > threshold OR candidate_count > threshold"
            if triggered
            else "item_count <= threshold AND candidate_count <= threshold"
        ),
        "reason": reason,
        "rejected_mode": rejected_mode,
        "rejected_reason": rejected_reason,
        "sample_fraction": fraction,
        "sample_seed": seed,
        "digest_buckets_total": buckets_total,
        "digest_buckets_sampled": buckets_sampled,
        "sampled_bucket_keys": selected if triggered else bucket_keys,
        "items_checked": len(selected_entries),
        "estimated_false_negative_upper_bound": bound,
        "observation_source": observation_source,
    }
    return record, selected_entries


def verify_entries(entries: list[CacheEntry]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for entry in entries:
        try:
            _algorithm, etag_digest, _etag, _weak = parse_etag(
                entry.etag,
                declared_algorithm=entry.digest_algorithm,
                expected_digest=entry.content_digest,
            )
        except BusinessError:
            issues.append(
                {
                    "code": "E1008_ETAG_DIGEST_MISMATCH",
                    "node": entry.node,
                    "path": entry.path,
                    "detail": "ETag digest differs from indexed content digest",
                }
            )
        if entry.expires_at_clock < entry.last_verified_clock:
            issues.append(
                {
                    "code": "E1018_NON_MONOTONIC_TIME",
                    "node": entry.node,
                    "path": entry.path,
                    "detail": "expiry precedes last verification",
                }
            )
        if not entry.digest_algorithm:
            issues.append(
                {
                    "code": "E1007_ETAG_INVALID",
                    "node": entry.node,
                    "path": entry.path,
                    "detail": "digest algorithm is missing",
                }
            )
    return issues


def evict_node(
    node: NodeConfig,
    entries: Iterable[CacheEntry],
    *,
    logical_clock: int,
    tolerance: int,
) -> tuple[list[CacheEntry], list[dict[str, Any]], list[dict[str, Any]]]:
    node_entries = sorted(entries, key=candidate_rank_key)
    freshness = {
        entry.key: freshness_at(entry, logical_clock, tolerance).status for entry in node_entries
    }

    def victim_key(entry: CacheEntry) -> tuple[Any, ...]:
        expired_rank = 0 if freshness[entry.key] == "expired" else 1
        return (
            expired_rank,
            entry.last_verified_clock,
            entry.content_version,
            entry.cache_generation,
            entry.path,
            format_priority(entry.source_version),
            entry.content_digest,
        )

    ordered = sorted(node_entries, key=victim_key)
    surplus = max(0, len(ordered) - node.capacity)
    victims = ordered[:surplus]
    retained = sorted(ordered[surplus:], key=candidate_rank_key)
    victim_keys = {entry.key for entry in victims}
    records = [
        {
            "node": node.name,
            "path": entry.path,
            "cache_generation": entry.cache_generation,
            "content_version": entry.content_version,
            "reason": (
                "expired_first"
                if freshness[entry.key] == "expired"
                else "oldest_logical_clock_then_stable_order"
            ),
            "freshness_status": freshness[entry.key],
            "last_verified_clock": entry.last_verified_clock,
            "stable_order": [
                entry.path,
                entry.last_verified_clock,
                entry.cache_generation,
                format_priority(entry.source_version),
                entry.content_digest,
            ],
        }
        for entry in victims
    ]
    max_verified = max((entry.last_verified_clock for entry in retained), default=None)
    retention = [
        {
            "node": node.name,
            "path": entry.path,
            "cache_generation": entry.cache_generation,
            "reason": (
                "retained_within_capacity_and_not_selected_for_expiry"
                if entry.key not in victim_keys
                else "evicted"
            ),
            "freshness_status": freshness[entry.key],
            "last_verified_clock": entry.last_verified_clock,
            "is_most_recently_verified": entry.last_verified_clock == max_verified,
        }
        for entry in retained
    ]
    return retained, records, retention


def select_revalidation_entries(
    entries: Iterable[CacheEntry],
    *,
    nodes: dict[str, NodeConfig],
    logical_clock: int,
    tolerance: int,
    blocked_paths: set[str],
) -> tuple[list[CacheEntry], list[dict[str, Any]]]:
    by_node: dict[str, list[CacheEntry]] = defaultdict(list)
    for entry in entries:
        if entry.path in blocked_paths:
            continue
        freshness = freshness_at(entry, logical_clock, tolerance)
        if freshness.status == "fresh":
            continue
        by_node[entry.node].append(entry)
    selected: list[CacheEntry] = []
    skipped: list[dict[str, Any]] = []
    for node_name in sorted(by_node):
        candidates = sorted(
            by_node[node_name],
            key=lambda entry: (
                0 if freshness_at(entry, logical_clock, tolerance).status == "expired" else 1,
                entry.last_verified_clock,
                entry.path,
                entry.cache_generation,
            ),
        )
        budget = nodes[node_name].validation_budget
        selected.extend(candidates[:budget])
        for entry in candidates[budget:]:
            skipped.append(
                {
                    "node": entry.node,
                    "path": entry.path,
                    "cache_generation": entry.cache_generation,
                    "reason": "per_round_validation_budget_exhausted",
                    "budget": budget,
                }
            )
    return selected, skipped


def algorithm_records(
    entries: Iterable[CacheEntry],
    *,
    snapshot_candidates: Iterable[CacheEntry],
    policy: Policy,
) -> tuple[list[dict[str, Any]], list[CacheEntry], list[dict[str, Any]]]:
    active = list(entries)
    candidates = list(snapshot_candidates)
    by_node: dict[str, list[CacheEntry]] = defaultdict(list)
    for entry in active:
        by_node[entry.node].append(entry)
    candidate_counts: dict[str, int] = defaultdict(int)
    for entry in candidates:
        candidate_counts[entry.path] += 1
    records: list[dict[str, Any]] = []
    checked: list[CacheEntry] = []
    issues: list[dict[str, Any]] = []
    for node_name in sorted({entry.node for entry in candidates}):
        node_entries = sorted(by_node.get(node_name, []), key=candidate_rank_key)
        node_candidates = sorted(
            [entry for entry in candidates if entry.node == node_name],
            key=candidate_rank_key,
        )
        node_candidate_counts = defaultdict(int)
        for entry in node_candidates:
            node_candidate_counts[entry.path] += 1
        max_candidates = max(node_candidate_counts.values(), default=0)
        record, selected = choose_validation_mode(
            scope="node",
            target=node_name,
            item_count=len(node_entries),
            candidate_count=max_candidates,
            threshold=policy.comparison_threshold,
            fraction=policy.sample_fraction,
            seed=policy.sample_seed,
            entries=node_entries,
        )
        compare_entries = selected or node_entries
        issues.extend(verify_entries(compare_entries))
        checked.extend(compare_entries)
        records.append(record)
    path_groups: dict[str, list[CacheEntry]] = defaultdict(list)
    for entry in candidates:
        path_groups[entry.path].append(entry)
    for path in sorted(path_groups):
        group = path_groups[path]
        if len(group) <= 1:
            continue
        record, selected = choose_validation_mode(
            scope="path",
            target=path,
            item_count=len(group),
            candidate_count=len(group),
            threshold=policy.comparison_threshold,
            fraction=policy.sample_fraction,
            seed=policy.sample_seed,
            entries=group,
        )
        if record["mode"] == "sampled_digest_buckets":
            issues.extend(verify_entries(selected))
            checked.extend(selected)
            records.append(record)
    return records, checked, issues

