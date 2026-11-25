from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .canonical import canonical_digest
from .planner import closure_downstream


def max_seed_generation(step: Mapping[str, Any]) -> int:
    generations = [
        int(candidate.get("cache_generation") or 0)
        for candidate in step.get("cache_candidates") or []
    ]
    return max(generations) if generations else 0


def build_invalidation_chain(
    bundle: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    explicit_roots: Optional[Sequence[str]] = None,
    cache_miss_roots: Optional[Sequence[str]] = None,
    clock_ms: int,
    epoch: int = 1,
    prior_generations: Optional[Mapping[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    explicit = sorted(set(explicit_roots or []))
    miss_roots = sorted(set(cache_miss_roots or []))
    parameter_roots = list(plan.get("invalid_roots") or [])
    roots = sorted(set(parameter_roots) | set(explicit) | set(miss_roots))
    if not roots:
        return None

    affected = closure_downstream(roots, bundle["steps"])
    step_by_id = {step["step_id"]: step for step in bundle["steps"]}
    prior = prior_generations or {}
    step_generations: Dict[str, int] = {}
    previous_generations: Dict[str, int] = {}
    for step_id in affected:
        seed_gen = max_seed_generation(step_by_id[step_id])
        previous = max(seed_gen, int(prior.get(step_id, 0) or 0))
        previous_generations[step_id] = previous
        step_generations[step_id] = previous + 1

    trigger_source: Dict[str, Any] = {
        "type": "combined_change",
        "parameter_changes": plan.get("parameter_changes") or [],
        "direct_parameter_steps": plan.get("direct_parameter_steps") or {},
        "explicit_invalidation_roots": explicit,
        "cache_miss_roots": miss_roots,
        "parameter_roots": parameter_roots,
    }
    chain_id = "inv:" + canonical_digest(
        {
            "epoch": epoch,
            "trigger_source": trigger_source,
            "roots": roots,
            "affected": affected,
            "generations": step_generations,
        }
    ).split(":", 1)[1][:24]
    return {
        "chain_id": chain_id,
        "epoch": epoch,
        "trigger_source": trigger_source,
        "root_steps": roots,
        "affected_steps": affected,
        "clock_ms": int(clock_ms),
        "previous_generations": previous_generations,
        "step_generations": step_generations,
        "propagation_rule": (
            "changed resolved build parameter or explicit cache invalidation marks its direct "
            "step; every parent/dependency edge descendant is invalidated exactly once"
        ),
    }


def invalidation_index(chain: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not chain:
        return {}
    result = {}
    for step_id in chain["affected_steps"]:
        result[step_id] = {
            "invalidated": True,
            "chain_id": chain["chain_id"],
            "invalidation_epoch": chain["epoch"],
            "invalidation_generation": chain["step_generations"][step_id],
            "previous_generation": chain["previous_generations"][step_id],
        }
    return result
