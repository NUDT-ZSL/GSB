"""Session-level aggregation with explicit time decay and independent checks."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import fail
from .risk import merge_risk_candidates, message_risk_intensity
from .util import quantize


DECAY_FORMULA = (
    "effective_weight = message_weight * 2 ** "
    "(-(max_msg_no - msg_no) / half_life_steps)"
)
SENTIMENT_AGGREGATION_FORMULA = (
    "class_weighted_mean = sum(effective_weight * message_class_score) / "
    "sum(effective_weight); sentiment_score = positive_mean - negative_mean"
)
RISK_AGGREGATION_FORMULA = (
    "message_risk_intensity = min(risk_score_cap, "
    "sum(max_severity_per_category * risk_category_weight)); "
    "risk_score = sum(effective_weight * message_risk_intensity) / "
    "sum(effective_weight)"
)
CANDIDATE_MERGE_RULE = (
    "same category candidates merge iff both adjacent messages contain a hit "
    "and next.msg_no == previous.msg_no + 1; empty messages or gaps break runs"
)


def _winner(scores: dict[str, float], order: list[str]) -> str:
    winner = order[0]
    for candidate in order[1:]:
        if scores[candidate] > scores[winner]:
            winner = candidate
    return winner


def _risk_level(score: float, config: dict[str, Any]) -> str:
    levels = config["aggregation"]["risk_levels"]
    for level in levels:
        if score >= float(level["min_score"]):
            return level["label"]
    return levels[-1]["label"]


def apply_decay(messages: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    decimals = int(config["precision"]["decimals"])
    decay_config = config["aggregation"]["decay"]
    max_msg_no = max(int(message["msg_no"]) for message in messages)
    half_life = float(decay_config["half_life_steps"])
    factors: dict[int, float] = {}
    effective_weights: list[float] = []
    for message in sorted(messages, key=lambda item: int(item["msg_no"])):
        msg_no = int(message["msg_no"])
        distance = max_msg_no - msg_no
        factor = (
            2.0 ** (-distance / half_life)
            if decay_config["enabled"]
            else 1.0
        )
        factor = quantize(factor, decimals + 3)
        effective = quantize(float(message["weight"]) * factor, decimals + 3)
        factors[msg_no] = factor
        effective_weights.append(effective)
        message["time_decay"] = factor
        message["effective_weight"] = effective
    normalizer = float(np.sum(np.asarray(effective_weights, dtype=np.float64)))
    return {
        "max_msg_no": max_msg_no,
        "factors": factors,
        "effective_weights": effective_weights,
        "normalizer": quantize(normalizer, decimals + 3),
    }


def aggregate_messages(
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    risk_words: dict[str, Any],
) -> dict[str, Any]:
    decimals = int(config["precision"]["decimals"])
    decay = apply_decay(messages, config)
    weights = np.asarray(decay["effective_weights"], dtype=np.float64)
    normalizer = float(np.sum(weights))

    label_order = config["sentiment"]["label_order"]
    class_values = np.asarray(
        [
            [
                float(message["sentiment_scores"][label])
                for label in label_order
            ]
            for message in messages
        ],
        dtype=np.float64,
    )
    class_means_array = (class_values * weights[:, None]).sum(axis=0) / normalizer
    class_means = {
        label: quantize(float(class_means_array[index]), decimals)
        for index, label in enumerate(label_order)
    }
    sentiment_label = _winner(class_means, label_order)
    sentiment_score = quantize(
        class_means["positive"] - class_means["negative"], decimals
    )

    intensities = [
        message_risk_intensity(message["risk_hits"], config) for message in messages
    ]
    for message, intensity in zip(messages, intensities):
        message["risk_intensity"] = intensity
    intensity_array = np.asarray(intensities, dtype=np.float64)
    risk_score = quantize(float((intensity_array * weights).sum() / normalizer), decimals)
    risk_candidates = merge_risk_candidates(messages, risk_words)
    risk_level = _risk_level(risk_score, config)

    dominant_sentiment = max(
        messages,
        key=lambda message: (
            abs(float(message["sentiment_score"])) * float(message["effective_weight"]),
            -int(message["msg_no"]),
        ),
    )
    dominant_risk = max(
        messages,
        key=lambda message: (
            float(message["risk_intensity"]) * float(message["effective_weight"]),
            -int(message["msg_no"]),
        ),
    )

    return {
        "sentiment_scores": class_means,
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_candidates": risk_candidates,
        "dominant_sentiment_msg_no": int(dominant_sentiment["msg_no"]),
        "dominant_risk_msg_no": int(dominant_risk["msg_no"]),
        "aggregation_diagnostics": {
            "decay": {
                "enabled": bool(config["aggregation"]["decay"]["enabled"]),
                "half_life_steps": float(
                    config["aggregation"]["decay"]["half_life_steps"]
                ),
                "reference": "max_msg_no",
                "formula": DECAY_FORMULA,
                "factors": {
                    str(msg_no): value
                    for msg_no, value in sorted(decay["factors"].items())
                },
            },
            "normalizer": decay["normalizer"],
            "sentiment_formula": SENTIMENT_AGGREGATION_FORMULA,
            "sentiment_tie_break_order": list(label_order),
            "same_score_message_tie_break": "ascending msg_no",
            "risk_formula": RISK_AGGREGATION_FORMULA,
            "risk_candidate_merge_rule": CANDIDATE_MERGE_RULE,
            "risk_thresholds": [
                {
                    "label": level["label"],
                    "min_score": float(level["min_score"]),
                }
                for level in config["aggregation"]["risk_levels"]
            ],
            "precision_decimals": decimals,
            "precision_flag": config["precision"]["flag"],
        },
    }


def recompute_from_details(
    result: dict[str, Any],
    config: dict[str, Any],
    risk_words: dict[str, Any],
) -> dict[str, Any]:
    messages = [
        {
            "msg_no": int(message["msg_no"]),
            "weight": float(message["weight"]),
            "sentiment_scores": dict(message["sentiment_scores"]),
            "sentiment_score": float(message["sentiment_score"]),
            "risk_hits": [
                dict(hit)
                for hit in message["risk_hits"]
            ],
        }
        for message in result["messages"]
    ]
    recomputed = aggregate_messages(messages, config, risk_words)
    for source, target in zip(result["messages"], messages):
        source["_recomputed_effective_weight"] = target["effective_weight"]
        source["_recomputed_time_decay"] = target["time_decay"]
        source["_recomputed_risk_intensity"] = target["risk_intensity"]
    return recomputed


def verify_aggregation(
    result: dict[str, Any],
    config: dict[str, Any],
    risk_words: dict[str, Any],
) -> None:
    decimals = int(config["precision"]["decimals"])
    recomputed = recompute_from_details(result, config, risk_words)
    fields = (
        "sentiment_score",
        "sentiment_label",
        "risk_score",
        "risk_level",
        "risk_candidates",
    )
    first_diff_msg_no = None
    for message in result["messages"]:
        if (
            quantize(message["effective_weight"], decimals + 3)
            != quantize(message["_recomputed_effective_weight"], decimals + 3)
            or quantize(message["time_decay"], decimals + 3)
            != quantize(message["_recomputed_time_decay"], decimals + 3)
            or quantize(message["risk_intensity"], decimals)
            != quantize(message["_recomputed_risk_intensity"], decimals)
        ):
            first_diff_msg_no = int(message["msg_no"])
            break
    if first_diff_msg_no is None:
        for field_name in fields:
            left = result[field_name]
            right = recomputed[field_name]
            if isinstance(left, float):
                if quantize(left, decimals) != quantize(right, decimals):
                    break
            elif left != right:
                break
        else:
            for message in result["messages"]:
                message.pop("_recomputed_effective_weight", None)
                message.pop("_recomputed_time_decay", None)
                message.pop("_recomputed_risk_intensity", None)
            return
        first_diff_msg_no = None
    for message in result["messages"]:
        message.pop("_recomputed_effective_weight", None)
        message.pop("_recomputed_time_decay", None)
        message.pop("_recomputed_risk_intensity", None)
    fail(
        "E_AGG_MISMATCH",
        "aggregate cannot be independently recomputed",
        category="aggregation",
        conv_id=result.get("conv_id"),
        first_diff_msg_no=first_diff_msg_no,
    )


def weighted_mean(values: list[float], weights: list[float]) -> float:
    """Small public helper used by independent verification and tests."""

    numerator = math.fsum(value * weight for value, weight in zip(values, weights))
    denominator = math.fsum(weights)
    return numerator / denominator

