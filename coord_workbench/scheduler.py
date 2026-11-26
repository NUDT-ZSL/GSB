"""Parallel election-topology merge scheduler (req 4).

Batches are selected under two constraints: a per-round candidate
concurrency cap and a total logical-time budget.  A started batch is
never truncated -- if its actual cost overruns the remaining budget the
scheduler stops starting new batches and proactively abandons the rest.

Three non-committed endings are distinguished:
  * NOT_STARTED_BUDGET -- never started (budget or concurrency exhausted)
  * START_FAILED       -- started but the commit raised an error
  * ABANDONED          -- proactively dropped (duplicate or budget overrun)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from .errors import BizError, Err
from .models import (
    OUTCOME_ABANDONED,
    OUTCOME_COMMITTED,
    OUTCOME_NOT_STARTED,
    OUTCOME_START_FAILED,
    Batch,
)


class MergeScheduler:
    def __init__(self, max_concurrency: int, time_budget_ms: int) -> None:
        self.max_concurrency = max_concurrency
        self.time_budget_ms = time_budget_ms

    def run(self, batches: List[Batch], commit_fn: Callable[[Batch], int]) -> List[Dict[str, Any]]:
        remaining = self.time_budget_ms
        started = 0
        overrun = False
        records: List[Dict[str, Any]] = []
        for batch in batches:
            rec: Dict[str, Any] = {
                "batch_id": batch.batch_id,
                "domain": batch.domain,
                "topo_epoch": batch.topo_epoch,
                "events": len(batch.events),
                "est_cost_ms": batch.est_cost_ms,
            }
            if overrun:
                records.append({**rec, "outcome": OUTCOME_ABANDONED, "reason": "budget-overrun"})
                continue
            if started >= self.max_concurrency:
                records.append({**rec, "outcome": OUTCOME_NOT_STARTED, "reason": "concurrency-limit"})
                continue
            if batch.est_cost_ms > remaining:
                records.append({**rec, "outcome": OUTCOME_NOT_STARTED, "reason": "budget-exhausted"})
                continue
            started += 1
            try:
                actual_cost = commit_fn(batch)
            except BizError as exc:
                if exc.code == Err.BATCH_DUPLICATE:
                    records.append({**rec, "outcome": OUTCOME_ABANDONED, "reason": "duplicate-committed"})
                else:
                    records.append(
                        {**rec, "outcome": OUTCOME_START_FAILED, "reason": exc.code, "error": exc.to_dict()}
                    )
                continue
            remaining -= actual_cost
            records.append({**rec, "outcome": OUTCOME_COMMITTED, "actual_cost_ms": actual_cost})
            if remaining < 0:
                # a started batch overran the budget: it completed (never
                # truncated) but no further batch may start
                overrun = True
        return records
