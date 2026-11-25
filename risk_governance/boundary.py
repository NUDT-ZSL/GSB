"""Category boundary distance, flip diagnosis and hysteresis repair."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from .canonical import natural_key, sha256_json
from .errors import E_BOUNDARY_FLIP, E_BOUNDARY_STATS, make_error
from .schema import record_score


def _entity_id(record: dict[str, Any]) -> str:
    for key in ("entity_id", "customer_id", "id"):
        if record.get(key) is not None:
            return str(record[key])
    features = record.get("features", {})
    return "surrogate:" + sha256_json(features)[:16]


def _score_value(record: dict[str, Any], field_name: str = "score") -> float | None:
    value = record_score(record, field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        return None
    number = float(value)
    return number if np.isfinite(number) else None


def _ref(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_id": _entity_id(record),
        "batch_id": record.get("batch_id"),
        "cycle_id": record.get("cycle_id"),
        "line_no": record.get("_line_no"),
    }


def cost_weighted_threshold(
    records: list[dict[str, Any]], target: dict[str, Any]
) -> dict[str, Any] | None:
    weights = target.get("cost_weights", {}) or {}
    false_positive_cost = float(weights.get("false_positive", 1.0))
    false_negative_cost = float(weights.get("false_negative", 1.0))
    points = [
        (_score_value(record, target.get("score_field", "score")), record.get("label"))
        for record in records
    ]
    points = [(score, int(label)) for score, label in points if score is not None and label in (0, 1, True, False)]
    if not points:
        return None
    scores = sorted({score for score, _ in points})
    best = None
    for threshold in scores:
        cost = 0.0
        fp = fn = 0
        for score, label in points:
            predicted_positive = score >= threshold
            if predicted_positive and label == 0:
                fp += 1
                cost += false_positive_cost
            elif not predicted_positive and label == 1:
                fn += 1
                cost += false_negative_cost
        candidate = {
            "threshold": threshold,
            "cost": cost,
            "false_positive_count": fp,
            "false_negative_count": fn,
        }
        if best is None or cost < best["cost"] or (cost == best["cost"] and threshold < best["threshold"]):
            best = candidate
    return best


def batch_boundary_statistics(
    dataset: dict[str, Any], target: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    field_name = target.get("score_field", "score")
    threshold = float(target.get("threshold", 0.5))
    direction = target.get("direction", "high_risk")
    records = dataset["records"]
    points = []
    for record in records:
        score = _score_value(record, field_name)
        if score is not None:
            points.append((score, record))
    points.sort(key=lambda item: (item[0], item[1].get("_line_no", 0)))

    stats: dict[str, Any] = {
        "target": target.get("target"),
        "batch_id": dataset["batch_id"],
        "cycle_id": dataset["cycle_id"],
        "threshold": threshold,
        "point_count": len(points),
        "nearest_threshold_distance": None,
        "nearest_below": None,
        "nearest_above": None,
        "nearest_opposite_label_distance": None,
        "nearest_opposite_label_pair": None,
        "nearest_neighbor_distance_mean": None,
        "nearest_neighbor_distance_max": None,
        "adjacent_label_flip_count": 0,
    }
    errors: list[dict[str, Any]] = []

    if not points:
        errors.append(
            make_error(
                E_BOUNDARY_STATS,
                "boundary distance statistics are missing",
                target=target.get("target"),
                batch_id=dataset["batch_id"],
                cycle_id=dataset["cycle_id"],
                reason="missing_scores",
            )
        )
        return stats, errors

    nearest_threshold = min(points, key=lambda item: abs(item[0] - threshold))
    stats["nearest_threshold_distance"] = abs(nearest_threshold[0] - threshold)
    stats["nearest_threshold_point"] = {**_ref(nearest_threshold[1]), "score": nearest_threshold[0]}

    below = [item for item in points if item[0] < threshold]
    above = [item for item in points if item[0] >= threshold]
    if below:
        score, record = below[-1]
        stats["nearest_below"] = {"score": score, "distance": threshold - score, **_ref(record)}
    if above:
        score, record = above[0]
        stats["nearest_above"] = {"score": score, "distance": score - threshold, **_ref(record)}

    nearest_pair = None
    nearest_pair_distance = None
    adjacent_flips = 0
    for (left_score, left_record), (right_score, right_record) in zip(points, points[1:]):
        left_label = left_record.get("label")
        right_label = right_record.get("label")
        if left_label in (0, 1) and right_label in (0, 1) and int(left_label) != int(right_label):
            adjacent_flips += 1
            distance = right_score - left_score
            if nearest_pair_distance is None or distance < nearest_pair_distance:
                nearest_pair_distance = distance
                nearest_pair = {
                    "distance": distance,
                    "left": {**_ref(left_record), "score": left_score, "label": left_label},
                    "right": {**_ref(right_record), "score": right_score, "label": right_label},
                }

    nearest_distances = []
    scores = np.asarray([score for score, _ in points], dtype=np.float64)
    labels = [record.get("label") for _, record in points]
    for index, (score, record) in enumerate(points):
        if record.get("label") not in (0, 1):
            continue
        opposite = [
            abs(score - other_score)
            for other_index, (other_score, other_record) in enumerate(points)
            if index != other_index
            and other_record.get("label") in (0, 1)
            and int(other_record["label"]) != int(record["label"])
        ]
        if opposite:
            nearest_distances.append(min(opposite))

    stats["adjacent_label_flip_count"] = adjacent_flips
    stats["nearest_opposite_label_distance"] = nearest_pair_distance
    stats["nearest_opposite_label_pair"] = nearest_pair
    if nearest_distances:
        stats["nearest_neighbor_distance_mean"] = float(np.mean(nearest_distances))
        stats["nearest_neighbor_distance_max"] = float(np.max(nearest_distances))

    require_pair = target.get("require_opposite_label_pair", True)
    if require_pair and (
        nearest_pair_distance is None or stats["nearest_neighbor_distance_mean"] is None
    ):
        errors.append(
            make_error(
                E_BOUNDARY_STATS,
                "nearest-neighbor boundary distance statistic is missing",
                target=target.get("target"),
                batch_id=dataset["batch_id"],
                cycle_id=dataset["cycle_id"],
                reason="missing_opposite_label_pair",
            )
        )
    return stats, errors


def _decision(score: float, threshold: float, direction: str) -> int:
    if direction == "low_risk":
        return int(score < threshold)
    return int(score >= threshold)


def diagnose_target(
    datasets: list[dict[str, Any]], target: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    batch_stats = []
    errors: list[dict[str, Any]] = []
    all_records = []
    for dataset in datasets:
        stats, batch_errors = batch_boundary_statistics(dataset, target)
        batch_stats.append(stats)
        errors.extend(batch_errors)
        all_records.extend(dataset["records"])

    threshold = float(target.get("threshold", 0.5))
    direction = target.get("direction", "high_risk")
    field_name = target.get("score_field", "score")
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in all_records:
        score = _score_value(record, field_name)
        if score is None:
            continue
        entity_id = _entity_id(record)
        histories[entity_id].append(
            {
                "entity_id": entity_id,
                "batch_id": record.get("batch_id"),
                "cycle_id": record.get("cycle_id"),
                "line_no": record.get("_line_no"),
                "score": score,
                "decision": _decision(score, threshold, direction),
                "label": record.get("label"),
            }
        )

    flip_events = []
    trajectory_count = 0
    for entity_id, observations in histories.items():
        observations.sort(
            key=lambda item: (
                natural_key(str(item["cycle_id"])),
                natural_key(str(item["batch_id"])),
                item["line_no"] or 0,
            )
        )
        if len(observations) < 2:
            continue
        trajectory_count += 1
        for previous, current in zip(observations, observations[1:]):
            if previous["decision"] != current["decision"]:
                flip_events.append(
                    {
                        "entity_id": entity_id,
                        "from_batch_id": previous["batch_id"],
                        "from_cycle_id": previous["cycle_id"],
                        "to_batch_id": current["batch_id"],
                        "to_cycle_id": current["cycle_id"],
                        "from_score": previous["score"],
                        "to_score": current["score"],
                        "from_decision": previous["decision"],
                        "to_decision": current["decision"],
                        "threshold": threshold,
                    }
                )

    margin = float(target.get("hysteresis_margin", target.get("repair_margin", 0.03)))
    repaired_flip_events = []
    repaired_decisions = {}
    for entity_id, observations in histories.items():
        observations.sort(
            key=lambda item: (
                natural_key(str(item["cycle_id"])),
                natural_key(str(item["batch_id"])),
                item["line_no"] or 0,
            )
        )
        previous_repaired = None
        for observation in observations:
            score = observation["score"]
            if score <= threshold - margin:
                repaired = 0 if direction != "low_risk" else 1
            elif score >= threshold + margin:
                repaired = 1 if direction != "low_risk" else 0
            else:
                repaired = observation["decision"] if previous_repaired is None else previous_repaired
            if previous_repaired is not None and repaired != previous_repaired:
                repaired_flip_events.append(
                    {
                        "entity_id": entity_id,
                        "batch_id": observation["batch_id"],
                        "cycle_id": observation["cycle_id"],
                        "score": score,
                        "decision": repaired,
                    }
                )
            repaired_decisions[(entity_id, observation["batch_id"], observation["line_no"])] = repaired
            previous_repaired = repaired

    max_flips = int(target.get("max_flips", 0))
    flip_count = len(flip_events)
    repair = {
        "strategy": "hysteresis_band",
        "margin": margin,
        "lower_threshold": threshold - margin,
        "upper_threshold": threshold + margin,
        "original_flip_count": flip_count,
        "residual_flip_count": len(repaired_flip_events),
        "repaired": len(repaired_flip_events) <= max_flips,
        "residual_flip_events": repaired_flip_events,
    }
    cost_threshold = cost_weighted_threshold(all_records, target)
    diagnosis = {
        "target": target.get("target"),
        "threshold": threshold,
        "direction": direction,
        "max_flips": max_flips,
        "trajectory_count": trajectory_count,
        "flip_count": flip_count,
        "flip_events": flip_events,
        "batch_statistics": batch_stats,
        "repair": repair,
        "cost_weighted_threshold_candidate": cost_threshold,
        "fingerprint": sha256_json(
            {
                "target": target.get("target"),
                "threshold": threshold,
                "flip_count": flip_count,
                "flip_events": flip_events,
                "batch_statistics": batch_stats,
            }
        ),
    }
    if flip_count > max_flips:
        errors.append(
            make_error(
                E_BOUNDARY_FLIP,
                "category boundary flipped more times than configured threshold",
                target=target.get("target"),
                flip_count=flip_count,
                max_flips=max_flips,
                replay_evidence=flip_events,
                repair=repair,
            )
        )
    return diagnosis, errors


def diagnose_boundaries(
    datasets: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    targets = config.get("boundary_targets") or config.get("category_targets")
    if not targets:
        thresholds = set()
        bands = config.get("threshold_bands") or config.get("decision_bands") or []
        for band in bands:
            for key in ("lower", "upper", "threshold"):
                if band.get(key) is not None:
                    thresholds.add(float(band[key]))
        targets = [
            {
                "target": f"threshold_{threshold:g}",
                "threshold": threshold,
                "max_flips": int(config.get("max_boundary_flips", 0)),
                "hysteresis_margin": float(config.get("hysteresis_margin", 0.03)),
                "cost_weights": config.get("cost_weights", {}),
            }
            for threshold in sorted(thresholds)
        ]

    diagnoses = []
    errors: list[dict[str, Any]] = []
    for target in targets:
        diagnosis, target_errors = diagnose_target(datasets, target)
        diagnoses.append(diagnosis)
        errors.extend(target_errors)
    return diagnoses, errors
