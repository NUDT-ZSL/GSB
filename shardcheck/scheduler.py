from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BudgetPlan:
    current_usage_bytes: int
    pending_bytes: int
    occupancy_ratio: float
    policy_occupancy: float
    occupancy_override_hit: bool
    desired_retention: int
    effective_retention: int
    retention_illegal: bool
    budget_action: str
    checkpoint_due: bool
    decision: str
    reason: str
    prospective_usage_bytes: int
    evictions: list[dict[str, Any]] = field(default_factory=list)
    retained_generation_ids: list[str] = field(default_factory=list)
    evicted_generation_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_usage_bytes": self.current_usage_bytes,
            "pending_bytes": self.pending_bytes,
            "occupancy_ratio": round(self.occupancy_ratio, 9),
            "policy_occupancy": round(self.policy_occupancy, 9),
            "occupancy_override_hit": self.occupancy_override_hit,
            "desired_retention": self.desired_retention,
            "effective_retention": self.effective_retention,
            "retention_illegal": self.retention_illegal,
            "budget_action": self.budget_action,
            "checkpoint_due": self.checkpoint_due,
            "decision": self.decision,
            "reason": self.reason,
            "prospective_usage_bytes": self.prospective_usage_bytes,
            "evictions": self.evictions,
            "retained_generation_ids": self.retained_generation_ids,
            "evicted_generation_ids": self.evicted_generation_ids,
        }


def resolve_retention(
    desired_retention: int, max_retention: int, *, override_value: int | None = None
) -> tuple[int, bool, int]:
    requested = desired_retention if override_value is None else override_value
    illegal = requested < 1 or requested > max_retention
    effective = min(max(1, requested), max(1, max_retention))
    return requested, illegal, effective


def plan_budget(
    *,
    manifest: dict[str, Any],
    pending_bytes: int,
    global_budget_bytes: int,
    desired_retention: int,
    max_retention: int,
    budget_action: str,
    checkpoint_due: bool,
    occupancy_override: float | None = None,
    retention_override: int | None = None,
) -> BudgetPlan:
    generations = sorted(
        manifest.get("generations", []), key=lambda item: int(item["generation"])
    )
    current_usage = sum(int(item.get("payload_bytes", 0)) for item in generations)
    actual_occupancy = current_usage / global_budget_bytes if global_budget_bytes else 1.0
    policy_occupancy = actual_occupancy
    occupancy_override_hit = False
    if occupancy_override is not None:
        policy_occupancy = float(occupancy_override)
        occupancy_override_hit = not (0.0 <= policy_occupancy <= 1.0)
    policy_occupancy = min(1.0, max(0.0, policy_occupancy))

    requested, retention_illegal, effective_retention = resolve_retention(
        desired_retention, max_retention, override_value=retention_override
    )

    if not checkpoint_due:
        return BudgetPlan(
            current_usage_bytes=current_usage,
            pending_bytes=0,
            occupancy_ratio=actual_occupancy,
            policy_occupancy=policy_occupancy,
            occupancy_override_hit=occupancy_override_hit,
            desired_retention=requested,
            effective_retention=effective_retention,
            retention_illegal=retention_illegal,
            budget_action=budget_action,
            checkpoint_due=False,
            decision="deferred_by_interval",
            reason="checkpoint commit interval has not elapsed",
            prospective_usage_bytes=current_usage,
            retained_generation_ids=[item["generation_id"] for item in generations],
        )

    keep_old_count = min(len(generations), max(0, effective_retention - 1))
    kept_old = generations[len(generations) - keep_old_count :] if keep_old_count else []
    retention_evictions = generations[: len(generations) - keep_old_count]
    home_evicted_ids = [item["generation_id"] for item in retention_evictions]
    prospective = sum(int(item.get("payload_bytes", 0)) for item in kept_old) + pending_bytes
    evictions = [
        {
            "order": index + 1,
            "generation_id": item["generation_id"],
            "generation": item["generation"],
            "round": item["round"],
            "bytes": int(item.get("payload_bytes", 0)),
            "reason": "retention_limit",
        }
        for index, item in enumerate(retention_evictions)
    ]

    if prospective <= global_budget_bytes:
        return BudgetPlan(
            current_usage_bytes=current_usage,
            pending_bytes=pending_bytes,
            occupancy_ratio=actual_occupancy,
            policy_occupancy=policy_occupancy,
            occupancy_override_hit=occupancy_override_hit,
            desired_retention=requested,
            effective_retention=effective_retention,
            retention_illegal=retention_illegal,
            budget_action=budget_action,
            checkpoint_due=True,
            decision="commit",
            reason="retention and storage budget satisfied",
            prospective_usage_bytes=prospective,
            evictions=evictions,
            retained_generation_ids=[item["generation_id"] for item in kept_old],
            evicted_generation_ids=home_evicted_ids,
        )

    if budget_action == "reject":
        return BudgetPlan(
            current_usage_bytes=current_usage,
            pending_bytes=pending_bytes,
            occupancy_ratio=actual_occupancy,
            policy_occupancy=policy_occupancy,
            occupancy_override_hit=occupancy_override_hit,
            desired_retention=requested,
            effective_retention=effective_retention,
            retention_illegal=retention_illegal,
            budget_action=budget_action,
            checkpoint_due=True,
            decision="reject",
            reason="policy budget_action=reject and projected usage exceeds budget",
            prospective_usage_bytes=current_usage,
            retained_generation_ids=[item["generation_id"] for item in generations],
        )

    kept_for_budget = list(kept_old)
    budget_evictions: list[dict[str, Any]] = []
    while prospective > global_budget_bytes and kept_for_budget:
        victim = kept_for_budget.pop(0)
        victim_bytes = int(victim.get("payload_bytes", 0))
        prospective -= victim_bytes
        budget_evictions.append(
            {
                "order": len(evictions) + len(budget_evictions) + 1,
                "generation_id": victim["generation_id"],
                "generation": victim["generation"],
                "round": victim["round"],
                "bytes": victim_bytes,
                "reason": "budget_reclaim",
            }
        )
    if prospective > global_budget_bytes:
        return BudgetPlan(
            current_usage_bytes=current_usage,
            pending_bytes=pending_bytes,
            occupancy_ratio=actual_occupancy,
            policy_occupancy=policy_occupancy,
            occupancy_override_hit=occupancy_override_hit,
            desired_retention=requested,
            effective_retention=effective_retention,
            retention_illegal=retention_illegal,
            budget_action=budget_action,
            checkpoint_due=True,
            decision="reject",
            reason="even evicting every old generation cannot satisfy the storage budget",
            prospective_usage_bytes=current_usage,
            retained_generation_ids=[item["generation_id"] for item in generations],
        )
    evictions.extend(budget_evictions)
    return BudgetPlan(
        current_usage_bytes=current_usage,
        pending_bytes=pending_bytes,
        occupancy_ratio=actual_occupancy,
        policy_occupancy=policy_occupancy,
        occupancy_override_hit=occupancy_override_hit,
        desired_retention=requested,
        effective_retention=effective_retention,
        retention_illegal=retention_illegal,
        budget_action=budget_action,
        checkpoint_due=True,
        decision="commit",
        reason="old generations evicted to satisfy storage budget",
        prospective_usage_bytes=prospective,
        evictions=evictions,
        retained_generation_ids=[item["generation_id"] for item in kept_for_budget],
        evicted_generation_ids=[item["generation_id"] for item in retention_evictions]
        + [item["generation_id"] for item in budget_evictions],
    )
