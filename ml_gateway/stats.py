from __future__ import annotations

from typing import Any, Iterable

from .canonical import sha256_json
from .models import BackendState, DeclarationBundle


def empty_stats(
    bundle: DeclarationBundle | None = None,
    states: dict[str, BackendState] | None = None,
) -> dict[str, Any]:
    rules = [] if bundle is None else sorted(rule.rule_id for rule in bundle.rules)
    backends = [] if states is None else sorted(states)
    return {
        "effective_requests": 0,
        "rule_hits": {rule_id: 0 for rule_id in rules},
        "backend_hits": {backend_id: 0 for backend_id in backends},
        "fingerprint_hits": {},
        "cache": {},
        "output_index": sha256_json([]),
    }


def compute_stats(
    ledger: list[dict[str, Any]],
    bundle: DeclarationBundle,
    states: dict[str, BackendState],
) -> dict[str, Any]:
    rule_hits = {rule.rule_id: 0 for rule in sorted(bundle.rules, key=lambda item: item.rule_id)}
    backend_hits = {backend_id: 0 for backend_id in sorted(states)}
    fingerprint_hits: dict[str, int] = {}
    cache_groups: dict[str, list[tuple[str, str | None]]] = {}
    effective_records: list[dict[str, Any]] = []

    for entry in sorted(
        ledger,
        key=lambda item: (
            item["request_ordinal"],
            item["attempt_ordinal"],
            item["entry_id"],
        ),
    ):
        if not entry.get("effective"):
            continue
        rule_id = entry["rule_id"]
        backend_id = entry["backend_id"]
        fingerprint = entry["bound_fingerprint"]
        rule_hits[rule_id] = rule_hits.get(rule_id, 0) + 1
        backend_hits[backend_id] = backend_hits.get(backend_id, 0) + 1
        fingerprint_key = f"{backend_id}@{fingerprint}"
        fingerprint_hits[fingerprint_key] = fingerprint_hits.get(fingerprint_key, 0) + 1
        cache_key = f"{rule_id}|{backend_id}|{fingerprint}"
        cache_groups.setdefault(cache_key, []).append(
            (entry["request_id"], entry.get("output_summary"))
        )
        effective_records.append(
            {
                "backend_id": backend_id,
                "bound_fingerprint": fingerprint,
                "output_summary": entry.get("output_summary"),
                "request_id": entry["request_id"],
                "rule_id": rule_id,
            }
        )

    cache: dict[str, dict[str, Any]] = {}
    for key in sorted(cache_groups):
        records = sorted(cache_groups[key])
        cache[key] = {
            "count": len(records),
            "output_digest": sha256_json(records),
        }
    return {
        "effective_requests": len(effective_records),
        "rule_hits": dict(sorted(rule_hits.items())),
        "backend_hits": dict(sorted(backend_hits.items())),
        "fingerprint_hits": dict(sorted(fingerprint_hits.items())),
        "cache": cache,
        "output_index": sha256_json(effective_records),
    }


def flatten_stats(stats: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {
        "global.effective_requests": stats.get("effective_requests", 0),
        "global.output_index": stats.get("output_index"),
    }
    for section in ("rule_hits", "backend_hits", "fingerprint_hits", "cache"):
        for key, value in sorted(stats.get(section, {}).items()):
            flat[f"{section}:{key}"] = value
    return flat


def _key_affected(key: str, affected_rules: set[str], affected_backends: set[str]) -> bool:
    if key.startswith("global."):
        return True
    if key.startswith("rule_hits:"):
        return key.split(":", 1)[1] in affected_rules
    if key.startswith("backend_hits:"):
        return key.split(":", 1)[1] in affected_backends
    if key.startswith("fingerprint_hits:"):
        backend_id = key.split(":", 1)[1].split("@", 1)[0]
        return backend_id in affected_backends
    if key.startswith("cache:"):
        parts = key.split(":", 1)[1].split("|")
        if len(parts) >= 2:
            return parts[0] in affected_rules or parts[1] in affected_backends
    return False


def diff_stats(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    affected_rules: Iterable[str],
    affected_backends: Iterable[str],
) -> dict[str, Any]:
    old_flat = {} if not previous else flatten_stats(previous)
    new_flat = flatten_stats(current)
    affected_rule_set = set(affected_rules)
    affected_backend_set = set(affected_backends)
    all_keys = sorted(set(old_flat) | set(new_flat))
    changed_keys = sorted(
        key for key in all_keys if old_flat.get(key) != new_flat.get(key)
    )
    affected_keys = sorted(
        key
        for key in all_keys
        if _key_affected(key, affected_rule_set, affected_backend_set)
    )
    recalculated = sorted(set(affected_keys) | set(changed_keys))
    unchanged = sorted(
        key
        for key in all_keys
        if key not in set(recalculated)
        and old_flat.get(key) == new_flat.get(key)
    )
    unexpected = sorted(
        key
        for key in changed_keys
        if key not in set(affected_keys) and not key.startswith("global.")
    )
    return {
        "affected_keys": affected_keys,
        "changed_keys": changed_keys,
        "recalculated_keys": recalculated,
        "unchanged_keys": unchanged,
        "unexpected_changed_keys": unexpected,
    }


def stats_summary(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend_hits": dict(sorted(stats.get("backend_hits", {}).items())),
        "cache_entry_count": len(stats.get("cache", {})),
        "effective_requests": int(stats.get("effective_requests", 0)),
        "fingerprint_hits": dict(sorted(stats.get("fingerprint_hits", {}).items())),
        "output_index": stats.get("output_index"),
        "rule_hits": dict(sorted(stats.get("rule_hits", {}).items())),
    }
