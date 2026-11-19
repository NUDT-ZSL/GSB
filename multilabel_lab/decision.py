"""Per-label threshold decisions and consistency checks."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from .errors import E_INCONSISTENT, LabError


def decide_scores(
    scores: Sequence[float],
    thresholds: Sequence[float],
    labels: Sequence[str],
    *,
    sample_id: str,
) -> list[bool]:
    if len(scores) != len(labels) or len(thresholds) != len(labels):
        position = min(len(scores), len(thresholds), len(labels))
        first_label = labels[position] if position < len(labels) else (
            labels[-1] if labels else None
        )
        raise LabError(
            E_INCONSISTENT,
            "score and threshold vectors must align with the label order",
            stage="decision",
            sample_id=sample_id,
            label=first_label,
            details={
                "label_count": len(labels),
                "score_count": len(scores),
                "threshold_count": len(thresholds),
            },
        )
    return [
        bool(float(score) >= float(threshold))
        for score, threshold in zip(scores, thresholds, strict=True)
    ]


def validate_decision_alignment(
    *,
    sample_id: str,
    labels: Sequence[str],
    scores: Sequence[float],
    thresholds: Sequence[float],
    decisions: Sequence[bool],
    predicted_labels: Sequence[str],
) -> None:
    if not (len(labels) == len(scores) == len(thresholds) == len(decisions)):
        position = min(len(labels), len(scores), len(thresholds), len(decisions))
        label = labels[position] if position < len(labels) else (
            labels[-1] if labels else None
        )
        raise LabError(
            E_INCONSISTENT,
            "sample label, score, threshold, and decision vectors are misaligned",
            stage="decision",
            sample_id=sample_id,
            label=label,
            details={
                "label_count": len(labels),
                "score_count": len(scores),
                "threshold_count": len(thresholds),
                "decision_count": len(decisions),
            },
        )
    expected_labels = [label for label, decision in zip(labels, decisions, strict=True) if decision]
    if list(predicted_labels) != expected_labels:
        raise LabError(
            E_INCONSISTENT,
            "predicted label set does not correspond to label-order decisions",
            stage="decision",
            sample_id=sample_id,
            label=next(
                (
                    label
                    for label, decision in zip(labels, decisions, strict=True)
                    if ((label in predicted_labels) != bool(decision))
                ),
                None,
            ),
            details={
                "expected_labels": expected_labels,
                "actual_labels": list(predicted_labels),
            },
        )
    for label, score, threshold, decision in zip(
        labels, scores, thresholds, decisions, strict=True
    ):
        if bool(float(score) >= float(threshold)) != bool(decision):
            raise LabError(
                E_INCONSISTENT,
                "decision cannot be reproduced from its label score and threshold",
                stage="decision",
                sample_id=sample_id,
                label=label,
                details={"score": float(score), "threshold": float(threshold), "decision": decision},
            )
