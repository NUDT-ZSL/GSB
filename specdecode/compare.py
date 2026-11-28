"""Parameter-grid comparison and request-level decision flip reporting."""

from __future__ import annotations

from itertools import product
from typing import Any, Mapping, Sequence

from .decoder import aggregate_results
from .loader import LoadedDataset, Policy
from .model import FALLBACK_RULE
from .reference import compare_result_lists, reference_run
from .runner import run_policy


class ComparisonError(RuntimeError):
    """Raised when an independent strategy recomputation disagrees."""


def grid_policies(
    widths: Sequence[int],
    thresholds_bps: Sequence[int],
    *,
    budget_ticks: int,
) -> list[Policy]:
    if not widths or not thresholds_bps:
        raise ValueError("comparison grid needs at least one width and threshold")
    if any(int(width) <= 0 for width in widths):
        raise ValueError("candidate widths must be positive")
    if any(not 0 <= int(threshold) <= 10000 for threshold in thresholds_bps):
        raise ValueError("acceptance thresholds must be within 0..10000 bps")
    seen: set[str] = set()
    policies: list[Policy] = []
    for width, threshold in product(widths, thresholds_bps):
        policy_id = f"grid-w{int(width)}-t{int(threshold)}"
        if policy_id in seen:
            continue
        seen.add(policy_id)
        policies.append(
            Policy(
                policy_id=policy_id,
                candidate_length=int(width),
                acceptance_threshold_bps=int(threshold),
                fallback_rule=FALLBACK_RULE,
                latency_budget_ticks=int(budget_ticks),
            )
        )
    return policies


def _applied_signature(result: Mapping[str, Any]) -> list[tuple[int, int, int]]:
    signature: list[tuple[int, int, int]] = []
    for round_record in result["rounds"]:
        for decision in round_record["decisions"]:
            if decision["applied"]:
                signature.append(
                    (
                        int(round_record["round_index"]),
                        int(decision["position"]),
                        int(decision["draft_token_id"]),
                    )
                )
        fallback = round_record["fallback"]
        if fallback is not None:
            signature.append(
                (
                    int(round_record["round_index"]),
                    -1,
                    int(fallback["resampled_internal_id"]),
                )
            )
    return signature


def _decision_verdict_flips(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    baseline_decisions = {
        (int(round_record["round_index"]), int(decision["position"])): decision
        for round_record in baseline["rounds"]
        for decision in round_record["decisions"]
    }
    candidate_decisions = {
        (int(round_record["round_index"]), int(decision["position"])): decision
        for round_record in candidate["rounds"]
        for decision in round_record["decisions"]
    }
    flips: list[dict[str, Any]] = []
    for key in sorted(set(baseline_decisions) & set(candidate_decisions)):
        left = baseline_decisions[key]
        right = candidate_decisions[key]
        if int(left["draft_token_id"]) != int(right["draft_token_id"]):
            continue
        if left["verdict"] == right["verdict"]:
            continue
        flips.append(
            {
                "round_index": key[0],
                "position": key[1],
                "draft_token_id": int(left["draft_token_id"]),
                "from": left["verdict"],
                "to": right["verdict"],
                "baseline_ratio_bps": int(left["ratio_bps"]),
                "candidate_ratio_bps": int(right["ratio_bps"]),
                "baseline_threshold_bps": int(left["threshold_bps"]),
                "candidate_threshold_bps": int(right["threshold_bps"]),
            }
        )
    return flips


def _request_flip(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any] | None:
    changed: list[str] = []
    if baseline["final_internal_ids"] != candidate["final_internal_ids"]:
        changed.append("final_token_sequence")
    if baseline["end_reason"] != candidate["end_reason"]:
        changed.append("end_reason")
    if bool(baseline["stats"]["within_budget"]) != bool(
        candidate["stats"]["within_budget"]
    ):
        changed.append("latency_budget_status")
    if int(baseline["stats"]["fallback_count"]) != int(
        candidate["stats"]["fallback_count"]
    ):
        changed.append("fallback_count")
    if _applied_signature(baseline) != _applied_signature(candidate):
        changed.append("applied_decision_path")
    decision_flips = _decision_verdict_flips(baseline, candidate)
    if decision_flips:
        changed.append("position_verdicts")
    if not changed:
        return None
    return {
        "request_id": baseline["request_id"],
        "changed": changed,
        "final_token_count": {
            "from": int(baseline["final_token_count"]),
            "to": int(candidate["final_token_count"]),
        },
        "end_reason": {
            "from": baseline["end_reason"],
            "to": candidate["end_reason"],
        },
        "acceptance_rate_bps": {
            "from": int(baseline["stats"]["acceptance_rate_bps"]),
            "to": int(candidate["stats"]["acceptance_rate_bps"]),
        },
        "fallback_count": {
            "from": int(baseline["stats"]["fallback_count"]),
            "to": int(candidate["stats"]["fallback_count"]),
        },
        "within_budget": {
            "from": bool(baseline["stats"]["within_budget"]),
            "to": bool(candidate["stats"]["within_budget"]),
        },
        "final_internal_ids": {
            "from": list(baseline["final_internal_ids"]),
            "to": list(candidate["final_internal_ids"]),
        },
        "decision_verdict_flips": decision_flips,
    }


def compare_policies(
    dataset: LoadedDataset,
    policies: Sequence[Policy],
    *,
    database_path: str | None = None,
    source_schema: int | None = None,
    independently_verify: bool = True,
) -> dict[str, Any]:
    if not policies:
        raise ValueError("at least one policy is required")
    policy_ids = [policy.policy_id for policy in policies]
    if len(policy_ids) != len(set(policy_ids)):
        raise ValueError("comparison policy IDs must be unique")

    runs: list[dict[str, Any]] = []
    for policy in policies:
        run = run_policy(
            dataset,
            policy,
            database_path=database_path,
            source_schema=source_schema,
        )
        if independently_verify:
            errors = compare_result_lists(
                reference_run(dataset, policy), run["requests"]
            )
            if errors:
                raise ComparisonError(
                    f"{policy.policy_id}: independent reference mismatch: "
                    + "; ".join(errors[:5])
                )
        runs.append(run)

    baseline = runs[0]
    baseline_by_request = {
        result["request_id"]: result for result in baseline["requests"]
    }

    strategies: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    for run in runs:
        aggregate = run["aggregate"]
        request_classes = sorted(
            {str(result["request_class"]) for result in run["requests"]}
        )
        class_metrics = {}
        for request_class in request_classes:
            class_aggregate = aggregate_results(
                [
                    result
                    for result in run["requests"]
                    if result["request_class"] == request_class
                ]
            )
            class_metrics[request_class] = {
                "request_count": int(class_aggregate["request_count"]),
                "acceptance_rate_bps": int(
                    class_aggregate["acceptance_rate_bps"]
                ),
                "fallback_count": int(class_aggregate["fallback_count"]),
                "budget_attainment_bps": int(
                    class_aggregate["budget_attainment_bps"]
                ),
                "speedup_ppm": int(class_aggregate["speedup_ppm"]),
                "total_ticks": int(class_aggregate["total_ticks"]),
                "baseline_ticks": int(class_aggregate["baseline_ticks"]),
            }
        strategy = {
            "policy_id": run["policy"]["policy_id"],
            "candidate_length": int(run["policy"]["candidate_length"]),
            "acceptance_threshold_bps": int(
                run["policy"]["acceptance_threshold_bps"]
            ),
            "latency_budget_ticks": int(
                run["policy"]["latency_budget_ticks"]
            ),
            "run_key": run["run_key"],
            "acceptance_rate_bps": int(aggregate["acceptance_rate_bps"]),
            "fallback_count": int(aggregate["fallback_count"]),
            "budget_attainment_bps": int(aggregate["budget_attainment_bps"]),
            "within_budget_count": int(aggregate["within_budget_count"]),
            "total_ticks": int(aggregate["total_ticks"]),
            "baseline_ticks": int(aggregate["baseline_ticks"]),
            "speedup_ppm": int(aggregate["speedup_ppm"]),
            "final_token_count": int(aggregate["final_token_count"]),
            "end_reason_counts": dict(aggregate["end_reason_counts"]),
            "request_class_metrics": class_metrics,
            "reference_verified": bool(independently_verify),
        }
        strategies.append(strategy)

        flips = []
        for result in run["requests"]:
            flip = _request_flip(
                baseline_by_request[result["request_id"]], result
            )
            if flip is not None:
                flips.append(flip)
        comparisons.append(
            {
                "baseline_policy_id": baseline["policy"]["policy_id"],
                "policy_id": run["policy"]["policy_id"],
                "flipped_request_count": len(flips),
                "flips": flips,
            }
        )

    ranking = sorted(
        strategies,
        key=lambda item: (
            -item["budget_attainment_bps"],
            -item["speedup_ppm"],
            item["fallback_count"],
            item["candidate_length"],
            item["acceptance_threshold_bps"],
        ),
    )
    recommended = ranking[0]
    request_classes = sorted(
        {
            request_class
            for strategy in strategies
            for request_class in strategy["request_class_metrics"]
        }
    )
    best_by_request_class: dict[str, dict[str, Any]] = {}
    for request_class in request_classes:
        candidates = [
            strategy
            for strategy in strategies
            if request_class in strategy["request_class_metrics"]
        ]
        best = sorted(
            candidates,
            key=lambda strategy: (
                -strategy["request_class_metrics"][request_class][
                    "budget_attainment_bps"
                ],
                -strategy["request_class_metrics"][request_class][
                    "speedup_ppm"
                ],
                strategy["request_class_metrics"][request_class][
                    "fallback_count"
                ],
            ),
        )[0]
        metrics = best["request_class_metrics"][request_class]
        best_by_request_class[request_class] = {
            "policy_id": best["policy_id"],
            **metrics,
        }
    return {
        "format": "specdecode-strategy-comparison-v1",
        "dataset_fingerprint": dataset.fingerprint,
        "seed": dataset.seed,
        "tick_ms": dataset.tick_ms,
        "request_count": len(dataset.requests),
        "baseline_policy_id": baseline["policy"]["policy_id"],
        "strategies": strategies,
        "comparisons_against_baseline": comparisons,
        "conclusion": {
            "recommended_policy_id": recommended["policy_id"],
            "ranking": [item["policy_id"] for item in ranking],
            "criteria": [
                "latency budget attainment descending",
                "aggregate speedup descending",
                "fallback count ascending",
                "candidate length ascending",
                "acceptance threshold ascending",
            ],
            "recommended_budget_attainment_bps": recommended[
                "budget_attainment_bps"
            ],
            "recommended_speedup_ppm": recommended["speedup_ppm"],
            "recommended_acceptance_rate_bps": recommended[
                "acceptance_rate_bps"
            ],
            "best_by_request_class": best_by_request_class,
            "notes": (
                "Acceptance rate alone is not a ranking criterion: a narrow "
                "draft can have a high hit rate while wasting target batches "
                "or missing latency budgets."
            ),
        },
    }


def comparison_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Speculative strategy comparison",
        "",
        f"Dataset: `{report['dataset_fingerprint']}`",
        f"Baseline: `{report['baseline_policy_id']}`",
        "",
        "| policy | width | threshold bps | acceptance bps | fallbacks | budget bps | speedup | final tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy in report["strategies"]:
        lines.append(
            "| {policy_id} | {candidate_length} | {acceptance_threshold_bps} | "
            "{acceptance_rate_bps} | {fallback_count} | "
            "{budget_attainment_bps} | {speedup:.3f}x | {final_token_count} |".format(
                **strategy,
                speedup=strategy["speedup_ppm"] / 1_000_000,
            )
        )
    conclusion = report["conclusion"]
    lines.extend(
        [
            "",
            "## Best policy by request class",
            "",
            "| request class | best policy | acceptance bps | fallbacks | budget bps | speedup |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for request_class, metrics in conclusion["best_by_request_class"].items():
        lines.append(
            "| {request_class} | {policy_id} | {acceptance_rate_bps} | "
            "{fallback_count} | {budget_attainment_bps} | {speedup:.3f}x |".format(
                request_class=request_class,
                speedup=metrics["speedup_ppm"] / 1_000_000,
                **metrics,
            )
        )
    lines.extend(
        [
            "",
            f"Recommended: **{conclusion['recommended_policy_id']}**",
            "",
            "Ranking: "
            + " > ".join(f"`{policy_id}`" for policy_id in conclusion["ranking"]),
            "",
            "Request-level flips are listed in "
            "`comparisons_against_baseline` in the JSON report.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "ComparisonError",
    "compare_policies",
    "comparison_markdown",
    "grid_policies",
]
