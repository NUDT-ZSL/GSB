"""Stage 3: explicit weighted length/repetition/noise quality scoring."""

from __future__ import annotations

from typing import Any

from ..scheduler import run_worker_batches, select_worker_count
from ..types import BudgetLimits
from ..utils import normalize_text


def _length_component(char_count: int, rule: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    minimum = int(rule["min_chars"])
    ideal_min = int(rule["ideal_min"])
    ideal_max = int(rule["ideal_max"])
    maximum = int(rule["max_chars"])
    if char_count < minimum or char_count > maximum:
        score = 0.0
    elif ideal_min <= char_count <= ideal_max:
        score = 1.0
    elif char_count < ideal_min:
        score = (char_count - minimum) / max(1, ideal_min - minimum)
    else:
        score = (maximum - char_count) / max(1, maximum - ideal_max)
    score = round(max(0.0, min(1.0, score)), 8)
    return score, {
        "char_count": char_count,
        "min_chars": minimum,
        "ideal_min": ideal_min,
        "ideal_max": ideal_max,
        "max_chars": maximum,
    }


def _repetition_component(
    text: str,
    rule: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    ngram_size = int(rule["ngram_size"])
    compact = "".join(text.casefold().split())
    if len(compact) < ngram_size:
        count = 0
        unique = 0
        rate = 0.0
    else:
        ngrams = [
            compact[index : index + ngram_size]
            for index in range(len(compact) - ngram_size + 1)
        ]
        count = len(ngrams)
        unique = len(set(ngrams))
        rate = 1.0 - (unique / count)
    zero_at = float(rule["zero_at_rate"])
    score = 0.0 if zero_at <= 0 else 1.0 - (rate / zero_at)
    score = round(max(0.0, min(1.0, score)), 8)
    return score, {
        "ngram_size": ngram_size,
        "ngram_count": count,
        "unique_ngram_count": unique,
        "repetition_rate": round(rate, 8),
        "zero_at_rate": zero_at,
    }


def _noise_component(
    text: str,
    rule: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    noise_chars = set(rule.get("characters", ""))
    count = sum(1 for char in text if char in noise_chars)
    ratio = count / max(1, len(text))
    zero_at = float(rule["zero_at_rate"])
    score = 0.0 if zero_at <= 0 else 1.0 - (ratio / zero_at)
    score = round(max(0.0, min(1.0, score)), 8)
    return score, {
        "noise_count": count,
        "char_count": len(text),
        "noise_rate": round(ratio, 8),
        "zero_at_rate": zero_at,
    }


def _tier_for(score: float, tiers: dict[str, Any]) -> str:
    ordered = sorted(
        ((name, float(threshold)) for name, threshold in tiers.items()),
        key=lambda pair: (-pair[1], pair[0]),
    )
    for name, threshold in ordered:
        if score >= threshold:
            return name
    return "ungraded"


def _score_sample(
    sample: dict[str, Any],
    quality_rules: dict[str, Any],
) -> dict[str, Any]:
    full_text = normalize_text(
        " ".join(
            [
                sample["canonical_text"],
                sample["output"],
            ]
        )
    )
    weights = quality_rules["weights"]
    length_score, length_metrics = _length_component(
        len(full_text),
        quality_rules["length"],
    )
    repetition_score, repetition_metrics = _repetition_component(
        full_text,
        quality_rules["repetition"],
    )
    noise_score, noise_metrics = _noise_component(
        full_text,
        quality_rules["noise"],
    )
    component_scores = {
        "length": length_score,
        "repetition": repetition_score,
        "noise": noise_score,
    }
    component_metrics = {
        "length": length_metrics,
        "repetition": repetition_metrics,
        "noise": noise_metrics,
    }
    contributions: dict[str, Any] = {}
    total = 0.0
    for name in ("length", "repetition", "noise"):
        weight = float(weights[name])
        contribution = round(weight * component_scores[name], 8)
        total += contribution
        contributions[name] = {
            "weight": weight,
            "component_score": component_scores[name],
            "contribution": contribution,
            "metrics": component_metrics[name],
        }
    total = round(total, 6)
    scored = dict(sample)
    scored.update(
        {
            "quality_score": total,
            "quality_tier": _tier_for(total, quality_rules["tiers"]),
            "quality_contributions": contributions,
            "quality_input_chars": len(full_text),
        }
    )
    return scored


def run_quality(
    samples: list[dict[str, Any]],
    quality_rules: dict[str, Any],
    config: dict[str, Any],
    *,
    failpoints: dict[str, str] | None = None,
) -> dict[str, Any]:
    runtime = config["runtime"]
    limits = BudgetLimits(
        max_workers=int(runtime["max_workers"]),
        compare_budget=int(runtime["compare_budget"]),
        memory_limit_bytes=int(runtime["memory_limit_bytes"]),
        error_priority=tuple(runtime["budget_error_priority"]),
    )
    workers, worker_decisions = select_worker_count(
        requested_workers=int(runtime["requested_workers"]),
        limits=limits,
        base_memory_bytes=len(samples) * int(runtime["per_sample_memory_bytes"]),
        per_worker_memory_bytes=int(runtime["per_worker_memory_bytes"]),
        stage="quality",
    )

    def worker(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [_score_sample(sample, quality_rules) for sample in batch]

    scored = run_worker_batches(
        samples,
        worker,
        stage="quality",
        batch_size=int(runtime["batch_size"]),
        workers=workers,
        failpoints=failpoints,
    )
    scored.sort(key=lambda item: item["sample_id"])
    tiers: dict[str, int] = {}
    for sample in scored:
        tiers[sample["quality_tier"]] = tiers.get(sample["quality_tier"], 0) + 1
    return {
        "stage": "quality",
        "samples": scored,
        "summary": {
            "input_samples": len(samples),
            "output_samples": len(scored),
            "tier_counts": dict(sorted(tiers.items())),
            "rules": quality_rules,
            "tie_break": "quality_score desc, sample_id asc",
        },
        "parallel": {
            "requested_workers": int(runtime["requested_workers"]),
            "workers": workers,
            "batch_size": int(runtime["batch_size"]),
            "worker_decisions": worker_decisions,
        },
        "completed_sample_ids": sorted(
            sample["sample_id"] for sample in samples
        ),
        "valid_sample_ids": [sample["sample_id"] for sample in scored],
        "expected_sample_ids": sorted(
            sample["sample_id"] for sample in samples
        ),
    }

