from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from .models import LAYERS, SampleBundle

SECONDS_TO_MS = 1000
FIXED_POINT_SCALE = 1000
SOURCE_PRIORITY = {"default": 0, "v1": 10, "v2": 20}


def convert_v1_time(relative_seconds: int, injected_clock_ms: int, tick_ms: int) -> dict[str, Any]:
    absolute_ms = injected_clock_ms + relative_seconds * SECONDS_TO_MS
    tick_index = absolute_ms // tick_ms
    remainder_ms = absolute_ms % tick_ms
    return {
        "kind": "relative_seconds_added_to_injected_clock",
        "original_value": relative_seconds,
        "original_representation": f"{relative_seconds}s",
        "injected_clock_ms": injected_clock_ms,
        "scale": {
            "seconds_to_ms": SECONDS_TO_MS,
            "fixed_point_scale_per_ms": FIXED_POINT_SCALE,
            "fixed_tick_ms": tick_ms,
        },
        "absolute_ms": absolute_ms,
        "fixed_point_ticks": absolute_ms * FIXED_POINT_SCALE,
        "rounded_tick_index_floor": tick_index,
        "rounding_direction": "floor_for_tick_index;exact_for_milliseconds",
        "sub_tick_remainder_ms": remainder_ms,
        "remainder_participates_in_comparison": True,
        "comparison_key": [absolute_ms, remainder_ms, absolute_ms * FIXED_POINT_SCALE],
    }


def _default_policy(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "layer": None,
        "tombstone": False,
        "invalid": False,
        "source": "default",
        "source_version": "heat-rules-v1",
        "generation": 0,
        "effective_at_ms": 0,
        "time_conversion": None,
        "original_time": {"kind": "heat_default", "value": 0},
        "decision_basis": "no policy mapping; deterministic heat placement applies",
        "candidates": [],
    }


def build_merged_view(bundle: SampleBundle, injected_clock_ms: int) -> dict[str, Any]:
    tick_ms = bundle.capacity.fixed_tick_ms
    v1 = bundle.policy_v1
    v2 = bundle.policy_v2
    policies: dict[str, dict[str, Any]] = {}

    for entry in bundle.entries:
        key = entry.key
        candidates: list[dict[str, Any]] = []
        if key in v1.mappings:
            conversion = convert_v1_time(v1.relative_effective_seconds, injected_clock_ms, tick_ms)
            candidates.append({
                "source": "v1",
                "source_version": v1.source_version,
                "generation": v1.generation,
                "layer": v1.mappings[key],
                "tombstone": False,
                "invalid": False,
                "effective_at_ms": conversion["absolute_ms"],
                "time_key": conversion["comparison_key"],
                "original_time": {
                    "kind": "relative_effective_seconds",
                    "value": v1.relative_effective_seconds,
                    "raw": f"{v1.relative_effective_seconds}s",
                },
                "time_conversion": conversion,
            })
        v2_record = v2.records.get(key)
        if v2_record is not None:
            remainder = v2_record.effective_at_ms % tick_ms
            fixed_ticks = v2_record.effective_at_ms * FIXED_POINT_SCALE
            time_key = [v2_record.effective_at_ms, remainder, fixed_ticks]
            candidates.append({
                "source": "v2",
                "source_version": v2.source_version,
                "generation": v2_record.generation,
                "layer": v2_record.layer,
                "tombstone": v2_record.tombstone,
                "invalid": v2_record.invalid,
                "effective_at_ms": v2_record.effective_at_ms,
                "time_key": time_key,
                "original_time": {
                    "kind": "absolute_effective_ms",
                    "value": v2_record.effective_at_ms,
                    "raw": f"{v2_record.effective_at_ms}ms",
                },
                "time_conversion": {
                    "kind": "absolute_ms",
                    "scale": {"ms_to_ms": 1, "fixed_tick_ms": tick_ms},
                    "absolute_ms": v2_record.effective_at_ms,
                    "rounded_tick_index_floor": v2_record.effective_at_ms // tick_ms,
                    "rounding_direction": "floor_for_tick_index;exact_for_milliseconds",
                    "sub_tick_remainder_ms": remainder,
                    "remainder_participates_in_comparison": True,
                    "comparison_key": time_key,
                },
            })

        if not candidates:
            policies[key] = _default_policy(key)
            continue

        def rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
            return (
                candidate["effective_at_ms"],
                int(candidate["time_key"][1]),
                SOURCE_PRIORITY[candidate["source"]],
                candidate["generation"],
                candidate["source"],
            )

        winner = max(candidates, key=rank)
        same_clock = [c for c in candidates if c["effective_at_ms"] == winner["effective_at_ms"]]
        basis = [
            f"winner={winner['source']}:{winner['source_version']}",
            f"effective_at_ms={winner['effective_at_ms']}",
            f"generation={winner['generation']}",
        ]
        if len(same_clock) > 1:
            basis.append("equal logical clock; source priority format rule v2>v1 wins")
        else:
            basis.append("later declared logical clock wins")
        policy = dict(winner)
        policy["key"] = key
        policy["decision_basis"] = "; ".join(basis)
        policy["candidates"] = candidates
        policies[key] = policy

    fingerprint_source = {
        "injected_clock_ms": injected_clock_ms,
        "relative_seconds": v1.relative_effective_seconds,
        "policies": {
            key: {
                "source": value["source"],
                "generation": value["generation"],
                "layer": value["layer"],
                "tombstone": value["tombstone"],
                "invalid": value["invalid"],
                "effective_at_ms": value["effective_at_ms"],
            }
            for key, value in sorted(policies.items())
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "format": "unified-cache-policy-view/v1",
        "injected_clock_ms": injected_clock_ms,
        "source_priority": SOURCE_PRIORITY,
        "policy_fingerprint": fingerprint,
        "v1_source_version": v1.source_version,
        "v2_source_version": v2.source_version,
        "v2_snapshot_generation": v2.snapshot_generation,
        "policies": policies,
    }


def build_v1_only_view(bundle: SampleBundle, injected_clock_ms: int) -> dict[str, dict[str, Any]]:
    view: dict[str, dict[str, Any]] = {}
    for entry in bundle.entries:
        key = entry.key
        if key in bundle.policy_v1.mappings:
            conversion = convert_v1_time(
                bundle.policy_v1.relative_effective_seconds,
                injected_clock_ms,
                bundle.capacity.fixed_tick_ms,
            )
            view[key] = {
                "key": key,
                "layer": bundle.policy_v1.mappings[key],
                "tombstone": False,
                "invalid": False,
                "generation": bundle.policy_v1.generation,
                "source": "v1",
                "effective_at_ms": conversion["absolute_ms"],
            }
        else:
            default = _default_policy(key)
            view[key] = {
                key_name: default[key_name]
                for key_name in ("key", "layer", "tombstone", "invalid", "generation", "source", "effective_at_ms")
            }
    return view


def _signature(policy: dict[str, Any]) -> tuple[Any, ...]:
    return (
        policy.get("layer"),
        bool(policy.get("tombstone", False)),
        bool(policy.get("invalid", False)),
        int(policy.get("generation", 0)),
        policy.get("source"),
        int(policy.get("effective_at_ms", 0)),
    )


def compute_closure(
    bundle: SampleBundle,
    old_view: dict[str, dict[str, Any]],
    new_view: dict[str, Any],
    *,
    current_layers: Optional[dict[str, str]] = None,
    extra_changed_keys: Optional[set[str]] = None,
) -> dict[str, Any]:
    current_layers = current_layers or {}
    entries_by_key = {entry.key: entry for entry in bundle.entries}
    new_policies = new_view.get("policies", new_view)
    changed_policy_keys: set[str] = set(extra_changed_keys or set())
    for key in entries_by_key:
        if _signature(old_view.get(key, {})) != _signature(new_policies.get(key, {})):
            changed_policy_keys.add(key)

    direct_entry_keys = {key for key in changed_policy_keys if key in entries_by_key}
    affected_tenants = {entries_by_key[key].tenant for key in direct_entry_keys}
    affected_layers: set[str] = set()
    edges: list[dict[str, str]] = []

    for key in sorted(direct_entry_keys):
        entry = entries_by_key[key]
        old_policy = old_view.get(key, {})
        new_policy = new_policies.get(key, {})
        ownership_layer = current_layers.get(key, entry.declared_layer)
        for layer in (ownership_layer, old_policy.get("layer"), new_policy.get("layer")):
            if layer in LAYERS:
                affected_layers.add(layer)
                edges.append({"from": f"policy:{key}", "to": f"layer:{layer}", "relation": "targets_or_owns"})
        edges.append({"from": f"policy:{key}", "to": f"entry:{key}", "relation": "policy_key_to_cache_entry"})
        edges.append({"from": f"entry:{key}", "to": f"tenant:{entry.tenant}", "relation": "entry_to_tenant_quota"})

    recompute: set[str] = set(direct_entry_keys)
    for entry in bundle.entries:
        if entry.tenant in affected_tenants:
            recompute.add(entry.key)
            edges.append({"from": f"tenant:{entry.tenant}", "to": f"entry:{entry.key}", "relation": "quota_eviction_priority"})
        ownership_layer = current_layers.get(entry.key, entry.declared_layer)
        if ownership_layer in affected_layers:
            recompute.add(entry.key)
            edges.append({"from": f"layer:{ownership_layer}", "to": f"entry:{entry.key}", "relation": "current_layer_ownership"})

    reused = sorted(set(entries_by_key) - recompute)
    return {
        "changed_policy_keys": sorted(changed_policy_keys),
        "direct_entry_keys": sorted(direct_entry_keys),
        "affected_tenants": sorted(affected_tenants),
        "affected_layers": sorted(affected_layers),
        "recompute_entry_keys": sorted(recompute),
        "reused_entry_keys": reused,
        "recompute_count": len(recompute),
        "reused_count": len(reused),
        "ledger_domains": {
            "tenant_quotas": sorted(affected_tenants),
            "layer_summaries": sorted(affected_layers),
            "decision_kinds": ["admit", "promote", "demote", "evict", "rebalance"],
        },
        "graph_edges": edges,
    }
