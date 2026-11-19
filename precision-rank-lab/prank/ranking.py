"""Gate decision, deterministic top-N ranking and diversity re-ranking."""
from __future__ import annotations

import numpy as np

from .errors import BusinessError
from .models import Item
from .precision import PrecisionContext


def gate_decisions(scores_native, item_ids: list[str], ctx: PrecisionContext,
                   threshold_f64: float, tolerance_f64: float, strategy: str,
                   near_delta_f64: float):
    """Return per-item gate decisions under strict and tolerance-band strategies.

    Decisions are evaluated in the active tier's representation; ``f64`` views
    are attached for reporting. The tolerance band treats scores inside
    ``[threshold - tolerance, threshold + tolerance]`` as admitted, which keeps
    threshold-near candidates stable against rounding flips between reruns and
    precision tiers.
    """
    threshold = ctx.encode(threshold_f64)
    tolerance = ctx.encode(tolerance_f64)
    lower = ctx.sub(threshold, tolerance)
    rows = []
    for idx, item_id in enumerate(item_ids):
        native = scores_native[idx]
        f64 = float(ctx.decode(native))
        strict_pass = bool(native >= threshold)
        band_pass = bool(native >= lower)
        admitted = band_pass if strategy == "tolerance_band" else strict_pass
        near = abs(f64 - threshold_f64) <= near_delta_f64
        rows.append({
            "item_id": item_id,
            "score": f64,
            "strict_pass": strict_pass,
            "band_pass": band_pass,
            "admitted": admitted,
            "near_threshold": near,
            "distance_to_threshold": f64 - threshold_f64,
        })
    return rows


def deterministic_order(scores, item_ids: list[str]) -> list[int]:
    """Sort by score descending, item_id ascending. Never relies on dict order."""
    score_arr = np.asarray(scores)
    ids_arr = np.asarray(item_ids)
    # lexsort applies the last key first: (-score, item_id)
    order = np.lexsort((ids_arr, -score_arr))
    return [int(i) for i in order]


def diversity_rerank(ranked_ids: list[str], items_by_id: dict[str, Item],
                     native_scores: dict[str, object],
                     ctx: PrecisionContext, category_penalty_f64: float,
                     decay_f64: float, query_id: str) -> tuple[list[str], list[dict]]:
    """Greedy diversity re-rank with category penalty and position decay.

    Adjusted score at slot ``p`` (0-based) for item ``i``::

        adj(i, p) = base(i) * decay^p - penalty * 1[category already seen]

    Every operation goes through the precision context so the fixed tier keeps
    the same semantics. Ties break by item_id ascending.
    """
    if len(ranked_ids) != len(set(ranked_ids)):
        raise BusinessError("E_INCONSISTENT", "rerank",
                            "duplicate item entering re-rank stage", query_id=query_id,
                            context={"ranked_ids": ranked_ids})
    if not ranked_ids:
        return []

    base_scores = {iid: native_scores[iid] for iid in ranked_ids}
    penalty = ctx.encode(category_penalty_f64)
    decay = ctx.encode(decay_f64)
    one = ctx.encode(1.0)

    remaining = list(ranked_ids)
    chosen: list[str] = []
    trace: list[dict] = []
    seen_categories: set[str] = set()
    decay_power = one
    while remaining:
        best_id = None
        best_adj = None
        best_weighted = None
        best_penalized = False
        for item_id in remaining:
            base = base_scores[item_id]
            weighted = ctx.mul(base, decay_power)
            if items_by_id[item_id].category in seen_categories:
                adj = ctx.sub(weighted, penalty)
                penalized = True
            else:
                adj = weighted
                penalized = False
            adj_cmp = int(adj) if ctx.is_fixed else float(adj)
            if best_adj is None or adj_cmp > best_adj or (adj_cmp == best_adj and item_id < best_id):
                best_adj = adj_cmp
                best_id = item_id
                best_weighted = float(ctx.decode(weighted))
                best_penalized = penalized
        chosen.append(best_id)
        seen_categories.add(items_by_id[best_id].category)
        trace.append({
            "position": len(chosen) - 1,
            "item_id": best_id,
            "category": items_by_id[best_id].category,
            "weighted_score": best_weighted,
            "adjusted_score": float(ctx.decode(best_adj)),
            "category_penalty_applied": best_penalized,
        })
        remaining.remove(best_id)
        decay_power = ctx.mul(decay_power, decay)

    if sorted(chosen) != sorted(ranked_ids):
        raise BusinessError("E_INCONSISTENT", "rerank",
                            "candidate set changed during re-rank", query_id=query_id,
                            context={"lost": sorted(set(ranked_ids) - set(chosen)),
                                     "gained": sorted(set(chosen) - set(ranked_ids)),
                                     "category": "candidate_set"})
    return chosen, trace

def verify_monotonicity(adjusted_trace: list[float]) -> bool:
    """Weighted (pre-penalty) score must be non-increasing for each category."""
    last_by_category: dict[str, float] = {}
    for row in adjusted_trace:
        cat = row["category"]
        score = row["weighted_score"]
        if cat in last_by_category and score > last_by_category[cat] + 1e-12:
            return False
        last_by_category[cat] = score
    return True
