from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Mapping, Sequence

from .filters import (
    FilterContext,
    _label_expression_matches,
    topology_counts_after_placement,
)
from .models import SCORE_PLUGINS


QUANTIZATION_SCALE = 6


def _round_half_up(value: Fraction, places: int = QUANTIZATION_SCALE) -> Fraction:
    factor = 10**places
    scaled_numerator = value.numerator * factor
    denominator = value.denominator
    if scaled_numerator >= 0:
        rounded = (2 * scaled_numerator + denominator) // (2 * denominator)
    else:
        rounded = -(
            (-2 * scaled_numerator + denominator) // (2 * denominator)
        )
    return Fraction(rounded, factor)


def _fraction_text(value: Fraction, places: int = QUANTIZATION_SCALE) -> str:
    quantized = _round_half_up(value, places)
    factor = 10**places
    scaled = quantized.numerator * factor // quantized.denominator
    sign = "-" if scaled < 0 else ""
    scaled = abs(scaled)
    whole, fraction = divmod(scaled, factor)
    return f"{sign}{whole}.{fraction:0{places}d}"


def _quantization_error_bound(term_count: int) -> str:
    scaled_units = term_count * 5
    return f"0.{scaled_units:07d}"


def _raw_resource_bin_pack(context: FilterContext) -> tuple[int, dict[str, Any]]:
    node = context.node
    cpu_after = (
        int(node["cpu_total"])
        - int(node["cpu_remaining"])
        + context.pod.cpu_millicores
    )
    memory_after = (
        int(node["memory_total"])
        - int(node["memory_remaining"])
        + context.pod.memory_mib
    )
    cpu_util_ppm = (cpu_after * 1000) // int(node["cpu_total"])
    memory_util_ppm = (memory_after * 1000) // int(node["memory_total"])
    raw = (cpu_util_ppm + memory_util_ppm) // 2
    return raw, {
        "cpu_after": cpu_after,
        "cpu_total": int(node["cpu_total"]),
        "cpu_utilization_per_mille": cpu_util_ppm,
        "memory_after": memory_after,
        "memory_total": int(node["memory_total"]),
        "memory_utilization_per_mille": memory_util_ppm,
    }


def _raw_node_affinity(
    context: FilterContext,
) -> tuple[int, dict[str, Any]]:
    preferred = context.pod.node_affinity.preferred
    if not preferred:
        return 1000, {"preferred_terms": 0, "matched_terms": 0}
    labels = context.node["labels"]
    total_weight = sum(expression.weight for expression in preferred)
    matched_weight = sum(
        expression.weight
        for expression in preferred
        if _label_expression_matches(expression, labels)
    )
    raw = (matched_weight * 1000) // total_weight
    return raw, {
        "preferred_terms": len(preferred),
        "matched_terms": sum(
            1
            for expression in preferred
            if _label_expression_matches(expression, labels)
        ),
        "total_weight": total_weight,
        "matched_weight": matched_weight,
    }


def _raw_topology_balance(
    context: FilterContext,
) -> tuple[int, dict[str, Any]]:
    if not context.pod.topology_spread:
        return 1000, {"constraints": []}
    details: list[dict[str, Any]] = []
    total = 0
    for constraint in context.pod.topology_spread:
        counts, candidate_domain = topology_counts_after_placement(
            context=context,
            constraint=constraint,
        )
        maximum = max(counts.values(), default=0)
        minimum = min(counts.values(), default=0)
        skew = maximum - minimum
        constraint_score = max(0, 1000 - skew * 250)
        total += constraint_score
        details.append(
            {
                "topology_key": constraint.topology_key,
                "label_selector": dict(constraint.label_selector),
                "candidate_domain": candidate_domain,
                "counts_after_placement": dict(sorted(counts.items())),
                "skew_after_placement": skew,
                "constraint_score": constraint_score,
            }
        )
    return total // len(context.pod.topology_spread), {"constraints": details}


RAW_SCORE_FUNCTIONS = {
    "ResourceBinPack": _raw_resource_bin_pack,
    "NodeAffinityMatch": _raw_node_affinity,
    "TopologySpreadBalance": _raw_topology_balance,
}


@dataclass(frozen=True)
class NodeScore:
    node: str
    raw_scores: Mapping[str, int]
    weights: Mapping[str, Decimal]
    normalized_weights: Mapping[str, str]
    normalized_terms: Mapping[str, str]
    total_exact: Fraction
    total_quantized_fraction: Fraction
    total_quantized: str
    weight_sum_is_integer: bool
    error_upper_bound: str
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "raw_scores": dict(self.raw_scores),
            "weights": {
                key: _decimal_text(value)
                for key, value in sorted(self.weights.items())
            },
            "normalized_weights": dict(sorted(self.normalized_weights.items())),
            "normalized_terms": dict(sorted(self.normalized_terms.items())),
            "total_score": self.total_quantized,
            "weight_sum_is_integer": self.weight_sum_is_integer,
            "error_upper_bound": self.error_upper_bound,
            "details": dict(self.details),
        }


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value.normalize(), "f")


def score_node(
    context: FilterContext,
    weights: Mapping[str, Decimal],
) -> NodeScore:
    raw_scores: dict[str, int] = {}
    details: dict[str, Any] = {}
    for plugin in SCORE_PLUGINS:
        raw, plugin_details = RAW_SCORE_FUNCTIONS[plugin](context)
        raw_scores[plugin] = raw
        details[plugin] = plugin_details
    weight_fractions = {
        plugin: Fraction(weights[plugin]) for plugin in SCORE_PLUGINS
    }
    total_weight = sum(weight_fractions.values(), Fraction(0))
    terms: dict[str, Fraction] = {}
    quantized_terms: dict[str, Fraction] = {}
    normalized_weights: dict[str, str] = {}
    normalized_text: dict[str, str] = {}
    for plugin in SCORE_PLUGINS:
        normalized_weight = weight_fractions[plugin] / total_weight
        term = Fraction(raw_scores[plugin]) * normalized_weight
        normalized_weights[plugin] = _fraction_text(normalized_weight)
        terms[plugin] = term
        quantized_terms[plugin] = _round_half_up(term)
        normalized_text[plugin] = _fraction_text(quantized_terms[plugin])
    total_exact = sum(terms.values(), Fraction(0))
    total_quantized_fraction = sum(quantized_terms.values(), Fraction(0))
    return NodeScore(
        node=str(context.node["name"]),
        raw_scores=raw_scores,
        weights=dict(weights),
        normalized_weights=normalized_weights,
        normalized_terms=normalized_text,
        total_exact=total_exact,
        total_quantized_fraction=total_quantized_fraction,
        total_quantized=_fraction_text(total_quantized_fraction),
        weight_sum_is_integer=total_weight.denominator == 1,
        error_upper_bound=_quantization_error_bound(len(SCORE_PLUGINS)),
        details=details,
    )


def score_nodes(
    contexts: Sequence[FilterContext],
    weights: Mapping[str, Decimal],
) -> dict[str, Any]:
    scores = [score_node(context, weights) for context in contexts]
    scores.sort(key=lambda score: (-score.total_quantized_fraction, score.node))
    weight_sum = sum((Fraction(value) for value in weights.values()), Fraction(0))
    return {
        "weight_sum": _fraction_text(weight_sum),
        "weight_sum_is_integer": weight_sum.denominator == 1,
        "quantization": {
            "scale": QUANTIZATION_SCALE,
            "mode": "ROUND_HALF_UP",
            "error_upper_bound": _quantization_error_bound(len(SCORE_PLUGINS)),
            "description": (
                "Each normalized term is quantized to six decimal places and the "
                "reported total is the exact sum of the quantized terms."
            ),
        },
        "ranking": [score.to_dict() for score in scores],
        "_ranking_order": [score.node for score in scores],
    }
