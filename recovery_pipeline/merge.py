"""Cross-version merge: build a unified, stably-sorted recovery plan.

Conflict rule for a service present in both generations:
  1. higher recovery generation wins (v1 actions carry implicit generation 1);
  2. otherwise the later declared logical clock wins (v1: max action tick,
     v2: effective_ms);
  3. otherwise source-format priority decides (v2 > v1).
Every merged action keeps its source version, original time representation
and original relative rollback value.
"""

from __future__ import annotations

from .models import UnifiedAction, UnifiedPlan, ServicePlan, V1Config, V2Config

_FORMAT_PRIORITY = {"v1": 1, "v2": 2}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _v1_declared_clock(svc) -> int:
    return max((a.at_tick for a in svc.actions), default=0)


def _pick_winner(v1_svc, v2_svc) -> str:
    v1_key = (1, _v1_declared_clock(v1_svc), _FORMAT_PRIORITY["v1"])
    v2_key = (v2_svc.generation, v2_svc.effective_ms, _FORMAT_PRIORITY["v2"])
    return "v2" if v2_key >= v1_key else "v1"


def merge_configs(v1: V1Config | None, v2: V2Config | None,
                  clock_now_ms: int) -> UnifiedPlan:
    """Merge both generations into one unified plan.

    ``clock_now_ms`` is the injected logical-clock reading used as the base
    for converting v1 relative rollback seconds; v2 absolute times are kept.
    """
    plan = UnifiedPlan()
    v1_by_name = {s.name: s for s in v1.services} if v1 else {}
    v2_live = {s.name: s for s in v2.services if not s.tombstone} if v2 else {}

    ordinal = 0
    for name in sorted(set(v1_by_name) | set(v2_live)):
        v1_svc = v1_by_name.get(name)
        v2_svc = v2_live.get(name)
        if v1_svc and v2_svc:
            winner = _pick_winner(v1_svc, v2_svc)
        elif v2_svc:
            winner = "v2"
        else:
            winner = "v1"

        sp = ServicePlan(service=name, fault_domain="")
        if winner == "v2":
            sp.fault_domain = f"fd-{v2_svc.generation}"
            sp.upstream = v2_svc.upstream
            sp.generation = v2_svc.generation
            sp.priority = v2_svc.priority
            sp.rollback_abs_ms = v2_svc.effective_ms  # absolute kept as-is
            action = UnifiedAction(
                service=name,
                action_id=f"{name}-v2-g{v2_svc.generation}",
                op="switch",
                source_version="v2",
                original_time_repr=f"ms:{v2_svc.effective_ms}",
                original_rollback_seconds=None,
                effective_ms=v2_svc.effective_ms,
                generation=v2_svc.generation,
                priority=v2_svc.priority,
                ordinal=ordinal,
            )
            ordinal += 1
            sp.actions.append(action)
        else:
            sp.fault_domain = v1_svc.fault_domain
            sp.generation = 1
            sp.priority = "P3"
            # v1 relative rollback seconds -> absolute ms under injected clock.
            sp.rollback_abs_ms = clock_now_ms + v1_svc.rollback_seconds * 1000
            plan.migration_map[name] = {
                "source_version": "v1",
                "target_version": "v2",
                "rollback_seconds": v1_svc.rollback_seconds,
                "rollback_abs_ms": sp.rollback_abs_ms,
                "scale": "1s=1000ms",
                "rounding": "EXACT",
                "generation_default": 1,
                "priority_default": "P3",
            }
            for act in v1_svc.actions:
                sp.actions.append(UnifiedAction(
                    service=name,
                    action_id=act.action_id,
                    op=act.op,
                    source_version="v1",
                    original_time_repr=f"tick:{act.at_tick}",
                    original_rollback_seconds=v1_svc.rollback_seconds,
                    effective_ms=clock_now_ms + act.at_tick * 1000,
                    generation=1,
                    priority="P3",
                    ordinal=ordinal,
                ))
                ordinal += 1

        # Stable sort: effective time, then priority rank, then declaration order.
        sp.actions.sort(key=lambda a: (a.effective_ms,
                                       _PRIORITY_RANK.get(a.priority, 9),
                                       a.ordinal))
        plan.services[name] = sp
    return plan
