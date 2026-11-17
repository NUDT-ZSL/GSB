"""Integer fixed-point estimates with explicit truncation and error bounds.

All non-negative values use floor truncation.  If ``q = floor(S * n / d)`` then
``0 <= S*n/d - q < 1`` fixed-point unit.  A weighted term with integer weight
``w`` therefore contributes strictly less than ``w`` fixed-point units of
absolute error.  The bounds are deliberately conservative and independent of
the scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Sequence

from .errors import GridError
from .util import ceil_div, is_int, is_positive_int


MAX_GRID = 10**12


@dataclass(frozen=True)
class Quantized:
    """One floor-quantized rational value."""

    value: int
    scale: int
    numerator: int
    denominator: int
    remainder: int
    error_bound_units: int

    @property
    def normalized_error_upper(self) -> Fraction:
        return Fraction(self.error_bound_units, self.scale)

    @property
    def exact(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def to_dict(self) -> dict[str, int | str]:
        return {
            "value_fixed": self.value,
            "scale": self.scale,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "remainder": self.remainder,
            "error_bound_units": self.error_bound_units,
            "truncation": "floor",
        }


def validate_scale(scale: object, field_path: str = "session.scale") -> int:
    if not is_positive_int(scale):
        raise GridError(
            "定点缩放因子必须是正整数",
            field_path,
            details={"actual": repr(scale)},
        )
    if scale > MAX_GRID:
        raise GridError(
            "定点缩放因子超出允许上界",
            field_path,
            details={"actual": scale, "max": MAX_GRID},
        )
    return int(scale)


def quantize_fraction(numerator: int, denominator: int, scale: int) -> Quantized:
    """Floor-quantize a non-negative rational using exact integer arithmetic."""

    scale = validate_scale(scale)
    if numerator < 0:
        raise ValueError("fixed-point inputs must be non-negative")
    if not is_positive_int(denominator):
        raise ValueError("denominator must be a positive integer")
    value, remainder = divmod(numerator * scale, denominator)
    return Quantized(
        value=value,
        scale=scale,
        numerator=numerator,
        denominator=denominator,
        remainder=remainder,
        # Strictly less than one unit.  The integer bound is 1 for callers
        # that require a finite inclusive bound.
        error_bound_units=1 if remainder else 0,
    )


@dataclass(frozen=True)
class RatioAlert:
    ratio: Quantized
    threshold: Quantized
    scale: int
    gap_units: int
    gap_error_bound_units: int
    observed_above: bool
    exact_above: bool
    certainty: str

    @property
    def decision(self) -> str:
        return self.certainty

    def to_dict(self) -> dict[str, object]:
        return {
            "scale": self.scale,
            "ratio": self.ratio.to_dict(),
            "threshold": self.threshold.to_dict(),
            "gap_units": self.gap_units,
            "gap_error_bound_units": self.gap_error_bound_units,
            "observed_above": self.observed_above,
            "exact_above": self.exact_above,
            "certainty": self.certainty,
            "decision": self.decision,
            "truncation": "floor",
        }


def classify_ratio(
    mismatch_count: int,
    replica_count: int,
    threshold_numerator: int,
    threshold_denominator: int,
    scale: int,
) -> RatioAlert:
    """Compare a mismatch ratio against a policy threshold.

    The exact relation is ``mismatch_count/replica_count > threshold``.  The
    quantized difference has error in ``(-1, 1)`` fixed units.  Consequently a
    non-negative gap of at least one proves the strict relation, while a gap of
    at most minus one proves the opposite.  A zero gap is deliberately reported
    as indeterminate rather than silently choosing a side.
    """

    if replica_count <= 0:
        raise ValueError("replica_count must be positive")
    if mismatch_count < 0 or mismatch_count > replica_count:
        raise ValueError("mismatch_count outside [0, replica_count]")
    scale = validate_scale(scale)
    ratio = quantize_fraction(mismatch_count, replica_count, scale)
    threshold = quantize_fraction(threshold_numerator, threshold_denominator, scale)
    gap = ratio.value - threshold.value
    exact_ratio = Fraction(mismatch_count, replica_count)
    exact_threshold = Fraction(threshold_numerator, threshold_denominator)
    exact_above = exact_ratio > exact_threshold
    if mismatch_count == 0:
        # Zero is exactly zero; reporting indeterminate here would be needlessly
        # noisy and cannot hide a positive mismatch.
        certainty = "not_above"
    elif gap >= 1:
        certainty = "above"
    elif gap <= -1:
        certainty = "not_above"
    else:
        certainty = "indeterminate"
    return RatioAlert(
        ratio=ratio,
        threshold=threshold,
        scale=scale,
        gap_units=gap,
        gap_error_bound_units=1,
        observed_above=gap > 0,
        exact_above=exact_above,
        certainty=certainty,
    )


@dataclass(frozen=True)
class CostTerm:
    name: str
    fixed_value: int
    weight: int
    weighted_value: int
    error_bound_units: int
    weighted_error_bound_units: int
    numerator: int
    denominator: int
    remainder: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "name": self.name,
            "fixed_value": self.fixed_value,
            "weight": self.weight,
            "weighted_value": self.weighted_value,
            "error_bound_units": self.error_bound_units,
            "weighted_error_bound_units": self.weighted_error_bound_units,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "remainder": self.remainder,
        }


@dataclass(frozen=True)
class CostEstimate:
    value_fixed: int
    scale: int
    error_bound_units: int
    terms: tuple[CostTerm, ...]
    exact_value: Fraction
    priority: int
    repair_load: int
    node_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "value_fixed": self.value_fixed,
            "scale": self.scale,
            "error_bound_units": self.error_bound_units,
            "terms": [term.to_dict() for term in self.terms],
            "exact_numerator": self.exact_value.numerator,
            "exact_denominator": self.exact_value.denominator,
            "priority": self.priority,
            "repair_load": self.repair_load,
            "node_id": self.node_id,
        }


@dataclass(frozen=True)
class RankedCost:
    candidate: CostEstimate
    cluster_index: int
    tie_key: tuple[int, str]
    tie_channel: bool


@dataclass(frozen=True)
class CostSelection:
    winner: CostEstimate
    ranked: tuple[RankedCost, ...]
    tie_channel: bool
    deciding_gap_units: int
    pair_error_bound_units: int
    tie_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "winner_node_id": self.winner.node_id,
            "winner": self.winner.to_dict(),
            "ranked": [
                {
                    "cluster_index": item.cluster_index,
                    "tie_channel": item.tie_channel,
                    "tie_key": list(item.tie_key),
                    **item.candidate.to_dict(),
                }
                for item in self.ranked
            ],
            "tie_channel": self.tie_channel,
            "deciding_gap_units": self.deciding_gap_units,
            "pair_error_bound_units": self.pair_error_bound_units,
            "tie_reason": self.tie_reason,
        }


def _cost_term(
    name: str,
    numerator: int,
    denominator: int,
    scale: int,
    weight: int,
) -> CostTerm:
    q = quantize_fraction(numerator, denominator, scale)
    return CostTerm(
        name=name,
        fixed_value=q.value,
        weight=weight,
        weighted_value=q.value * weight,
        error_bound_units=q.error_bound_units,
        weighted_error_bound_units=q.error_bound_units * weight,
        numerator=numerator,
        denominator=denominator,
        remainder=q.remainder,
    )


def estimate_transfer(
    byte_size: int,
    overhead_numerator: int,
    overhead_denominator: int,
    scale: int,
) -> Quantized:
    """Estimate wire transfer including a rational protocol overhead."""

    return quantize_fraction(
        byte_size * (overhead_denominator + overhead_numerator),
        overhead_denominator,
        scale,
    )


def estimate_workload(
    byte_size: int, work_numerator: int, work_denominator: int, scale: int
) -> Quantized:
    return quantize_fraction(byte_size * work_numerator, work_denominator, scale)


def estimate_source_cost(
    *,
    node_id: str,
    priority: int,
    repair_load: int,
    max_priority: int,
    max_load: int,
    byte_size: int,
    scale: int,
    transfer_weight: int,
    work_weight: int,
    priority_weight: int,
    load_weight: int,
    overhead_numerator: int,
    overhead_denominator: int,
    work_numerator: int,
    work_denominator: int,
) -> CostEstimate:
    transfer = estimate_transfer(
        byte_size, overhead_numerator, overhead_denominator, scale
    )
    workload = estimate_workload(byte_size, work_numerator, work_denominator, scale)
    priority_term = _cost_term(
        "priority",
        max_priority - priority,
        max_priority,
        scale,
        priority_weight,
    )
    load_term = _cost_term(
        "load", repair_load, max(1, max_load), scale, load_weight
    )
    transfer_term = CostTerm(
        name="transfer",
        fixed_value=transfer.value,
        weight=transfer_weight,
        weighted_value=transfer.value * transfer_weight,
        error_bound_units=transfer.error_bound_units,
        weighted_error_bound_units=transfer.error_bound_units * transfer_weight,
        numerator=transfer.numerator,
        denominator=transfer.denominator,
        remainder=transfer.remainder,
    )
    work_term = CostTerm(
        name="workload",
        fixed_value=workload.value,
        weight=work_weight,
        weighted_value=workload.value * work_weight,
        error_bound_units=workload.error_bound_units,
        weighted_error_bound_units=workload.error_bound_units * work_weight,
        numerator=workload.numerator,
        denominator=workload.denominator,
        remainder=workload.remainder,
    )
    terms = (transfer_term, work_term, priority_term, load_term)
    value = sum(term.weighted_value for term in terms)
    bound = sum(term.weighted_error_bound_units for term in terms)
    exact = (
        Fraction(byte_size * (overhead_denominator + overhead_numerator), overhead_denominator)
        * transfer_weight
        + Fraction(byte_size * work_numerator, work_denominator) * work_weight
        + Fraction((max_priority - priority) * priority_weight, max_priority)
        + Fraction(repair_load * load_weight, max(1, max_load))
    )
    return CostEstimate(
        value_fixed=value,
        scale=scale,
        error_bound_units=bound,
        terms=terms,
        exact_value=exact,
        priority=priority,
        repair_load=repair_load,
        node_id=node_id,
    )


def rank_candidates(candidates: Sequence[CostEstimate]) -> CostSelection:
    """Sort candidates with an explicit error-bound tie channel.

    Candidates are first ordered by (estimated cost, higher priority, node id).
    Starting at the cheapest cost, any later candidate whose cost differs by no
    more than the sum of both candidates' derived error bounds joins the same
    cluster.  Clusters are then ordered by their cheapest member.  Inside a
    cluster the stable tie rule (priority descending, node id ascending) chooses
    the winner.  No hash iteration order is used.
    """

    if not candidates:
        raise ValueError("at least one cost candidate is required")
    ordered = sorted(
        candidates,
        key=lambda item: (item.value_fixed, -item.priority, item.node_id),
    )
    clusters: list[list[CostEstimate]] = []
    for candidate in ordered:
        if not clusters:
            clusters.append([candidate])
            continue
        cluster = clusters[-1]
        anchor = min(item.value_fixed for item in cluster)
        bound = max(
            item.error_bound_units + candidate.error_bound_units for item in cluster
        )
        if candidate.value_fixed - anchor <= bound:
            cluster.append(candidate)
        else:
            clusters.append([candidate])

    ranked: list[RankedCost] = []
    for cluster_index, cluster in enumerate(clusters):
        tie_channel = len(cluster) > 1
        cluster_sorted = sorted(cluster, key=lambda item: (-item.priority, item.node_id))
        for candidate in cluster_sorted:
            ranked.append(
                RankedCost(
                    candidate=candidate,
                    cluster_index=cluster_index,
                    tie_key=(-candidate.priority, candidate.node_id),
                    tie_channel=tie_channel,
                )
            )
    winner = ranked[0].candidate
    winner_cluster_index = ranked[0].cluster_index
    winner_cluster = clusters[winner_cluster_index]
    if len(clusters) > 1:
        next_cluster = clusters[winner_cluster_index + 1]
        nearest_next = min(
            next_cluster,
            key=lambda item: (item.value_fixed, -item.priority, item.node_id),
        )
        gap = nearest_next.value_fixed - winner.value_fixed
        pair_bound = winner.error_bound_units + nearest_next.error_bound_units
    elif len(winner_cluster) > 1:
        competitor_pairs = [
            (abs(left.value_fixed - right.value_fixed), left, right)
            for left in winner_cluster
            for right in winner_cluster
            if left is not right
        ]
        gap, left, right = min(
            competitor_pairs,
            key=lambda item: (item[0], item[1].node_id, item[2].node_id),
        )
        pair_bound = left.error_bound_units + right.error_bound_units
    else:
        gap = 0
        pair_bound = 0
    tie_reason = (
        "cost_gap_within_error_bound:priority_desc_then_node_id_asc"
        if ranked[0].tie_channel
        else "cost_gap_exceeds_error_bound"
    )
    return CostSelection(
        winner=winner,
        ranked=tuple(ranked),
        tie_channel=ranked[0].tie_channel,
        deciding_gap_units=gap,
        pair_error_bound_units=pair_bound,
        tie_reason=tie_reason,
    )


def aggregate_error_bound(terms: Iterable[int]) -> int:
    return sum(int(term) for term in terms)


def error_bound_share_ppm(bound_units: int, gap_units: int) -> int:
    """Integer ppm ratio of an error bound to a decision gap.

    The denominator is clamped to one fixed unit so perfectly tied values still
    produce a finite integer.  Values above 1,000,000 mean the bound exceeds the
    observed gap.
    """

    return ceil_div(max(0, bound_units) * 1_000_000, max(1, abs(gap_units)))
