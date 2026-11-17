"""Deterministic scheduling plan.

The plan is computed before execution from the ready queue ordered by
stable task key, using list scheduling over virtual slot clocks and the
deterministic cost model. Because every input to the planner is
deterministic, the decision log, the enqueue order and the budget ledger
are identical for any max_concurrency and any wall-clock interleaving;
worker threads only execute the precomputed per-slot queues.

Budget policy: a task is started only if the reserved (estimated) cost of
all previously selected tasks plus its own estimate fits the global
budget. Otherwise it is marked E_BUDGET_EXHAUSTED and keeps its position
in the ready queue (reported in enqueue order).
"""

from . import cost as cost_mod
from .data import fold_indices
from .errors import E_BUDGET_EXHAUSTED


def build_plan(specs, dataset, cfg, max_concurrency):
    costs = cost_mod.cost_model(cfg)
    dim = dataset.feature_dim
    folds_count = cfg.get("cv_folds", 3)
    global_budget = float(cfg["scheduler"]["global_budget_ms"])
    slot_available = [0.0] * max_concurrency
    assignments = [[] for _ in range(max_concurrency)]
    decisions = []
    exhausted = []
    reserved_total = 0.0

    for spec in specs:  # specs already sorted by stable key
        folds = fold_indices(dataset.n, folds_count, spec.seed)
        shapes = []
        for test_idx in folds:
            shapes.append((dataset.n - len(test_idx), len(test_idx)))
        estimate = cost_mod.estimate_task_cost(spec, shapes, dim, costs)
        spec.estimated_ms = estimate

        if reserved_total + estimate > global_budget:
            exhausted.append(spec.key)
            decisions.append({
                "seq": spec.enqueue_seq,
                "decision": "defer",
                "task_key": spec.key,
                "code": E_BUDGET_EXHAUSTED,
                "reason": "global_budget_reservation_exceeded",
                "estimated_ms": estimate,
                "budget_reserved_ms": reserved_total,
            })
            continue

        slot = min(range(max_concurrency),
                   key=lambda i: (slot_available[i], i))
        start = slot_available[slot]
        end = start + estimate
        slot_available[slot] = end
        reserved_total += estimate
        assignments[slot].append(spec)
        decisions.append({
            "seq": spec.enqueue_seq,
            "decision": "assign",
            "task_key": spec.key,
            "slot": slot,
            "reason": "min_stable_key_from_ready_queue;"
                      "slot_earliest_virtual_free",
            "estimated_ms": estimate,
            "virtual_start_ms": start,
            "virtual_end_ms": end,
            "budget_reserved_ms": reserved_total,
        })
        spec.slot = slot
        spec.virtual_start_ms = start
        spec.virtual_end_ms = end

    return {
        "assignments": assignments,
        "decisions": decisions,
        "exhausted": exhausted,
        "reserved_ms": reserved_total,
    }
