"""Deterministic feature construction shared by every precision tier."""
from __future__ import annotations

import numpy as np

from .errors import BusinessError
from .models import CandidateSet
from .precision import PrecisionContext

FEATURE_NAMES = ("term_match", "category_match", "price_bucket", "quality_scaled")

# Fixed price bucket edges in the input's native float64 domain; encoding into
# the active precision tier happens through the shared operators only.
PRICE_EDGES = (10.0, 50.0, 150.0, 300.0)
PRICE_BUCKET_SCORES = (0.9, 0.75, 0.55, 0.35, 0.15)


def validate_candidates(cand: CandidateSet, bounds: dict) -> None:
    """Input validation producing structured business errors per requirement 6."""
    qid = cand.query.query_id
    seen_items: set[str] = set()
    for item in cand.items:
        if item.item_id in seen_items:
            raise BusinessError("E_INCONSISTENT", "validate",
                                f"duplicate item_id {item.item_id}", query_id=qid,
                                context={"item_id": item.item_id})
        seen_items.add(item.item_id)
        if len(item.item_terms) == 0:
            raise BusinessError("E_ITEM_EMPTY", "validate",
                                f"item {item.item_id} has empty item_terms",
                                query_id=qid, context={"item_id": item.item_id})
        if not (bounds["price_min"] <= item.price <= bounds["price_max"]):
            raise BusinessError("E_VALUE_RANGE", "validate",
                                f"price {item.price} out of bounds",
                                query_id=qid, field="price",
                                context={"item_id": item.item_id, "value": item.price,
                                         "bounds": [bounds["price_min"], bounds["price_max"]]})
        if not (bounds["quality_min"] <= item.quality <= bounds["quality_max"]):
            raise BusinessError("E_VALUE_RANGE", "validate",
                                f"quality {item.quality} out of bounds",
                                query_id=qid, field="quality",
                                context={"item_id": item.item_id, "value": item.quality,
                                         "bounds": [bounds["quality_min"], bounds["quality_max"]]})


def build_features(cand: CandidateSet, ctx: PrecisionContext) -> tuple[np.ndarray, list[str]]:
    """Build the 4-column feature matrix using only the tier's operators.

    Returned matrix rows align with the sorted item_id list.
    """
    q_terms = set(cand.query.query_terms)
    n = len(cand.items)
    matrix = np.empty((n, len(FEATURE_NAMES)),
                      dtype=object if ctx.is_fixed else ctx.dtype)
    for row, item in enumerate(cand.items):
        # 1. term match: intersection count divided by query term count
        overlap = float(len(q_terms.intersection(item.item_terms)))
        denom = float(len(q_terms)) if q_terms else 1.0
        term_match = ctx.encode(overlap / denom)

        # 2. category match: deterministic function of query/category tokens
        cat_hit = 1.0 if item.category in cand.query.query_text or _category_term_hit(
            item.category, cand.query.query_terms) else 0.0
        category_match = ctx.encode(cat_hit)

        # 3. price bucket: map bucket index to a fixed score, via tier operators
        bucket_idx = _bucket_index(item.price)
        price_bucket = ctx.encode(PRICE_BUCKET_SCORES[bucket_idx])

        # 4. scaled quality: min-max style scaling computed with tier operators
        quality = ctx.encode(item.quality)
        qmin = ctx.encode(0.0)
        qspan = ctx.encode(1.0)
        quality_scaled = ctx.div(ctx.sub(quality, qmin), qspan)

        matrix[row, 0] = term_match
        matrix[row, 1] = category_match
        matrix[row, 2] = price_bucket
        matrix[row, 3] = quality_scaled
    return matrix, [it.item_id for it in cand.items]


def _category_term_hit(category: str, query_terms) -> bool:
    cat_tokens = set(category.split("_"))
    return any(term in cat_tokens for term in query_terms)


def _bucket_index(price: float) -> int:
    for idx, edge in enumerate(PRICE_EDGES):
        if price < edge:
            return idx
    return len(PRICE_EDGES)


def score_features(matrix: np.ndarray, weights: dict[str, float], bias: float,
                   ctx: PrecisionContext):
    """Linear score followed by sigmoid, both executed on the active tier."""
    ordered_weights = [weights[name] for name in FEATURE_NAMES]
    linear = ctx.linear_dot(matrix, ordered_weights, bias)
    sigmoid = ctx.sigmoid(linear)
    sigmoid = ctx.flush_subnormal(sigmoid)
    return linear, sigmoid
