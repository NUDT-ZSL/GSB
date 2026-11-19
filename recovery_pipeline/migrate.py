"""Time/dependency semantics migration helpers (v1 -> v2 unified semantics)."""

from __future__ import annotations

from .models import ServicePlan, UnifiedPlan


def should_rollback(rollback_abs_ms, now_ms: int) -> bool:
    """Unified predicate: a service is due for rollback at/after its instant."""
    return rollback_abs_ms is not None and now_ms >= rollback_abs_ms


def migration_invariant_holds(plan: UnifiedPlan, service: str, now_ms: int) -> bool:
    """Check the should-rollback state is identical before/after migration.

    The pre-migration state is recomputed from the recorded original relative
    rollback seconds and the recorded conversion scale; the post-migration
    state uses the migrated absolute instant stored on the plan.
    """
    sp: ServicePlan = plan.services[service]
    record = plan.migration_map.get(service)
    if record is None:
        return True  # nothing migrated for this service
    base_ms = record["rollback_abs_ms"] - record["rollback_seconds"] * 1000
    pre = should_rollback(base_ms + record["rollback_seconds"] * 1000, now_ms)
    post = should_rollback(sp.rollback_abs_ms, now_ms)
    return pre == post
