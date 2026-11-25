from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .models import LAYERS, LAYER_RANK, Capacity, Entry, Tenant

HEAT_COUNT_BASE = 1 << 64


def heat_score(last_access_ms: int, access_count: int) -> int:
    """Lexicographic score encoded as one exact integer: clock then count."""
    if access_count < 0 or access_count >= HEAT_COUNT_BASE:
        raise ValueError("access_count must fit below the fixed heat base")
    return last_access_ms * HEAT_COUNT_BASE + access_count


def entry_heat(entry: Entry | Any) -> int:
    return heat_score(int(entry["last_access_ms"]) if not isinstance(entry, Entry) else entry.last_access_ms,
                      int(entry["access_count"]) if not isinstance(entry, Entry) else entry.access_count)


def heat_target_layer(entry: Entry | Any, clock_ms: int, capacity: Capacity) -> str:
    if isinstance(entry, Entry):
        last_access = entry.last_access_ms
        count = entry.access_count
    else:
        last_access = int(entry["last_access_ms"])
        count = int(entry["access_count"])
    age = clock_ms - last_access
    if count >= capacity.hot_access_count and age <= capacity.hot_window_ms:
        return "L1"
    if count >= capacity.warm_access_count or age <= capacity.hot_window_ms * 2:
        return "L2"
    return "L3"


def policy_target(policy: Optional[dict[str, Any]], entry: Entry | Any, clock_ms: int, capacity: Capacity) -> tuple[Optional[str], list[str]]:
    reasons: list[str] = []
    if not policy:
        return heat_target_layer(entry, clock_ms, capacity), ["policy_missing;heat_tier"]
    effective_at = int(policy.get("effective_at_ms", 0))
    if effective_at > clock_ms:
        reasons.append(f"policy_not_effective_until_{effective_at};heat_tier")
        return heat_target_layer(entry, clock_ms, capacity), reasons
    if policy.get("tombstone"):
        return None, ["active_tombstone"]
    if policy.get("invalid"):
        return heat_target_layer(entry, clock_ms, capacity), ["invalid_marker_ignores_mapping;heat_tier"]
    layer = policy.get("layer")
    if layer in LAYERS:
        return layer, [f"policy_layer_{layer}"]
    return heat_target_layer(entry, clock_ms, capacity), ["policy_without_layer;heat_tier"]


def victim_sort_key(row: Any, *, tombstone_first: bool = True) -> tuple[Any, ...]:
    """Higher eviction weight means higher eviction priority.

    After policy urgency, colder heat and earlier seq make the order stable.
    """
    if isinstance(row, dict):
        tenant_weight = float(row.get("eviction_priority_weight", 1.0))
        score = int(row.get("heat_score", 0))
        seq = int(row.get("seq", 0))
        key = str(row.get("key", ""))
        tombstone = bool(row.get("policy_tombstone", False))
        invalid = bool(row.get("policy_invalid", False))
    else:
        tenant_weight = float(row["eviction_priority_weight"])
        score = int(row["heat_score"])
        seq = int(row["seq"])
        key = str(row["key"])
        tombstone = bool(row["policy_tombstone"])
        invalid = bool(row["policy_invalid"])
    urgency = 0 if tombstone_first and tombstone else (1 if invalid else 2)
    return (urgency, -tenant_weight, score, seq, key)


@dataclass
class Usage:
    tenant_blocks: dict[str, int]
    tenant_bytes: dict[str, int]
    layer_blocks: dict[str, int]
    layer_bytes: dict[str, int]
    total_bytes: int


def fits(
    *,
    entry: Entry | Any,
    layer: str,
    tenants: dict[str, Tenant],
    capacity: Capacity,
    usage: Usage,
) -> tuple[bool, Optional[str]]:
    tenant = entry.tenant if isinstance(entry, Entry) else entry["tenant"]
    size_blocks = entry.block_size if isinstance(entry, Entry) else int(entry["block_size"])
    size_bytes = size_blocks * capacity.block_bytes
    tenant_obj = tenants[tenant]
    if usage.tenant_blocks.get(tenant, 0) + size_blocks > tenant_obj.hard_quota_blocks:
        return False, "tenant_hard_quota"
    if usage.layer_blocks.get(layer, 0) + size_blocks > capacity.layer_max_blocks[layer]:
        return False, "layer_capacity"
    if usage.total_bytes + size_bytes > capacity.total_byte_budget:
        return False, "total_byte_budget"
    return True, None


def admission_layers(desired_layer: str) -> list[str]:
    start = LAYER_RANK[desired_layer]
    return list(LAYERS[start:])


def threshold_blocks(capacity: Capacity, layer: str) -> int:
    limit = capacity.layer_max_blocks[layer]
    if capacity.threshold_unit == "blocks":
        return min(capacity.rebalance_threshold, limit)
    return limit * capacity.rebalance_threshold // 100


def layer_over_threshold(capacity: Capacity, layer: str, used_blocks: int) -> bool:
    if capacity.threshold_unit == "blocks":
        return used_blocks > min(capacity.rebalance_threshold, capacity.layer_max_blocks[layer])
    return used_blocks * 100 > capacity.layer_max_blocks[layer] * capacity.rebalance_threshold


def migration_cost(capacity: Capacity, block_size: int) -> int:
    return capacity.migration_base_cost_ms + block_size * capacity.migration_block_cost_ms
