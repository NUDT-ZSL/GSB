"""Admission control, budget ledger and explicit conflict arbitration.

Two global budgets guard the run:

* ``total_candidate_budget`` bounds the sum of per-channel candidate counts.
* ``fusion_compare_budget`` bounds the sum of fusion comparisons (aligned
  unique docs, plus a second pass for two-stage fine reranking).

When a query does not fit, the planner never silently drops work: it either
degrades channels along the configured degrade priority (highest priority =
cut first) and records the path, or rejects the query with E_RECALL_BUDGET and
a human-readable reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import CHANNEL_ORDER, Config
from .data import DataStore, Query
from .errors import E_RECALL_BUDGET, E_RESOURCE_LIMIT

# Deterministic platform-independent byte weights for the resource ledger.
BYTES_PER_CANDIDATE = 96
BYTES_PER_FUSION_ROW = 128
BYTES_PER_DETAIL_CELL = 48


def estimate_channel_limit(
    store: DataStore, query: Query, channel: str, cap: int
) -> int:
    """Cheap deterministic estimate of the achievable candidate count."""
    from .config import CHANNEL_CATEGORY, CHANNEL_INVERTED, CHANNEL_PREFIX

    if cap == 0:
        return 0
    category = query.filters.get("category")
    min_quality = query.filters.get("min_quality")
    if channel == CHANNEL_CATEGORY:
        if category is not None:
            pool = len(store.category_docs.get(str(category), ()))
        else:
            pool = store.num_docs
        if min_quality is not None:
            pool = max(1, pool // 2)
        return min(cap, pool)
    if channel in (CHANNEL_INVERTED, CHANNEL_PREFIX):
        if channel == CHANNEL_PREFIX:
            term_count = sum(
                len(store.prefix_matching_terms(term)) for term in query.terms
            )
        else:
            term_count = sum(
                1 for term in query.terms if term in store.postings
            )
        pool = min(store.num_docs, term_count * 12 + 8)
        if category is not None:
            pool = max(1, pool // 3)
        return min(cap, pool)
    # vector channel scans the whole corpus under filters
    pool = store.num_docs
    if category is not None:
        pool = max(1, pool // len(store.category_docs))
    return min(cap, pool)


def estimate_query_bytes(
    total_candidates: int, aligned_docs: int, config: Config
) -> int:
    enabled = sum(1 for c in CHANNEL_ORDER if config.channels[c].enabled)
    return (
        total_candidates * BYTES_PER_CANDIDATE
        + aligned_docs * BYTES_PER_FUSION_ROW
        + aligned_docs * enabled * BYTES_PER_DETAIL_CELL
    )


@dataclass
class QueryPlan:
    query_id: str
    requested_limits: dict[str, int]
    effective_limits: dict[str, int]
    executed_channels: list[str]
    degraded_channels: list[str]
    degradation_steps: list[dict] = field(default_factory=list)
    two_stage: bool = False
    stage_mode: str = "single_stage"
    estimated_candidates: int = 0
    estimated_comparisons: int = 0
    estimated_bytes: int = 0
    admitted: bool = True
    status: str = "ok"  # ok | degraded | rejected | resource_limited
    error_code: str = ""
    reason: str = ""
    policy_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "requested_limits": self.requested_limits,
            "effective_limits": self.effective_limits,
            "executed_channels": self.executed_channels,
            "degraded_channels": self.degraded_channels,
            "degradation_steps": self.degradation_steps,
            "two_stage": self.two_stage,
            "stage_mode": self.stage_mode,
            "estimated_candidates": self.estimated_candidates,
            "estimated_comparisons": self.estimated_comparisons,
            "estimated_bytes": self.estimated_bytes,
            "admitted": self.admitted,
            "status": self.status,
            "error_code": self.error_code,
            "reason": self.reason,
            "policy_path": self.policy_path,
        }


@dataclass
class BudgetLedger:
    total_candidate_budget: int
    fusion_compare_budget: int
    memory_limit_bytes: int
    used_candidates: int = 0
    used_comparisons: int = 0
    peak_resident_bytes: int = 0
    resident_bytes: int = 0
    admitted_queries: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    @property
    def remaining_candidates(self) -> int:
        return self.total_candidate_budget - self.used_candidates

    @property
    def remaining_comparisons(self) -> int:
        return self.fusion_compare_budget - self.used_comparisons


def _initial_limits(store: DataStore, query: Query, config: Config) -> dict[str, int]:
    limits: dict[str, int] = {}
    for channel in CHANNEL_ORDER:
        spec = config.channels[channel]
        if not spec.enabled:
            limits[channel] = 0
        else:
            limits[channel] = estimate_channel_limit(
                store, query, channel, spec.candidate_limit
            )
    return limits


def _sizes(limits: dict[str, int], store: DataStore, query: Query, config: Config):
    total_candidates = sum(limits.values())
    aligned = min(
        max(total_candidates, 1),
        store.num_docs,
    )
    comparisons = aligned
    if config.two_stage_enabled and aligned > config.fine_k:
        comparisons += min(aligned, max(1, config.coarse_k))
    size = estimate_query_bytes(total_candidates, aligned, config)
    return total_candidates, comparisons, size


def plan_query(
    store: DataStore,
    query: Query,
    config: Config,
    ledger: BudgetLedger,
) -> QueryPlan:
    requested = _initial_limits(store, query, config)
    effective = dict(requested)
    plan = QueryPlan(
        query_id=query.query_id,
        requested_limits=requested,
        effective_limits=effective,
        executed_channels=[
            c for c in CHANNEL_ORDER if config.channels[c].enabled
        ],
        degraded_channels=[],
    )

    total_candidates, comparisons, size = _sizes(effective, store, query, config)
    plan.estimated_candidates, plan.estimated_comparisons, plan.estimated_bytes = (
        total_candidates,
        comparisons,
        size,
    )

    # Conflict 1: memory first when policy demands it; a single oversized query
    # can never be admitted, even if budget remains.
    if size > ledger.memory_limit_bytes:
        plan.policy_path.append(
            "oversize_query:"
            + ",".join(config.policy_priority)
        )

    # Deterministic degradation loop driven by the configured degrade priority.
    def fits() -> bool:
        cand, comp, sz = _sizes(effective, store, query, config)
        return (
            cand <= ledger.remaining_candidates
            and comp <= ledger.remaining_comparisons
            and sz <= ledger.memory_limit_bytes
        )

    while not fits():
        # choose the highest-priority channel that can still be reduced
        target = ""
        for channel in config.degrade_priority:
            if effective.get(channel, 0) > 0:
                target = channel
                break
        if not target:
            cand, comp, sz = _sizes(effective, store, query, config)
            if sz > ledger.memory_limit_bytes:
                plan.status = "resource_limited"
                plan.error_code = E_RESOURCE_LIMIT
                plan.reason = (
                    f"query needs {sz} bytes but memory limit is "
                    f"{ledger.memory_limit_bytes} even at minimum limits"
                )
            else:
                plan.status = "rejected"
                plan.error_code = E_RECALL_BUDGET
                plan.reason = (
                    f"minimum plan needs candidates={cand}, comparisons={comp} "
                    f"but remaining budget is candidates={ledger.remaining_candidates}, "
                    f"comparisons={ledger.remaining_comparisons}"
                )
            plan.admitted = False
            return plan
        before = effective[target]
        effective[target] = before // 2
        if effective[target] == before and before > 0:
            effective[target] = before - 1
        plan.degradation_steps.append(
            {
                "channel": target,
                "from_limit": before,
                "to_limit": effective[target],
                "reason": "exceeded_budget_or_memory",
            }
        )

    total_candidates, comparisons, size = _sizes(effective, store, query, config)
    plan.effective_limits = effective
    plan.estimated_candidates, plan.estimated_comparisons, plan.estimated_bytes = (
        total_candidates,
        comparisons,
        size,
    )
    plan.degraded_channels = [
        channel
        for channel in config.degrade_priority
        if effective[channel] < requested[channel]
    ]
    plan.status = "degraded" if plan.degradation_steps else "ok"

    # Conflict 2: two-stage coarse->fine rerank vs. fusion compare budget.
    full_two_stage = config.two_stage_enabled and total_candidates > config.fine_k
    if full_two_stage:
        single_comparisons = min(store.num_docs, total_candidates)
        if comparisons <= ledger.remaining_comparisons:
            plan.two_stage = True
            plan.stage_mode = "two_stage"
            plan.policy_path.append(
                "two_stage:accepted_within_compare_budget"
            )
        else:
            plan.two_stage = False
            plan.stage_mode = "single_stage"
            plan.policy_path.append(
                "two_stage:rejected_compare_budget_single_stage_fallback"
            )
            plan.estimated_comparisons = single_comparisons
            comparisons = single_comparisons
    else:
        plan.two_stage = False
        plan.stage_mode = "single_stage"

    # Conflict 3: worker count (latency vs. memory) is arbitrated once per run in
    # the engine and recorded on every plan for observability.
    plan.policy_path.append(
        "parallelism:resolved_by_policy_priority=" + ",".join(config.policy_priority)
    )
    return plan
