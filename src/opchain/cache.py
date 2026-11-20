"""Content-addressed intermediate cache with suffix invalidation planning."""

from dataclasses import dataclass

from .hashing import hash_json
from .errors import ErrorCode, OpChainError


@dataclass
class CacheEntry:
    key: str
    case_id: str
    stage: int
    input_hash: str
    chain_digest: str
    axis_order: tuple
    block_size: int
    eps: float
    precision: str
    activation_kind: str
    content_hash: str

    def to_dict(self):
        data = self.__dict__.copy()
        data["axis_order"] = list(self.axis_order)
        return data

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["axis_order"] = tuple(data["axis_order"])
        return cls(**data)


def build_cache_key(parts):
    """Key covers input content, chain, axes, block size, eps and precision."""
    return hash_json(parts)


class ContentCache:
    def __init__(self):
        self._entries = {}

    def get(self, key):
        return self._entries.get(key)

    def put(self, entry):
        self._entries[entry.key] = entry

    def size(self):
        return len(self._entries)

    def entries(self):
        return list(self._entries.values())

    def clear(self):
        self._entries.clear()

    def plan_invalidation(self, case_id, old_spec, new_spec, old_stage_keys, new_stage_keys):
        """Split affected entries into disjoint invalidated and reused sets.

        A stage is reused only when its full content-addressed key is identical;
        any change to axes, block size, eps, precision, activation or upstream
        chain changes the key, invalidating that stage and every suffix stage.
        """
        reused_keys = []
        invalidated_keys = []
        divergence_seen = False
        for stage, (old_key, new_key) in enumerate(zip(old_stage_keys, new_stage_keys)):
            if not divergence_seen and old_key == new_key:
                reused_keys.append(new_key)
            else:
                divergence_seen = True
                invalidated_keys.append(old_key)

        # Specs with different stage counts invalidate every remaining suffix.
        if len(old_stage_keys) > len(new_stage_keys):
            invalidated_keys.extend(old_stage_keys[len(new_stage_keys):])

        affected = set(reused_keys) | set(invalidated_keys)
        return {
            "case_id": case_id,
            "reused_keys": reused_keys,
            "invalidated_keys": invalidated_keys,
            "affected_keys": sorted(affected),
            "reused_count": len(reused_keys),
            "invalidated_count": len(invalidated_keys),
        }

    def apply_invalidation(self, plan):
        for key in plan["invalidated_keys"]:
            self._entries.pop(key, None)

    def enforce_recompute_limits(self, plan, max_cache_entries, max_parallel_recompute):
        """Resource ceilings for cache volume and parallel recompute tasks."""
        if self.size() + len(plan["invalidated_keys"]) > max_cache_entries:
            raise OpChainError(
                ErrorCode.RESOURCE_LIMIT,
                "recompute would exceed the cache entry ceiling",
                {
                    "cache_size": self.size(),
                    "to_recompute": len(plan["invalidated_keys"]),
                    "max_cache_entries": max_cache_entries,
                },
            )
        if len(plan["invalidated_keys"]) > max_parallel_recompute:
            raise OpChainError(
                ErrorCode.RESOURCE_LIMIT,
                "parallel recompute task count exceeds the configured ceiling",
                {
                    "recompute_tasks": len(plan["invalidated_keys"]),
                    "max_parallel_recompute": max_parallel_recompute,
                },
            )


def stage_cache_parts(spec, stage, input_hash, block_size, precision):
    op_prefix = spec.op_chain[: stage + 1]
    current_axes = _semantic_axes_at(spec, stage)
    return {
        "case_id": spec.case_id,
        "stage": stage,
        "input_hash": input_hash,
        "chain_prefix": op_prefix,
        "axis_order": list(current_axes),
        "declared_axis_order": list(spec.axis_order),
        "block_size": int(block_size),
        "eps": float(spec.eps),
        "precision": precision,
        "activation_kind": spec.activation_kind,
    }


def _semantic_axes_at(spec, stage):
    axes = list(range(len(spec.input_shape)))
    for op in spec.op_chain[: stage + 1]:
        if op["op"] == "transpose":
            axes = [axes[source] for source in op["axes"]]
    return axes
