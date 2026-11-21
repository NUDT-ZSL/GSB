from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from fractions import Fraction
from typing import Any

import numpy as np

from .filters import NodeFilterResult, topology_distribution
from .jsonutil import decimal_text
from .models import Pod, ScheduleConfig, labels_match
from .runtime import ClusterState, NodeState


@dataclass(frozen=True)
class RawScore:
    plugin: str
    fraction: Fraction
    detail: dict[str, Any]


@dataclass(frozen=True)
class ScoreSelection:
    node_scores: tuple[dict[str, Any], ...]
    selected_node: str | None
    precision_sensitive: bool
    tolerance: Decimal
    top_two_difference: Decimal | None
    error_sources: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        node_scores: list[dict[str, Any]] = []
        for item in self.node_scores:
            normalized = dict(item)
            normalized["plugins"] = [dict(plugin) for plugin in item["plugins"]]
            node_scores.append(normalized)
        return {
            "plugin_order": list(("resource_bin_packing", "node_affinity", "topology_balance")),
            "node_scores": node_scores,
            "selected_node": self.selected_node,
            "precision_sensitive": self.precision_sensitive,
            "tolerance": decimal_text(self.tolerance),
            "top_two_difference": (
                decimal_text(self.top_two_difference)
                if self.top_two_difference is not None
                else None
            ),
            "error_sources": [dict(source) for source in self.error_sources],
        }


def _rounding_constant(name: str):
    return ROUND_HALF_UP if name == "ROUND_HALF_UP" else ROUND_HALF_EVEN


def _decimal_quantum(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def _fraction_to_decimal(value: Fraction, places: int, rounding: str) -> Decimal:
    with localcontext() as context:
        context.prec = 60
        approximate = Decimal(value.numerator) / Decimal(value.denominator)
        return approximate.quantize(_decimal_quantum(places), rounding=_rounding_constant(rounding))


def _resource_score(pod: Pod, node: NodeState) -> RawScore:
    capacity = node.capacity
    remaining = node.remaining
    request = pod.resources
    cpu_after = capacity.cpu_millicores - remaining.cpu_millicores + request.cpu_millicores
    memory_after = capacity.memory_mib - remaining.memory_mib + request.memory_mib
    gpu_after = capacity.gpu - remaining.gpu + request.gpu
    utilization = [
        Fraction(cpu_after, capacity.cpu_millicores),
        Fraction(memory_after, capacity.memory_mib),
    ]
    if capacity.gpu > 0:
        utilization.append(Fraction(gpu_after, capacity.gpu))
    fraction = Fraction(100) * sum(utilization, Fraction(0)) / len(utilization)
    return RawScore(
        "resource_bin_packing",
        fraction,
        {
            "allocated_after": {
                "cpu_millicores": cpu_after,
                "memory_mib": memory_after,
                "gpu": gpu_after,
            },
            "utilization_after": [
                {
                    "numerator": item.numerator,
                    "denominator": item.denominator,
                }
                for item in utilization
            ],
            "objective": "higher_utilization_reduces_fragmentation",
        },
    )


def _affinity_score(pod: Pod, node: NodeState) -> RawScore:
    preferred = pod.node_affinity.preferred
    if not preferred:
        return RawScore(
            "node_affinity",
            Fraction(100),
            {"preferred_rules": 0, "matched_weight": 0, "total_weight": 0},
        )
    matched = 0
    total = 0
    details: list[dict[str, Any]] = []
    for index, rule in enumerate(preferred):
        total += rule.weight
        rule_matches = labels_match(node.labels, (), rule.expressions)
        if rule_matches:
            matched += rule.weight
        details.append(
            {
                "rule_index": index,
                "weight": rule.weight,
                "matched": rule_matches,
            }
        )
    return RawScore(
        "node_affinity",
        Fraction(100 * matched, total),
        {
            "preferred_rules": len(preferred),
            "matched_weight": matched,
            "total_weight": total,
            "rules": details,
        },
    )


def _topology_score(
    pod: Pod,
    node: NodeState,
    state: ClusterState,
) -> RawScore:
    if not pod.topology_spread:
        return RawScore(
            "topology_balance",
            Fraction(100),
            {"constraints": 0, "balance_scores": []},
        )
    scores: list[Fraction] = []
    details: list[dict[str, Any]] = []
    for index, constraint in enumerate(pod.topology_spread):
        domain = node.labels.get(constraint.topology_key)
        if domain is None:
            score = Fraction(0)
            details.append(
                {
                    "constraint_index": index,
                    "topology_key": constraint.topology_key,
                    "domain": None,
                    "skew_after": None,
                    "score": "0",
                }
            )
            scores.append(score)
            continue
        counts = topology_distribution(constraint, state, excluded_pod=pod.name)
        if domain not in counts:
            score = Fraction(0)
            skew = None
        else:
            after = dict(counts)
            after[domain] += 1
            skew = max(after.values()) - min(after.values())
            score = Fraction(100, 1 + skew)
        scores.append(score)
        details.append(
            {
                "constraint_index": index,
                "topology_key": constraint.topology_key,
                "domain": domain,
                "skew_after": skew,
                "score": f"{score.numerator}/{score.denominator}",
            }
        )
    return RawScore(
        "topology_balance",
        sum(scores, Fraction(0)) / len(scores),
        {"constraints": len(scores), "balance_scores": details},
    )


def raw_scores(
    pod: Pod,
    node: NodeState,
    state: ClusterState,
) -> tuple[RawScore, ...]:
    return (
        _resource_score(pod, node),
        _affinity_score(pod, node),
        _topology_score(pod, node, state),
    )


def score_nodes(
    pod: Pod,
    filters: tuple[NodeFilterResult, ...],
    state: ClusterState,
    config: ScheduleConfig,
) -> ScoreSelection:
    feasible_names = {result.node_name for result in filters if result.passed}
    nodes = sorted(
        (node for node in state.nodes if node.name in feasible_names),
        key=lambda item: item.name,
    )
    rounding = _rounding_constant(config.weight_rounding)
    weight_fractions = {
        plugin: Fraction(weight)
        for plugin, weight in config.score_weights.items()
    }
    weight_sum = sum(weight_fractions.values(), Fraction(0))
    raw_values: dict[str, dict[str, RawScore]] = {}
    normalized: dict[str, dict[str, Any]] = {}

    with localcontext() as context:
        context.prec = 60
        for plugin in ("resource_bin_packing", "node_affinity", "topology_balance"):
            exact_weight = weight_fractions[plugin] / weight_sum
            decimal_exact = Decimal(exact_weight.numerator) / Decimal(exact_weight.denominator)
            rounded_weight = decimal_exact.quantize(
                _decimal_quantum(config.weight_decimals),
                rounding=rounding,
            )
            rounded_fraction = Fraction(rounded_weight)
            if rounded_fraction > exact_weight:
                direction = "UP"
            elif rounded_fraction < exact_weight:
                direction = "DOWN"
            else:
                direction = "EXACT"
            normalized[plugin] = {
                "weight": config.score_weights[plugin],
                "normalized_weight": rounded_weight,
                "normalized_weight_exact": decimal_exact,
                "rounding_direction": direction,
                "rounding_abs_error": abs(rounded_fraction - exact_weight),
            }

    node_scores: list[dict[str, Any]] = []
    for node in nodes:
        raws = raw_scores(pod, node, state)
        raw_values[node.name] = {item.plugin: item for item in raws}
        plugin_rows: list[dict[str, Any]] = []
        terms: list[Decimal] = []
        error_bound = Fraction(0)
        for raw in raws:
            raw_decimal = _fraction_to_decimal(
                raw.fraction,
                config.score_decimals,
                config.weight_rounding,
            )
            raw_error = abs(Fraction(raw_decimal) - raw.fraction)
            weight_info = normalized[raw.plugin]
            normalized_weight: Decimal = weight_info["normalized_weight"]
            normalized_fraction: Fraction = Fraction(normalized_weight)
            exact_weight: Fraction = Fraction(
                weight_info["normalized_weight_exact"]
            )
            intermediate = raw_decimal * normalized_weight
            intermediate_error = (
                abs(Fraction(raw_decimal)) * abs(normalized_fraction - exact_weight)
                + abs(raw.fraction) * abs(Fraction(raw_decimal) - raw.fraction)
                + abs(normalized_fraction - exact_weight)
                * abs(Fraction(raw_decimal) - raw.fraction)
            )
            term_error = intermediate_error
            terms.append(intermediate)
            error_bound += term_error
            plugin_rows.append(
                {
                    "plugin": raw.plugin,
                    "raw_score": decimal_text(raw_decimal),
                    "raw_score_float": float(raw_decimal),
                    "raw_score_error_bound": decimal_text(
                        Decimal(raw_error.numerator) / Decimal(raw_error.denominator)
                    ),
                    "weight": decimal_text(weight_info["weight"]),
                    "weight_float": float(weight_info["weight"]),
                    "normalized_weight": decimal_text(normalized_weight),
                    "normalized_weight_float": float(normalized_weight),
                    "normalized_weight_exact": decimal_text(
                        weight_info["normalized_weight_exact"]
                    ),
                    "normalization_rounding": config.weight_rounding,
                    "rounding_direction": weight_info["rounding_direction"],
                    "weight_rounding_abs_error": decimal_text(
                        Decimal(weight_info["rounding_abs_error"].numerator)
                        / Decimal(weight_info["rounding_abs_error"].denominator)
                    ),
                    "normalized_intermediate": decimal_text(intermediate),
                    "normalized_intermediate_float": float(intermediate),
                    "intermediate_error_bound": decimal_text(
                        Decimal(intermediate_error.numerator)
                        / Decimal(intermediate_error.denominator)
                    ),
                    "detail": raw.detail,
                }
            )
        total_unrounded = sum(terms, Decimal(0))
        total = total_unrounded.quantize(
            _decimal_quantum(config.score_decimals),
            rounding=rounding,
        )
        total_round_error = abs(Fraction(total) - Fraction(total_unrounded))
        error_bound += total_round_error
        float_terms = np.asarray([float(term) for term in terms], dtype=np.float64)
        float_cross_check = float(np.sum(float_terms, dtype=np.float64))
        node_scores.append(
            {
                "node": node.name,
                "resource_version": node.resource_version,
                "plugins": plugin_rows,
                "weight_sum": decimal_text(
                    Decimal(weight_sum.numerator) / Decimal(weight_sum.denominator)
                ),
                "total_score": decimal_text(total),
                "total_score_float": float(total),
                "score_rounding": config.weight_rounding,
                "total_rounding_abs_error": decimal_text(
                    Decimal(total_round_error.numerator)
                    / Decimal(total_round_error.denominator)
                ),
                "error_bound": decimal_text(
                    Decimal(error_bound.numerator) / Decimal(error_bound.denominator)
                ),
                "error_bound_float": float(error_bound),
                "np_float64_cross_check": float_cross_check,
            }
        )

    ranked = sorted(
        node_scores,
        key=lambda item: (-Decimal(item["total_score"]), item["node"]),
    )
    if not ranked:
        return ScoreSelection((), None, False, config.precision_tolerance, None, ())
    selected = ranked[0]["node"]
    diff: Decimal | None = None
    sensitive = False
    error_sources: list[dict[str, Any]] = []
    if len(ranked) >= 2:
        diff = Decimal(ranked[0]["total_score"]) - Decimal(ranked[1]["total_score"])
        sensitive = abs(diff) < config.precision_tolerance
        if sensitive:
            for plugin in ("resource_bin_packing", "node_affinity", "topology_balance"):
                info = normalized[plugin]
                error_sources.append(
                    {
                        "kind": "weight_normalization",
                        "plugin": plugin,
                        "rounding": config.weight_rounding,
                        "direction": info["rounding_direction"],
                        "exact": decimal_text(info["normalized_weight_exact"]),
                        "rounded": decimal_text(info["normalized_weight"]),
                        "max_abs_error": decimal_text(
                            Decimal(info["rounding_abs_error"].numerator)
                            / Decimal(info["rounding_abs_error"].denominator)
                        ),
                    }
                )
            error_sources.append(
                {
                    "kind": "raw_score_quantization",
                    "decimal_places": config.score_decimals,
                    "max_abs_error_per_plugin": decimal_text(
                        Decimal(1) / Decimal(2 * 10**config.score_decimals)
                    ),
                }
            )
            error_sources.append(
                {
                    "kind": "total_score_quantization",
                    "decimal_places": config.score_decimals,
                    "max_abs_error": decimal_text(
                        Decimal(1) / Decimal(2 * 10**config.score_decimals)
                    ),
                }
            )
            error_sources.append(
                {
                    "kind": "candidate_comparison",
                    "selected_node": ranked[0]["node"],
                    "runner_up_node": ranked[1]["node"],
                    "difference": decimal_text(abs(diff)),
                    "tolerance": decimal_text(config.precision_tolerance),
                    "selected_error_bound": ranked[0]["error_bound"],
                    "runner_up_error_bound": ranked[1]["error_bound"],
                    "tie_breaker": "lexicographically_smaller_node_name",
                }
            )
    return ScoreSelection(
        node_scores=tuple(node_scores),
        selected_node=selected,
        precision_sensitive=sensitive,
        tolerance=config.precision_tolerance,
        top_two_difference=diff,
        error_sources=tuple(error_sources),
    )
