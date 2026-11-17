from __future__ import annotations

import math
from typing import Any

from .config import semantic_config_hash
from .dependencies import build_summary_references
from .features import tokenize_text
from .io_utils import sha256_obj, sorted_failures


def _quantile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def reward_delta_distribution(scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values = [scores[pair_id]["reward_delta"] for pair_id in sorted(scores)]
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p10": None,
            "p90": None,
            "stddev": None,
        }
    sorted_values = sorted(float(value) for value in values)
    mean = math.fsum(sorted_values) / len(sorted_values)
    variance = math.fsum((value - mean) ** 2 for value in sorted_values) / len(
        sorted_values
    )
    return {
        "count": len(sorted_values),
        "min": sorted_values[0],
        "max": sorted_values[-1],
        "mean": mean,
        "median": _quantile(sorted_values, 0.5),
        "p10": _quantile(sorted_values, 0.1),
        "p90": _quantile(sorted_values, 0.9),
        "stddev": math.sqrt(variance),
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


def annotator_groups(
    valid: dict[str, dict[str, Any]],
    features: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for pair_id in sorted(valid):
        if pair_id not in features:
            continue
        feature = features[pair_id]
        chosen_tokens = set(
            tokenize_text(feature["normalized"]["chosen"], config)
        )
        rejected_tokens = set(
            tokenize_text(feature["normalized"]["rejected"], config)
        )
        similarity = _jaccard(chosen_tokens, rejected_tokens)
        annotator = valid[pair_id]["annotator_id"]
        grouped.setdefault(annotator, []).append(
            {"pair_id": pair_id, "chosen_rejected_jaccard": similarity}
        )

    groups: dict[str, Any] = {}
    for annotator in sorted(grouped):
        members = grouped[annotator]
        mean = math.fsum(item["chosen_rejected_jaccard"] for item in members) / len(
            members
        )
        groups[annotator] = {
            "pair_count": len(members),
            "mean_chosen_rejected_jaccard": mean,
            "members": members,
        }

    nonempty = [
        (annotator, data["mean_chosen_rejected_jaccard"])
        for annotator, data in groups.items()
        if data["pair_count"] > 0
    ]
    extrema = None
    if nonempty:
        minimum = min(nonempty, key=lambda item: (item[1], item[0]))
        maximum = max(nonempty, key=lambda item: (item[1], item[0]))
        extrema = {
            "min_group": minimum[0],
            "min_value": minimum[1],
            "max_group": maximum[0],
            "max_value": maximum[1],
        }
    return {"groups": groups, "extrema": extrema}


def bucket_frequency(features: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts: dict[int, int] = {}
    for pair_id in sorted(features):
        for field in ("prompt", "chosen", "rejected"):
            for index, value in enumerate(features[pair_id]["vectors"][field]):
                if value:
                    counts[index] = counts.get(index, 0) + int(value)
    return {str(index): counts[index] for index in sorted(counts)}


def compute_summary(
    loaded: dict[str, Any],
    features: dict[str, dict[str, Any]],
    scores: dict[str, dict[str, Any]],
    load_failures: list[dict[str, Any]],
    feature_failures: list[dict[str, Any]],
    score_failures: list[dict[str, Any]],
    config: dict[str, Any],
    batch_ids: list[str],
    data_fingerprint: str,
    dependencies: dict[str, Any],
) -> dict[str, Any]:
    failures = sorted_failures(load_failures + feature_failures + score_failures)
    boundary_samples = [
        {
            "pair_id": pair_id,
            "reward_delta": scores[pair_id]["reward_delta"],
            "reference_delta_double": scores[pair_id]["reference_delta_double"],
        }
        for pair_id in sorted(scores)
        if scores[pair_id]["is_boundary"]
    ]
    clear_pairs = [
        pair_id for pair_id in sorted(scores) if not scores[pair_id]["is_boundary"]
    ]
    threshold_relations = {"below": 0, "equal": 0, "above": 0}
    for pair_id in scores:
        threshold_relations[scores[pair_id]["threshold_relation"]] += 1
    active_buckets = sorted(
        {
            int(bucket)
            for feature in features.values()
            for bucket in feature["referenced_buckets"]
        }
    )
    warning_pair_ids = [
        pair_id
        for pair_id in sorted(features)
        if features[pair_id].get("empty_subword_sets")
    ]
    summary = {
        "format_version": 1,
        "config_hash": semantic_config_hash(config),
        "data_fingerprint": data_fingerprint,
        "valid_pair_count": len(loaded["valid"]),
        "feature_pair_count": len(features),
        "scored_pair_count": len(scores),
        "unscored_pair_ids": sorted(set(loaded["valid"]).difference(scores)),
        "failure_count": len(failures),
        "failures": failures,
        "boundary_count": len(boundary_samples),
        "clear_count": len(clear_pairs),
        "boundary_pair_ids": [item["pair_id"] for item in boundary_samples],
        "clear_pair_ids": clear_pairs,
        "boundary_samples": boundary_samples,
        "threshold_relation_counts": threshold_relations,
        "reward_delta_distribution": reward_delta_distribution(scores),
        "bucket_frequency": bucket_frequency(features),
        "bucket_references": {
            str(index): [
                pair_id
                for pair_id in sorted(features)
                if index in features[pair_id]["referenced_buckets"]
            ]
            for index in active_buckets
        },
        "annotator_groups": annotator_groups(loaded["valid"], features, config),
        "warning_pair_ids": warning_pair_ids,
        "dependency_refs": build_summary_references(dependencies, batch_ids, active_buckets),
    }
    summary["summary_fingerprint"] = sha256_obj(summary)
    return summary
