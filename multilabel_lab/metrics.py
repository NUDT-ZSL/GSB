"""Multi-label metrics computed from auditable sample-label cells."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .config import UNCERTAIN_STRATEGIES, ZERO_DIVISION_POLICIES
from .errors import E_LABEL_INDEX, E_METRIC_UNDEFINED, LabError


def _resolve_ratio(
    numerator: float,
    denominator: float,
    *,
    zero_division: str | None,
    label: str,
    metric: str,
) -> float | None:
    if denominator != 0:
        return float(numerator) / float(denominator)
    if zero_division not in ZERO_DIVISION_POLICIES:
        raise LabError(
            E_METRIC_UNDEFINED,
            "zero-division policy is missing or invalid",
            stage="metrics",
            label=label,
            details={
                "metric": metric,
                "zero_division": zero_division,
                "allowed": list(ZERO_DIVISION_POLICIES),
            },
        )
    if zero_division == "zero":
        return 0.0
    if zero_division == "one":
        return 1.0
    if zero_division == "skip":
        return None
    raise LabError(
        E_METRIC_UNDEFINED,
        "metric denominator is zero under the error policy",
        stage="metrics",
        label=label,
        details={"metric": metric, "numerator": numerator, "denominator": denominator},
    )


def _validate_label_sequence(labels: Sequence[str]) -> list[str]:
    label_list = list(labels)
    if not label_list or len(set(label_list)) != len(label_list):
        raise LabError(
            E_METRIC_UNDEFINED,
            "metrics require a non-empty unique label order",
            stage="metrics",
            label=label_list[0] if label_list else "*",
        )
    return label_list


def _build_cells(
    sample_records: Iterable[dict[str, Any]],
    labels: list[str],
    uncertain_strategy: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if uncertain_strategy not in UNCERTAIN_STRATEGIES:
        raise LabError(
            E_METRIC_UNDEFINED,
            "uncertain-label strategy is not supported",
            stage="metrics",
            label=labels[0],
            details={"allowed": list(UNCERTAIN_STRATEGIES)},
        )
    label_set = set(labels)
    cells: list[dict[str, Any]] = []
    uncertain_counts = {label: {"uncertain": 0, "predicted_positive": 0} for label in labels}
    seen_ids: set[str] = set()
    for record in sample_records:
        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in seen_ids:
            raise LabError(
                E_METRIC_UNDEFINED,
                "metric sample IDs must be unique non-empty strings",
                stage="metrics",
                sample_id=sample_id,
                label=labels[0],
            )
        seen_ids.add(sample_id)
        truth = list(record.get("truth_labels", []))
        uncertain = list(record.get("uncertain_labels", []))
        prediction = list(record.get("predicted_labels", []))
        for field_name, values in (
            ("truth_labels", truth),
            ("uncertain_labels", uncertain),
            ("predicted_labels", prediction),
        ):
            if len(set(values)) != len(values):
                raise LabError(
                    E_LABEL_INDEX,
                    f"{field_name} contains duplicate labels",
                    stage="metrics",
                    sample_id=sample_id,
                )
            unknown = [label for label in values if label not in label_set]
            if unknown:
                raise LabError(
                    E_LABEL_INDEX,
                    f"{field_name} contains a label outside the controlled label set",
                    stage="metrics",
                    sample_id=sample_id,
                    label=str(unknown[0]),
                )
        truth_set = set(truth)
        uncertain_set = set(uncertain)
        prediction_set = set(prediction)
        for label in labels:
            is_truth = label in truth_set
            is_prediction = label in prediction_set
            is_uncertain = label in uncertain_set
            included = True
            reason: str | None = None
            if is_uncertain:
                uncertain_counts[label]["uncertain"] += 1
                if is_prediction:
                    uncertain_counts[label]["predicted_positive"] += 1
                if uncertain_strategy == "positive":
                    is_truth = True
                    reason = "uncertain_positive"
                elif uncertain_strategy == "ignore":
                    included = False
                    reason = "uncertain_ignored"
                else:
                    included = False
                    reason = "uncertain_separate"
            cells.append(
                {
                    "sample_id": sample_id,
                    "label": label,
                    "truth": int(bool(is_truth)),
                    "prediction": int(bool(is_prediction)),
                    "included": included,
                    "uncertain": is_uncertain,
                    "reason": reason,
                }
            )
    if not cells:
        raise LabError(
            E_METRIC_UNDEFINED,
            "metrics require at least one sample-label cell",
            stage="metrics",
            label=labels[0],
        )
    uncertainty = {
        "strategy": uncertain_strategy,
        "excluded_cells": sum(1 for cell in cells if not cell["included"]),
        "per_label": [
            {
                "label": label,
                "uncertain": uncertain_counts[label]["uncertain"],
                "predicted_positive": uncertain_counts[label]["predicted_positive"],
            }
            for label in labels
        ],
    }
    return cells, uncertainty


def _aggregate(
    cells: list[dict[str, Any]],
    labels: list[str],
    *,
    zero_division: str | None,
) -> dict[str, Any]:
    if zero_division not in ZERO_DIVISION_POLICIES:
        raise LabError(
            E_METRIC_UNDEFINED,
            "zero-division policy is missing or invalid",
            stage="metrics",
            label=labels[0],
            details={
                "metric": "zero_division_policy",
                "zero_division": zero_division,
                "allowed": list(ZERO_DIVISION_POLICIES),
            },
        )
    per_label: list[dict[str, Any]] = []
    total_tp = total_fp = total_fn = total_tn = 0
    included_cells = 0
    for label in labels:
        label_cells = [cell for cell in cells if cell["label"] == label and cell["included"]]
        tp = sum(1 for cell in label_cells if cell["truth"] == 1 and cell["prediction"] == 1)
        fp = sum(1 for cell in label_cells if cell["truth"] == 0 and cell["prediction"] == 1)
        fn = sum(1 for cell in label_cells if cell["truth"] == 1 and cell["prediction"] == 0)
        tn = sum(1 for cell in label_cells if cell["truth"] == 0 and cell["prediction"] == 0)
        total_tp += tp
        total_fp += fp
        total_fn += fn
        total_tn += tn
        included_cells += len(label_cells)
        precision = _resolve_ratio(
            tp,
            tp + fp,
            zero_division=zero_division,
            label=label,
            metric="precision",
        )
        recall = _resolve_ratio(
            tp,
            tp + fn,
            zero_division=zero_division,
            label=label,
            metric="recall",
        )
        f1 = _resolve_ratio(
            2 * tp,
            2 * tp + fp + fn,
            zero_division=zero_division,
            label=label,
            metric="f1",
        )
        per_label.append(
            {
                "label": label,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "tn": tn,
                "included_cells": len(label_cells),
            }
        )

    micro_precision = _resolve_ratio(
        total_tp,
        total_tp + total_fp,
        zero_division=zero_division,
        label="__micro__",
        metric="micro_precision",
    )
    micro_recall = _resolve_ratio(
        total_tp,
        total_tp + total_fn,
        zero_division=zero_division,
        label="__micro__",
        metric="micro_recall",
    )
    micro_f1 = _resolve_ratio(
        2 * total_tp,
        2 * total_tp + total_fp + total_fn,
        zero_division=zero_division,
        label="__micro__",
        metric="micro_f1",
    )
    defined_f1 = [entry["f1"] for entry in per_label if entry["f1"] is not None]
    macro_f1 = sum(defined_f1) / len(defined_f1) if defined_f1 else None
    hamming_loss = _resolve_ratio(
        total_fp + total_fn,
        included_cells,
        zero_division=zero_division,
        label="__hamming__",
        metric="hamming_loss",
    )
    return {
        "labels": labels,
        "zero_division": zero_division,
        "micro": {
            "precision": micro_precision,
            "recall": micro_recall,
            "f1": micro_f1,
            "tp": total_tp,
            "fp": total_fp,
            "fn": total_fn,
            "tn": total_tn,
        },
        "macro_f1": macro_f1,
        "macro_label_count": len(defined_f1),
        "per_label": per_label,
        "hamming_loss": hamming_loss,
        "included_cells": included_cells,
    }


def compute_metrics(
    sample_records: Iterable[dict[str, Any]],
    labels: Sequence[str],
    *,
    uncertain_strategy: str,
    zero_division: str | None,
) -> dict[str, Any]:
    """Compute metrics and retain all per-sample/per-label evidence."""

    label_list = _validate_label_sequence(labels)
    cells, uncertainty = _build_cells(sample_records, label_list, uncertain_strategy)
    metrics = _aggregate(cells, label_list, zero_division=zero_division)
    metrics["uncertainty"] = uncertainty
    metrics["details"] = cells
    return metrics


def recompute_metrics_from_details(
    details: Iterable[dict[str, Any]],
    labels: Sequence[str],
    *,
    zero_division: str | None,
) -> dict[str, Any]:
    """Independently recompute aggregate metrics from serialized cells."""

    label_list = _validate_label_sequence(labels)
    label_set = set(label_list)
    cells: list[dict[str, Any]] = []
    for raw in details:
        if not isinstance(raw, dict):
            raise LabError(
                E_METRIC_UNDEFINED,
                "metric detail cell must be an object",
                stage="metrics",
                label=label_list[0],
            )
        label = raw.get("label")
        if label not in label_set:
            raise LabError(
                E_METRIC_UNDEFINED,
                "metric detail cell contains an unknown label",
                stage="metrics",
                sample_id=raw.get("sample_id"),
                label=label,
            )
        if (
            raw.get("truth") not in (0, 1)
            or raw.get("prediction") not in (0, 1)
            or not isinstance(raw.get("included"), bool)
        ):
            raise LabError(
                E_METRIC_UNDEFINED,
                "metric detail cell has invalid values",
                stage="metrics",
                sample_id=raw.get("sample_id"),
                label=label,
            )
        cells.append(
            {
                "sample_id": raw.get("sample_id"),
                "label": label,
                "truth": int(raw["truth"]),
                "prediction": int(raw["prediction"]),
                "included": bool(raw["included"]),
            }
        )
    return _aggregate(cells, label_list, zero_division=zero_division)
