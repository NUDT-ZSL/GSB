from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from .errors import E_INCONSISTENT, PipelineError
from .models import ExpandedSample


@dataclass
class _Deviation:
    category: str
    stage: str
    message: str
    position: dict[str, Any] = field(default_factory=dict)


def _independent_auc(details: Sequence[dict[str, Any]]) -> float:
    positives = [detail for detail in details if int(detail["label"]) == 1]
    negatives = [detail for detail in details if int(detail["label"]) == 0]
    total_pairs = len(positives) * len(negatives)
    if total_pairs == 0:
        return 0.5
    wins = 0.0
    for positive in positives:
        positive_score = float(positive["score"])
        for negative in negatives:
            negative_score = float(negative["score"])
            if positive_score > negative_score:
                wins += 1.0
            elif positive_score == negative_score:
                wins += 0.5
    return wins / total_pairs


def _independent_log_loss(details: Sequence[dict[str, Any]]) -> float:
    if not details:
        return 0.0
    total = 0.0
    for detail in details:
        probability = float(detail["score"])
        probability = max(probability, 1e-15)
        probability = min(probability, 1.0 - 1e-15)
        label = int(detail["label"])
        if label == 1:
            total -= math.log(probability)
        else:
            total -= math.log(1.0 - probability)
    return total / len(details)


def _first_id_delta(
    expected: Sequence[str],
    actual: Sequence[str],
) -> dict[str, Any] | None:
    expected_set = set(expected)
    actual_set = set(actual)
    if expected_set == actual_set:
        return None
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    combined = sorted(missing + unexpected)
    return {
        "first_sample_id": combined[0] if combined else None,
        "missing": missing[:10],
        "missing_count": len(missing),
        "unexpected": unexpected[:10],
        "unexpected_count": len(unexpected),
    }


def _check_id_sets(
    result: dict[str, Any],
    deviations: list[_Deviation],
) -> None:
    stage_ids = result.get("stage_ids", {})
    ingested = list(stage_ids.get("ingested", []))
    expanded = list(stage_ids.get("expanded", []))
    admitted = list(stage_ids.get("admitted", []))
    rejected = list(stage_ids.get("rejected", []))
    scored = list(stage_ids.get("scored", []))
    trained = list(result.get("summary", {}).get("trained_sample_ids", []))

    delta = _first_id_delta(ingested, expanded)
    if delta is not None:
        deviations.append(
            _Deviation(
                "sample_id_set",
                "ingest_vs_expand",
                "ingested and expanded sample ID sets differ",
                delta,
            )
        )
        return

    if set(admitted) & set(rejected):
        overlap = sorted(set(admitted) & set(rejected))
        deviations.append(
            _Deviation(
                "rejected_sample_present",
                "admission",
                "admitted and rejected sample ID sets overlap",
                {"first_sample_id": overlap[0], "overlap_count": len(overlap)},
            )
        )
        return
    union_delta = _first_id_delta(ingested, list(admitted) + list(rejected))
    if union_delta is not None:
        deviations.append(
            _Deviation(
                "sample_id_set",
                "admission",
                "admitted plus rejected sample IDs do not equal ingested IDs",
                union_delta,
            )
        )
        return

    if result.get("status") in {"budget_rejected"}:
        if admitted:
            deviations.append(
                _Deviation(
                    "sample_id_set",
                    "budget_rejected",
                    "budget-rejected result unexpectedly admits samples",
                    {"position": 0, "first_sample_id": admitted[0]},
                )
            )
            return
        leaked = sorted(
            set(rejected)
            & set(
                scored
                + trained
                + list((result.get("chunked") or {}).get("sample_ids", []))
                + list((result.get("full") or {}).get("sample_ids", []))
            )
        )
        if leaked:
            deviations.append(
                _Deviation(
                    "rejected_sample_present",
                    "budget_rejected",
                    "a budget-rejected sample appears in training output",
                    {"first_sample_id": leaked[0]},
                )
            )
        return
    if result.get("status") == "partial":
        leaked = sorted(
            set(rejected)
            & set(
                scored
                + trained
                + list((result.get("chunked") or {}).get("sample_ids", []))
                + list((result.get("full") or {}).get("sample_ids", []))
            )
        )
        if leaked:
            deviations.append(
                _Deviation(
                    "rejected_sample_present",
                    "partial_training",
                    "a rejected sample appears in partial training output",
                    {"first_sample_id": leaked[0]},
                )
            )
        return

    for stage, values in (
        ("scored_vs_admitted", scored),
        ("summary_vs_admitted", trained),
    ):
        delta = _first_id_delta(admitted, values)
        if delta is not None:
            deviations.append(
                _Deviation(
                    "sample_id_set",
                    stage,
                    f"{stage.split('_vs_')[0]} and admitted sample ID sets differ",
                    delta,
                )
            )
            return

    chunked_ids = list(result.get("chunked", {}).get("sample_ids", []))
    full_ids = list(result.get("full", {}).get("sample_ids", []))
    for stage, values in (
        ("chunked_vs_admitted", chunked_ids),
        ("full_vs_admitted", full_ids),
    ):
        delta = _first_id_delta(admitted, values)
        if delta is not None:
            deviations.append(
                _Deviation(
                    "sample_id_set",
                    stage,
                    f"{stage.split('_vs_')[0]} and admitted sample ID sets differ",
                    delta,
                )
            )
            return

    for sample_id in sorted(set(rejected) & set(scored + trained + chunked_ids + full_ids)):
        deviations.append(
            _Deviation(
                "rejected_sample_present",
                "summary",
                "a rejected sample appears in training or scoring output",
                {"first_sample_id": sample_id},
            )
        )
        return


def _check_reduction(
    result: dict[str, Any],
    deviations: list[_Deviation],
) -> dict[str, Any]:
    chunked = result.get("chunked", {})
    config = result.get("config", {})
    initial = np.asarray(chunked.get("initial_parameters", []), dtype=np.float64)
    parameters = initial.copy()
    learning_rate = float(config.get("learning_rate", 0.0))
    l2 = float(config.get("l2", 0.0))
    recomputed_rounds: list[dict[str, Any]] = []

    for round_row in sorted(chunked.get("rounds", []), key=lambda row: row["round"]):
        round_number = int(round_row["round"])
        chunk_results = sorted(
            round_row.get("chunk_results", []),
            key=lambda row: (row["batch_index"], row["chunk_index"]),
        )
        if not chunk_results:
            deviations.append(
                _Deviation(
                    "reduction_missing_chunks",
                    "chunk_reduction",
                    "round has no chunk reduction results",
                    {"round": round_number},
                )
            )
            return recomputed_rounds
        loss_sum = 0.0
        gradient_sum = np.zeros_like(initial)
        sample_count = 0
        for chunk in chunk_results:
            loss_sum += float(chunk["loss_sum"])
            gradient_sum += np.asarray(chunk["gradient"], dtype=np.float64)
            sample_count += int(chunk["sample_count"])
        if sample_count <= 0:
            deviations.append(
                _Deviation(
                    "reduction_zero_samples",
                    "chunk_reduction",
                    "round has zero reduced samples",
                    {"round": round_number},
                )
            )
            return recomputed_rounds
        recomputed_loss = loss_sum / sample_count
        if abs(recomputed_loss - float(round_row["loss"])) > 1e-12:
            deviations.append(
                _Deviation(
                    "loss_reduction_mismatch",
                    "chunk_reduction",
                    "loss cannot be reproduced from chunk loss sums",
                    {
                        "expected": recomputed_loss,
                        "actual": float(round_row["loss"]),
                        "round": round_number,
                    },
                )
            )
            return recomputed_rounds
        recomputed_gradient = gradient_sum / sample_count
        reported_gradient = np.asarray(round_row["gradient"], dtype=np.float64)
        gradient_delta = np.abs(recomputed_gradient - reported_gradient)
        if float(np.max(gradient_delta)) > 1e-12:
            index = int(np.argmax(gradient_delta))
            deviations.append(
                _Deviation(
                    "gradient_reduction_mismatch",
                    "chunk_reduction",
                    "mean gradient cannot be reproduced from chunk gradients",
                    {
                        "parameter_index": index,
                        "round": round_number,
                    },
                )
            )
            return recomputed_rounds
        update_gradient = recomputed_gradient
        if l2:
            update_gradient = update_gradient + l2 * parameters
        parameters = parameters - learning_rate * update_gradient
        expected_round_parameters = np.asarray(
            round_row["parameters"], dtype=np.float64
        )
        parameter_delta = np.abs(parameters - expected_round_parameters)
        if float(np.max(parameter_delta)) > 1e-12:
            index = int(np.argmax(parameter_delta))
            deviations.append(
                _Deviation(
                    "parameter_reduction_mismatch",
                    "chunk_reduction",
                    "round parameters cannot be reproduced from reduced gradients",
                    {
                        "parameter_index": index,
                        "round": round_number,
                    },
                )
            )
            return recomputed_rounds
        recomputed_rounds.append(
            {
                "loss": recomputed_loss,
                "parameters": [float(value) for value in parameters.tolist()],
                "round": round_number,
            }
        )

    final_parameters = np.asarray(chunked.get("parameters", []), dtype=np.float64)
    final_delta = np.abs(parameters - final_parameters)
    if final_delta.size and float(np.max(final_delta)) > 1e-12:
        index = int(np.argmax(final_delta))
        deviations.append(
            _Deviation(
                "final_parameter_mismatch",
                "chunk_reduction",
                "final coefficients cannot be reproduced from chunk reductions",
                {
                    "parameter_index": index,
                    "round": len(recomputed_rounds),
                },
            )
        )
    return {
        "final_parameters": [float(value) for value in parameters.tolist()],
        "rounds": recomputed_rounds,
    }


def _check_metrics(
    result: dict[str, Any],
    deviations: list[_Deviation],
) -> dict[str, Any]:
    chunked = result.get("chunked", {})
    scores = chunked.get("scores", [])
    recomputed_auc = _independent_auc(scores)
    recomputed_log_loss = _independent_log_loss(scores)
    reported = chunked.get("metrics") or {}
    if abs(recomputed_auc - float(reported.get("auc", -1.0))) > 1e-12:
        deviations.append(
            _Deviation(
                "auc_mismatch",
                "metric_validation",
                "AUC cannot be reproduced from per-sample score details",
                {
                    "expected": recomputed_auc,
                    "actual": reported.get("auc"),
                    "field": "chunked.metrics.auc",
                },
            )
        )
        return {"auc": recomputed_auc, "log_loss": recomputed_log_loss}
    if abs(recomputed_log_loss - float(reported.get("log_loss", -1.0))) > 1e-12:
        deviations.append(
            _Deviation(
                "log_loss_mismatch",
                "metric_validation",
                "LogLoss cannot be reproduced from per-sample score details",
                {
                    "expected": recomputed_log_loss,
                    "actual": reported.get("log_loss"),
                    "field": "chunked.metrics.log_loss",
                },
            )
        )
        return {"auc": recomputed_auc, "log_loss": recomputed_log_loss}

    summary_metrics = result.get("summary", {}).get("metrics") or {}
    for metric_name, value in (
        ("auc", recomputed_auc),
        ("log_loss", recomputed_log_loss),
    ):
        if abs(value - float(summary_metrics.get(metric_name, -1.0))) > 1e-12:
            deviations.append(
                _Deviation(
                    "summary_metric_mismatch",
                    "metric_validation",
                    "summary metric differs from independently recomputed score metrics",
                    {
                        "actual": summary_metrics.get(metric_name),
                        "expected": value,
                        "field": f"summary.metrics.{metric_name}",
                    },
                )
            )
            return {"auc": recomputed_auc, "log_loss": recomputed_log_loss}
    return {"auc": recomputed_auc, "log_loss": recomputed_log_loss}


def validate_result(
    result: dict[str, Any],
    *,
    expanded_samples: Sequence[ExpandedSample] | None = None,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    deviations: list[_Deviation] = []
    status = result.get("status")
    _check_id_sets(result, deviations)

    recomputed_reduction: dict[str, Any] | None = None
    recomputed_metrics: dict[str, Any] | None = None
    if not deviations and status == "ok":
        recomputed_reduction = _check_reduction(result, deviations)
    if not deviations and status == "ok":
        recomputed_metrics = _check_metrics(result, deviations)

    if expanded_samples is not None and not deviations:
        expected_ids = [sample.sample_id for sample in expanded_samples]
        delta = _first_id_delta(expected_ids, result.get("stage_ids", {}).get("expanded", []))
        if delta is not None:
            deviations.append(
                _Deviation(
                    "input_expansion_mismatch",
                    "input_vs_result",
                    "provided input samples differ from the expanded result stage",
                    delta,
                )
            )

    if deviations:
        first = deviations[0]
        error = PipelineError(
            E_INCONSISTENT,
            "validation",
            first.message,
            {
                "category": first.category,
                "first_deviation": {
                    "category": first.category,
                    "message": first.message,
                    "position": first.position,
                    "stage": first.stage,
                },
                "deviation_count": len(deviations),
            },
        )
        if raise_on_error:
            raise error
        return {
            "checks": {"deviation_count": len(deviations)},
            "error": error.to_dict(),
            "status": "inconsistent",
        }

    checks = {
        "auc_recomputed": recomputed_metrics is not None,
        "log_loss_recomputed": recomputed_metrics is not None,
        "reduction_recomputed": recomputed_reduction is not None,
        "sample_id_sets_consistent": True,
    }
    if status == "ok":
        checks["degraded_mode_supported"] = bool(
            result.get("summary", {}).get("degraded", False)
        )
    return {
        "checks": checks,
        "recomputed": {
            "metrics": recomputed_metrics,
            "reduction": recomputed_reduction,
        },
        "status": "ok" if status == "ok" else status,
    }
