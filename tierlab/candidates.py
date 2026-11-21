from __future__ import annotations

import math
import random
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Iterable

import numpy as np

from .models import (
    COLD,
    HOT,
    Candidate,
    Evaluation,
    ModeDecision,
    Observation,
    StorageObject,
    TierConfig,
    stable_digest,
)
from .scoring import heat_score, target_layer


def evaluate_object(
    obj: StorageObject,
    logical_clock: int,
    config: TierConfig,
) -> Evaluation:
    score = heat_score(obj, logical_clock)
    target = target_layer(score, config)
    exclusion: str | None = None
    if not obj.migratable:
        exclusion = "not_migratable"
    elif obj.migration_state == "migrating":
        exclusion = "in_flight"
    elif (
        obj.last_outcome == "abandoned_retry_limit"
        or (
            obj.migration_state == "abandoned"
            and obj.failure_count >= config.backoff.max_consecutive_failures
        )
    ):
        exclusion = "retry_limit"
    elif obj.backoff_until_clock > logical_clock:
        exclusion = "backoff"
    elif target == obj.current_layer:
        exclusion = "already_at_target"
    direction = None
    if target != obj.current_layer:
        direction = f"{obj.current_layer}_to_{target}"
    eligible = exclusion is None
    return Evaluation(
        obj=obj,
        score=score,
        target_layer=target,
        eligible=eligible,
        exclusion_reason=exclusion,
        direction=direction,
    )


def _to_candidate(evaluation: Evaluation) -> Candidate:
    assert evaluation.eligible
    return Candidate(
        obj=evaluation.obj,
        score=evaluation.score,
        source_layer=evaluation.obj.current_layer,
        target_layer=evaluation.target_layer,
        direction=evaluation.direction or f"{evaluation.obj.current_layer}_to_unknown",
    )


def candidate_sort_key(candidate: Candidate, threshold: Decimal) -> tuple[Any, ...]:
    urgency = abs(candidate.score - threshold)
    return (-urgency, candidate.obj.key)


def sort_candidates(candidates: Iterable[Candidate], threshold: Decimal) -> list[Candidate]:
    return sorted(candidates, key=lambda item: candidate_sort_key(item, threshold))


def _observation_item(evaluation: Evaluation) -> dict[str, Any]:
    obj = evaluation.obj
    return {
        "key": obj.key,
        "size_bytes": obj.size_bytes,
        "last_access_clock": obj.last_access_clock,
        "access_count": obj.access_count,
        "heat_weight": str(obj.heat_weight),
        "current_layer": obj.current_layer,
        "declared_layer": obj.declared_layer,
        "migratable": obj.migratable,
        "score": str(evaluation.score),
        "target_layer": evaluation.target_layer,
        "eligible_candidate": evaluation.eligible,
        "exclusion_reason": evaluation.exclusion_reason,
    }


def exact_scan(
    objects: list[StorageObject],
    logical_clock: int,
    config: TierConfig,
) -> Observation:
    ordered = sorted(objects, key=lambda obj: obj.key)
    evaluations: list[Evaluation] = []
    excluded: dict[str, int] = {}
    for obj in ordered:
        evaluation = evaluate_object(obj, logical_clock, config)
        evaluations.append(evaluation)
        if evaluation.exclusion_reason:
            excluded[evaluation.exclusion_reason] = (
                excluded.get(evaluation.exclusion_reason, 0) + 1
            )
    candidates = [
        _to_candidate(item) for item in evaluations if item.eligible
    ]
    return Observation(
        mode="exact",
        truth_source=(
            "exact full scan: every persisted object was evaluated against the "
            "injected logical clock and fixed heat formula"
        ),
        population_count=len(ordered),
        observed_count=len(ordered),
        candidate_count=len(candidates),
        candidate_bytes=sum(item.obj.size_bytes for item in candidates),
        candidates=candidates,
        observations=[_observation_item(item) for item in evaluations],
        excluded=dict(sorted(excluded.items())),
    )


def _round_nonnegative(value: float) -> int:
    if not math.isfinite(value):
        return 0
    with localcontext():
        decimal_value = Decimal(str(max(0.0, value)))
        return int(decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _confidence_margin(
    values: np.ndarray,
    population: int,
    sample_size: int,
    z_value: float,
    fallback: int,
) -> int:
    if sample_size >= population:
        return 0
    if sample_size < 2:
        return fallback
    variance = float(np.var(values, ddof=1))
    finite_population = 1.0 - (sample_size / population)
    standard_error_total = population * math.sqrt(
        max(0.0, variance / sample_size) * max(0.0, finite_population)
    )
    raw_margin = z_value * standard_error_total
    if not math.isfinite(raw_margin):
        return fallback
    return min(fallback, max(0, math.ceil(raw_margin)))


def stratified_sample(
    objects: list[StorageObject],
    logical_clock: int,
    config: TierConfig,
    *,
    bucket: str,
    round_number: int,
) -> Observation:
    population = len(objects)
    seed_material = {
        "bucket": bucket,
        "round": round_number,
        "clock": logical_clock,
        "ratio": str(config.sampling.ratio),
        "random_seed": config.sampling.random_seed,
    }
    seed = (
        int(stable_digest(seed_material)[:16], 16)
        ^ config.sampling.random_seed
    ) & ((1 << 63) - 1)
    rng = random.Random(seed)
    z_value = float(config.sampling.confidence_z)

    strata: list[dict[str, Any]] = []
    sampled_evaluations: list[Evaluation] = []
    sampled_candidates: list[Candidate] = []
    sample_keys: list[str] = []

    for layer in (HOT, COLD):
        stratum_objects = sorted(
            (obj for obj in objects if obj.current_layer == layer),
            key=lambda obj: obj.key,
        )
        stratum_population = len(stratum_objects)
        if stratum_population == 0:
            continue
        requested = math.ceil(
            float(config.sampling.ratio) * stratum_population
        )
        sample_size = max(config.sampling.min_sample_per_stratum, requested)
        sample_size = min(stratum_population, sample_size)
        if sample_size >= stratum_population:
            indexes = list(range(stratum_population))
        else:
            indexes = sorted(rng.sample(range(stratum_population), sample_size))
        selected = [stratum_objects[index] for index in indexes]

        evaluations = [
            evaluate_object(obj, logical_clock, config) for obj in selected
        ]
        sampled_evaluations.extend(evaluations)
        local_candidates = [item for item in evaluations if item.eligible]
        sampled_candidates.extend(_to_candidate(item) for item in local_candidates)
        sample_keys.extend(obj.key for obj in selected)

        indicator = np.asarray(
            [1.0 if item.eligible else 0.0 for item in evaluations],
            dtype=np.float64,
        )
        candidate_bytes = np.asarray(
            [float(item.obj.size_bytes) if item.eligible else 0.0 for item in evaluations],
            dtype=np.float64,
        )
        max_size = max(obj.size_bytes for obj in stratum_objects)
        if sample_size >= stratum_population:
            estimated_count = int(indicator.sum())
            estimated_bytes = int(candidate_bytes.sum())
            count_margin = 0
            bytes_margin = 0
            worst_count = 0
            worst_bytes = 0
        else:
            estimated_count = _round_nonnegative(
                stratum_population * float(indicator.mean())
            )
            estimated_bytes = _round_nonnegative(
                stratum_population * float(candidate_bytes.mean())
            )
            count_margin = _confidence_margin(
                indicator,
                stratum_population,
                sample_size,
                z_value,
                fallback=stratum_population,
            )
            bytes_margin = _confidence_margin(
                candidate_bytes,
                stratum_population,
                sample_size,
                z_value,
                fallback=stratum_population * max_size,
            )
            unobserved = stratum_population - sample_size
            observed_count = int(indicator.sum())
            observed_bytes = int(candidate_bytes.sum())
            worst_count = max(
                abs(estimated_count - observed_count),
                abs(estimated_count - (observed_count + unobserved)),
            )
            worst_bytes = max(
                abs(estimated_bytes - observed_bytes),
                abs(estimated_bytes - (observed_bytes + unobserved * max_size)),
            )
        strata.append(
            {
                "stratum": f"current_layer={layer}",
                "population_count": stratum_population,
                "sample_count": sample_size,
                "sample_coverage": (
                    "1" if stratum_population == 0 else str(
                        Decimal(sample_size) / Decimal(stratum_population)
                    )
                ),
                "sample_candidate_count": len(local_candidates),
                "sample_candidate_bytes": sum(
                    item.obj.size_bytes for item in local_candidates
                ),
                "estimated_candidate_count": estimated_count,
                "candidate_count_error_bound": count_margin,
                "estimated_candidate_bytes": estimated_bytes,
                "candidate_bytes_error_bound": bytes_margin,
                "confidence_count_interval": [
                    max(0, estimated_count - count_margin),
                    min(stratum_population, estimated_count + count_margin),
                ],
                "worst_case_count_error_bound": worst_count,
                "worst_case_bytes_error_bound": worst_bytes,
            }
        )

    estimated_count = sum(
        int(item["estimated_candidate_count"]) for item in strata
    )
    estimated_bytes = sum(
        int(item["estimated_candidate_bytes"]) for item in strata
    )
    count_margin = sum(
        int(item["candidate_count_error_bound"]) for item in strata
    )
    bytes_margin = sum(
        int(item["candidate_bytes_error_bound"]) for item in strata
    )
    observed_count = len(sampled_candidates)
    observed_bytes = sum(item.obj.size_bytes for item in sampled_candidates)
    worst_count = sum(
        int(item["worst_case_count_error_bound"]) for item in strata
    )
    worst_bytes = sum(
        int(item["worst_case_bytes_error_bound"]) for item in strata
    )
    excluded: dict[str, int] = {}
    for evaluation in sampled_evaluations:
        if evaluation.exclusion_reason:
            excluded[evaluation.exclusion_reason] = (
                excluded.get(evaluation.exclusion_reason, 0) + 1
            )

    summary = {
        "method": "deterministic stratified sampling by current_layer",
        "seed": seed,
        "ratio": str(config.sampling.ratio),
        "confidence_z": str(config.sampling.confidence_z),
        "population_count": population,
        "sampled_count": len(sampled_evaluations),
        "sample_keys": sorted(sample_keys),
        "sample_candidate_count": observed_count,
        "sample_candidate_bytes": observed_bytes,
        "estimated_candidate_count": estimated_count,
        "candidate_count_error_bound": count_margin,
        "confidence_count_interval": [
            max(0, estimated_count - count_margin),
            min(population, estimated_count + count_margin),
        ],
        "worst_case_candidate_count_error_bound": worst_count,
        "estimated_candidate_bytes": estimated_bytes,
        "candidate_bytes_error_bound": bytes_margin,
        "confidence_bytes_interval": [
            max(0, estimated_bytes - bytes_margin),
            estimated_bytes + bytes_margin,
        ],
        "worst_case_candidate_bytes_error_bound": worst_bytes,
        "error_ratio": str(
            _error_ratio(
                estimated_count,
                count_margin,
                estimated_bytes,
                bytes_margin,
            )
        ),
        "strata": strata,
    }
    return Observation(
        mode="sampled",
        truth_source=(
            "deterministic stratified sample of persisted objects; candidate "
            "counts and bytes are extrapolated per current-layer stratum"
        ),
        population_count=population,
        observed_count=len(sampled_evaluations),
        candidate_count=observed_count,
        candidate_bytes=observed_bytes,
        candidates=sampled_candidates,
        observations=[
            _observation_item(item)
            for item in sorted(sampled_evaluations, key=lambda item: item.obj.key)
        ],
        sampling_summary=summary,
        excluded=dict(sorted(excluded.items())),
    )


def _error_ratio(
    estimated_count: int,
    count_margin: int,
    estimated_bytes: int,
    bytes_margin: int,
) -> Decimal:
    count_ratio = Decimal(count_margin) / Decimal(max(1, estimated_count))
    bytes_ratio = Decimal(bytes_margin) / Decimal(max(1, estimated_bytes))
    return max(count_ratio, bytes_ratio).quantize(Decimal("0.000000000001"))


def choose_observation(
    objects: list[StorageObject],
    logical_clock: int,
    config: TierConfig,
    *,
    bucket: str,
    round_number: int,
    requested_mode: str | None,
) -> tuple[Observation, ModeDecision]:
    requested = requested_mode or config.mode
    population = len(objects)
    pilot = stratified_sample(
        objects,
        logical_clock,
        config,
        bucket=bucket,
        round_number=round_number,
    )
    assert pilot.sampling_summary is not None
    summary = pilot.sampling_summary
    estimated_count = int(summary["estimated_candidate_count"])
    estimated_bytes = int(summary["estimated_candidate_bytes"])
    count_margin = int(summary["candidate_count_error_bound"])
    bytes_margin = int(summary["candidate_bytes_error_bound"])
    error_ratio = _error_ratio(
        estimated_count,
        count_margin,
        estimated_bytes,
        bytes_margin,
    )

    adaptive = config.adaptive
    exact_cost = (
        Decimal(population) * adaptive.exact_scan_unit_cost
        + Decimal(estimated_count) * adaptive.exact_candidate_unit_cost
    )
    sampled_cost = (
        Decimal(population) * adaptive.sampled_index_unit_cost
        + Decimal(pilot.observed_count) * adaptive.sampled_object_unit_cost
    )
    cost_estimate = {
        "exact": str(exact_cost.quantize(Decimal("0.000001"))),
        "sampled": str(sampled_cost.quantize(Decimal("0.000001"))),
    }

    if requested == "exact":
        observation = exact_scan(objects, logical_clock, config)
        decision = ModeDecision(
            requested_mode=requested,
            selected_mode="exact",
            trigger_conditions=["requested_mode=exact"],
            rejected_mode="sampled",
            rejected_reason=(
                "sampled mode was rejected because exact mode was explicitly requested"
            ),
            cost_estimate=cost_estimate,
            metrics={
                "object_count": population,
                "sampled_population_estimate": estimated_count,
                "pilot_sampling": summary,
            },
        )
        return observation, decision

    if requested == "sampled":
        decision = ModeDecision(
            requested_mode=requested,
            selected_mode="sampled",
            trigger_conditions=["requested_mode=sampled"],
            rejected_mode="exact",
            rejected_reason=(
                "exact mode was rejected because sampled mode was explicitly requested"
            ),
            cost_estimate=cost_estimate,
            metrics={
                "object_count": population,
                "estimated_candidate_count": estimated_count,
                "estimated_candidate_bytes": estimated_bytes,
                "estimated_error_ratio": str(error_ratio),
                "pilot_sampling": summary,
            },
        )
        return pilot, decision

    triggers: list[str] = []
    if population > adaptive.exact_max_objects:
        triggers.append(
            f"object_count={population} > exact_max_objects="
            f"{adaptive.exact_max_objects}"
        )
    if estimated_count > adaptive.exact_max_candidates:
        triggers.append(
            f"estimated_candidate_count={estimated_count} > "
            f"exact_max_candidates={adaptive.exact_max_candidates}"
        )

    if not triggers:
        observation = exact_scan(objects, logical_clock, config)
        decision = ModeDecision(
            requested_mode="auto",
            selected_mode="exact",
            trigger_conditions=[
                f"object_count={population} <= exact_max_objects="
                f"{adaptive.exact_max_objects}",
                f"estimated_candidate_count={estimated_count} <= "
                f"exact_max_candidates={adaptive.exact_max_candidates}",
            ],
            rejected_mode="sampled",
            rejected_reason=(
                "sampled mode was rejected: workload is within exact thresholds"
            ),
            cost_estimate=cost_estimate,
            metrics={
                "object_count": population,
                "estimated_candidate_count": estimated_count,
                "exact_candidate_count": observation.candidate_count,
                "estimated_error_ratio": str(error_ratio),
                "pilot_sampling": summary,
            },
        )
        return observation, decision

    if pilot.observed_count >= population:
        observation = exact_scan(objects, logical_clock, config)
        decision = ModeDecision(
            requested_mode="auto",
            selected_mode="exact",
            trigger_conditions=triggers,
            rejected_mode="sampled",
            rejected_reason=(
                "sampled mode was rejected because the configured sample was a "
                "full census; equivalent exact results cost the same"
            ),
            cost_estimate=cost_estimate,
            metrics={
                "object_count": population,
                "estimated_candidate_count": estimated_count,
                "exact_candidate_count": observation.candidate_count,
                "estimated_error_ratio": str(error_ratio),
                "pilot_sampling": summary,
            },
        )
        return observation, decision

    if error_ratio > adaptive.max_estimated_error_ratio:
        observation = exact_scan(objects, logical_clock, config)
        decision = ModeDecision(
            requested_mode="auto",
            selected_mode="exact",
            trigger_conditions=triggers,
            rejected_mode="sampled",
            rejected_reason=(
                f"sampled mode was rejected: estimated_error_ratio="
                f"{error_ratio} exceeds max_estimated_error_ratio="
                f"{adaptive.max_estimated_error_ratio}"
            ),
            cost_estimate=cost_estimate,
            metrics={
                "object_count": population,
                "estimated_candidate_count": estimated_count,
                "exact_candidate_count": observation.candidate_count,
                "estimated_error_ratio": str(error_ratio),
                "pilot_sampling": summary,
            },
        )
        return observation, decision

    if sampled_cost >= exact_cost:
        observation = exact_scan(objects, logical_clock, config)
        decision = ModeDecision(
            requested_mode="auto",
            selected_mode="exact",
            trigger_conditions=triggers,
            rejected_mode="sampled",
            rejected_reason=(
                f"sampled estimated cost {cost_estimate['sampled']} is not lower "
                f"than exact estimated cost {cost_estimate['exact']}"
            ),
            cost_estimate=cost_estimate,
            metrics={
                "object_count": population,
                "estimated_candidate_count": estimated_count,
                "exact_candidate_count": observation.candidate_count,
                "estimated_error_ratio": str(error_ratio),
                "pilot_sampling": summary,
            },
        )
        return observation, decision

    decision = ModeDecision(
        requested_mode="auto",
        selected_mode="sampled",
        trigger_conditions=triggers
        + [
            f"estimated_error_ratio={error_ratio} <= "
            f"max_estimated_error_ratio={adaptive.max_estimated_error_ratio}",
            f"sampled_cost={cost_estimate['sampled']} < "
            f"exact_cost={cost_estimate['exact']}",
        ],
        rejected_mode="exact",
        rejected_reason=(
            "exact mode was rejected: scale thresholds were exceeded and the "
            "sampling error bound was acceptable"
        ),
        cost_estimate=cost_estimate,
        metrics={
            "object_count": population,
            "estimated_candidate_count": estimated_count,
            "estimated_candidate_bytes": estimated_bytes,
            "estimated_error_ratio": str(error_ratio),
            "pilot_sampling": summary,
        },
    )
    return pilot, decision
