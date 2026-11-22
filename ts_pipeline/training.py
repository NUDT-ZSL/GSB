"""Chunked training with explicit reduction order and one-shot comparison."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .errors import ConfigInvalid
from .models import (
    LinearARModel,
    Model,
    Stump,
    StumpBoostingModel,
    model_parameter_sha256,
)
from .precision import Quantizer
from .utils import (
    NeumaierSum,
    array_digest,
    guard_subnormal,
    ordered_sum,
    sha256_json,
)


@dataclass
class TrainingResult:
    model: Model
    full_batch_model: Model
    pre_model_quantization_model: Model
    rounds: list[dict[str, Any]]
    full_batch_rounds: list[dict[str, Any]]
    comparisons: list[dict[str, Any]]
    sample_count: int
    chunk_size: int
    reduction_order: str
    compensated_sum: bool
    subnormal_guard: bool

    def summary(self) -> dict[str, Any]:
        loss_deviations = [
            item["loss_absolute_deviation"] for item in self.comparisons
        ]
        parameter_deviations = [
            item["parameter_max_abs_deviation"] for item in self.comparisons
        ]
        return {
            "model_family": self.model.family,
            "sample_count": self.sample_count,
            "chunk_size": self.chunk_size,
            "reduction_order": self.reduction_order,
            "compensated_sum": self.compensated_sum,
            "subnormal_guard": self.subnormal_guard,
            "round_count": len(self.rounds),
            "model": self.model.to_dict(),
            "full_batch_model": self.full_batch_model.to_dict(),
            "rounds": self.rounds,
            "full_batch_rounds": self.full_batch_rounds,
            "full_batch_comparison": {
                "rounds": self.comparisons,
                "max_loss_absolute_deviation": max(loss_deviations, default=0.0),
                "max_parameter_absolute_deviation": max(
                    parameter_deviations,
                    default=0.0,
                ),
                "loss_equals_full_batch_exactly": all(
                    finite_equal(item["loss_absolute_deviation"], 0.0)
                    for item in self.comparisons
                ),
                "parameter_equals_full_batch_exactly": all(
                    finite_equal(item["parameter_max_abs_deviation"], 0.0)
                    for item in self.comparisons
                ),
            },
            "reproducibility": {
                "model_parameter_sha256": model_parameter_sha256(self.model),
                "full_batch_parameter_sha256": model_parameter_sha256(
                    self.full_batch_model
                ),
                "rounds_sha256": sha256_json(self.rounds),
                "full_batch_rounds_sha256": sha256_json(self.full_batch_rounds),
            },
        }


def finite_equal(left: float, right: float) -> bool:
    return math.isfinite(float(left)) and abs(float(left) - float(right)) == 0.0


def _safe_loss(value: float, guard: bool) -> float:
    return guard_subnormal(value, guard)


def _loss_and_gradient(
    X: np.ndarray,
    y: np.ndarray,
    coefficients: np.ndarray,
    intercept: float,
    *,
    compensated: bool,
    subnormal_guard: bool,
) -> tuple[float, np.ndarray, float]:
    sample_count = int(X.shape[0])
    squared_errors: list[float] = []
    gradient = np.zeros(X.shape[1], dtype=np.float64)
    intercept_gradient = 0.0
    for row in range(sample_count):
        prediction = float(intercept)
        if compensated:
            dot = NeumaierSum()
            for column in range(X.shape[1]):
                dot.add(float(X[row, column]) * float(coefficients[column]))
            prediction += dot.value
        else:
            for column in range(X.shape[1]):
                prediction += float(X[row, column]) * float(coefficients[column])
        prediction = guard_subnormal(prediction, subnormal_guard)
        residual = prediction - float(y[row])
        squared_errors.append(residual * residual)
        scale = 2.0 * residual
        for column in range(X.shape[1]):
            gradient[column] += scale * float(X[row, column])
        intercept_gradient += scale

    squared_sum = ordered_sum(squared_errors, compensated)
    return (
        _safe_loss(squared_sum / float(sample_count), subnormal_guard),
        gradient,
        intercept_gradient,
    )


def _chunk_ranges(sample_count: int, chunk_size: int) -> list[tuple[int, int]]:
    return [
        (start, min(start + chunk_size, sample_count))
        for start in range(0, sample_count, chunk_size)
    ]


def _train_linear_mode(
    X: np.ndarray,
    y: np.ndarray,
    config: Mapping[str, Any],
    *,
    chunk_size: int,
    model_quantizer: Quantizer,
) -> tuple[LinearARModel, list[dict[str, Any]]]:
    """Linear trainer with per-coefficient compensated chunk reduction."""

    model_config = config["model"]
    training_config = config["training"]
    epochs = int(model_config["epochs"])
    learning_rate = float(model_config["learning_rate"])
    l2 = float(model_config["l2"])
    compensated = bool(training_config["compensated_sum"])
    guard = bool(training_config["subnormal_guard"])
    coefficients = np.zeros(X.shape[1], dtype=np.float64)
    intercept = 0.0
    rounds: list[dict[str, Any]] = []
    ranges = _chunk_ranges(int(X.shape[0]), chunk_size)

    for epoch in range(1, epochs + 1):
        coefficient_accumulators = [NeumaierSum() for _ in range(X.shape[1])]
        intercept_accumulator = NeumaierSum()
        aggregated_gradient = np.zeros(X.shape[1], dtype=np.float64)
        aggregated_intercept = 0.0
        squared_error_sum = 0.0
        l2_penalty = float(l2) * ordered_sum(
            (float(value) * float(value) for value in coefficients),
            compensated,
        )
        chunk_records: list[dict[str, Any]] = []
        for chunk_index, (start, end) in enumerate(ranges):
            chunk_loss, chunk_gradient, chunk_intercept_gradient = _loss_and_gradient(
                X[start:end],
                y[start:end],
                coefficients,
                intercept,
                compensated=compensated,
                subnormal_guard=guard,
            )
            chunk_count = end - start
            squared_error_sum += chunk_loss * float(chunk_count)
            if compensated:
                for column in range(X.shape[1]):
                    coefficient_accumulators[column].add(
                        float(chunk_gradient[column])
                    )
                intercept_accumulator.add(float(chunk_intercept_gradient))
            else:
                for column in range(X.shape[1]):
                    aggregated_gradient[column] += float(chunk_gradient[column])
                aggregated_intercept += float(chunk_intercept_gradient)
            chunk_records.append(
                {
                    "chunk_index": chunk_index,
                    "row_start": start,
                    "row_end_exclusive": end,
                    "loss": float(chunk_loss),
                    "gradient_digest": array_digest(chunk_gradient),
                    "intercept_gradient": float(chunk_intercept_gradient),
                }
            )
        if compensated:
            aggregated_gradient = np.asarray(
                [item.value for item in coefficient_accumulators],
                dtype=np.float64,
            )
            aggregated_intercept = intercept_accumulator.value
        aggregated_gradient = np.ascontiguousarray(
            aggregated_gradient / float(X.shape[0]),
            dtype=np.float64,
        )
        aggregated_intercept /= float(X.shape[0])
        if l2:
            aggregated_gradient = np.ascontiguousarray(
                aggregated_gradient + 2.0 * float(l2) * coefficients,
                dtype=np.float64,
            )
        epoch_loss = _safe_loss(
            squared_error_sum / float(X.shape[0]) + l2_penalty,
            guard,
        )
        gradient_norm = float(
            np.sqrt(
                ordered_sum(
                    (float(value) * float(value) for value in aggregated_gradient),
                    compensated,
                )
                + aggregated_intercept * aggregated_intercept
            )
        )
        coefficients = np.ascontiguousarray(
            coefficients - learning_rate * aggregated_gradient,
            dtype=np.float64,
        )
        intercept = guard_subnormal(
            intercept - learning_rate * aggregated_intercept,
            guard,
        )
        coefficients = np.asarray(
            [
                guard_subnormal(float(value), guard)
                for value in model_quantizer.quantize_array(coefficients)
            ],
            dtype=np.float64,
        )
        intercept = guard_subnormal(
            model_quantizer.quantize_scalar(intercept),
            guard,
        )
        rounds.append(
            {
                "epoch": epoch,
                "loss": float(epoch_loss),
                "gradient": [float(value) for value in aggregated_gradient],
                "intercept_gradient": float(aggregated_intercept),
                "gradient_norm": gradient_norm,
                "coefficient_digest": array_digest(coefficients),
                "intercept": float(intercept),
                "parameter_vector": [
                    *[float(value) for value in coefficients],
                    float(intercept),
                ],
                "chunks": chunk_records,
                "chunk_count": len(chunk_records),
                "loss_reduction": "epoch_then_chunk_then_row",
                "gradient_reduction": "epoch_then_chunk_then_row",
            }
        )

    temporary_model = LinearARModel(
        coefficients=coefficients,
        intercept=intercept,
        residual_std=0.0,
        compensated_sum=compensated,
        subnormal_guard=guard,
        feature_count=int(X.shape[1]),
    )
    predictions = np.asarray(
        [temporary_model.predict_one(row) for row in X],
        dtype=np.float64,
    )
    residuals = predictions - y
    residual_std = float(np.sqrt(np.mean(residuals * residuals)))
    model = LinearARModel(
        coefficients=coefficients,
        intercept=intercept,
        residual_std=model_quantizer.quantize_scalar(residual_std),
        compensated_sum=compensated,
        subnormal_guard=guard,
        feature_count=int(X.shape[1]),
    )
    return model, rounds


def _ordered_bincount(
    indices: np.ndarray,
    weights: np.ndarray,
    bins: int,
    compensated: bool,
) -> np.ndarray:
    result = np.zeros(bins, dtype=np.float64)
    if compensated:
        accumulators = [NeumaierSum() for _ in range(bins)]
        for index, weight in zip(indices.tolist(), weights.tolist()):
            accumulators[int(index)].add(float(weight))
        for index, accumulator in enumerate(accumulators):
            result[index] = accumulator.value
    else:
        for index, weight in zip(indices.tolist(), weights.tolist()):
            result[int(index)] += float(weight)
    return result


def _train_stumps(
    X: np.ndarray,
    y: np.ndarray,
    config: Mapping[str, Any],
    *,
    chunk_size: int,
    model_quantizer: Quantizer,
) -> tuple[StumpBoostingModel, list[dict[str, Any]]]:
    model_config = config["model"]
    training_config = config["training"]
    rounds_count = int(model_config["stump_rounds"])
    learning_rate = float(model_config["stump_learning_rate"])
    min_leaf = int(model_config["stump_min_leaf"])
    compensated = bool(training_config["compensated_sum"])
    guard = bool(training_config["subnormal_guard"])
    sample_count = int(X.shape[0])
    ranges = _chunk_ranges(sample_count, chunk_size)
    base = ordered_sum((float(value) for value in y), compensated) / sample_count
    base = model_quantizer.quantize_scalar(base)
    learning_rate = model_quantizer.quantize_scalar(learning_rate)
    predictions = np.full(sample_count, base, dtype=np.float64)
    stumps: list[Stump] = []
    rounds: list[dict[str, Any]] = []

    for round_index in range(1, rounds_count + 1):
        residual = y - predictions
        loss = _safe_loss(
            ordered_sum(
                (float(value) * float(value) for value in residual),
                compensated,
            )
            / float(sample_count),
            guard,
        )
        gradient_norm = float(
            np.sqrt(
                ordered_sum(
                    (
                        (2.0 * float(value) / float(sample_count)) ** 2
                        for value in residual
                    ),
                    compensated,
                )
            )
        )
        best: dict[str, Any] | None = None
        for feature_index in range(X.shape[1]):
            values = X[:, feature_index]
            unique_values = np.unique(values)
            if unique_values.size < 2:
                continue
            bins = int(unique_values.size)
            inverse = np.searchsorted(unique_values, values, side="right") - 1
            threshold_values = (unique_values[:-1] + unique_values[1:]) / 2.0
            counts = np.zeros(bins, dtype=np.float64)
            sum_accumulators = [NeumaierSum() for _ in range(bins)]
            squared_accumulators = [NeumaierSum() for _ in range(bins)]
            for start, end in ranges:
                chunk_inverse = inverse[start:end]
                chunk_residual = residual[start:end]
                counts += np.bincount(
                    chunk_inverse,
                    minlength=bins,
                ).astype(np.float64)
                chunk_sums = _ordered_bincount(
                    chunk_inverse,
                    chunk_residual,
                    bins,
                    compensated,
                )
                chunk_squared = _ordered_bincount(
                    chunk_inverse,
                    chunk_residual * chunk_residual,
                    bins,
                    compensated,
                )
                for bin_index in range(bins):
                    if compensated:
                        sum_accumulators[bin_index].add(
                            float(chunk_sums[bin_index])
                        )
                        squared_accumulators[bin_index].add(
                            float(chunk_squared[bin_index])
                        )
                    else:
                        sum_accumulators[bin_index].total += float(
                            chunk_sums[bin_index]
                        )
                        squared_accumulators[bin_index].total += float(
                            chunk_squared[bin_index]
                        )
            sums = np.asarray(
                [item.value for item in sum_accumulators],
                dtype=np.float64,
            )
            squared_sums = np.asarray(
                [item.value for item in squared_accumulators],
                dtype=np.float64,
            )
            cumulative_count = np.cumsum(counts)
            cumulative_sum = np.cumsum(sums)
            cumulative_squared = np.cumsum(squared_sums)
            total_count = cumulative_count[-1]
            total_sum = cumulative_sum[-1]
            total_squared = cumulative_squared[-1]
            for split_index in range(bins - 1):
                left_count = cumulative_count[split_index]
                right_count = total_count - left_count
                if left_count < min_leaf or right_count < min_leaf:
                    continue
                left_sum = cumulative_sum[split_index]
                right_sum = total_sum - left_sum
                left_sse = cumulative_squared[split_index] - (
                    left_sum * left_sum / left_count
                )
                right_sse = (total_squared - cumulative_squared[split_index]) - (
                    right_sum * right_sum / right_count
                )
                sse = float(left_sse + right_sse)
                candidate = {
                    "feature_index": feature_index,
                    "threshold": float(threshold_values[split_index]),
                    "left_value": float(left_sum / left_count),
                    "right_value": float(right_sum / right_count),
                    "sse": sse,
                }
                if best is None or (
                    candidate["sse"],
                    candidate["feature_index"],
                    candidate["threshold"],
                ) < (
                    best["sse"],
                    best["feature_index"],
                    best["threshold"],
                ):
                    best = candidate
        if best is None:
            raise ValueError("no valid stump split under min_leaf constraint")
        stump = Stump(
            feature_index=int(best["feature_index"]),
            threshold=model_quantizer.quantize_scalar(float(best["threshold"])),
            left_value=model_quantizer.quantize_scalar(float(best["left_value"])),
            right_value=model_quantizer.quantize_scalar(float(best["right_value"])),
        )
        stumps.append(stump)
        for row in range(sample_count):
            predictions[row] += learning_rate * stump.predict(
                float(X[row, stump.feature_index])
            )
            predictions[row] = guard_subnormal(predictions[row], guard)
        rounds.append(
            {
                "round": round_index,
                "loss": float(loss),
                "gradient_norm": gradient_norm,
                "selected_stump": stump.to_dict(),
                "parameter_vector": [
                    float(base),
                    float(learning_rate),
                    *[
                        float(value)
                        for item in stumps
                        for value in (
                            item.feature_index,
                            item.threshold,
                            item.left_value,
                            item.right_value,
                        )
                    ],
                ],
                "prediction_digest": array_digest(predictions),
                "chunk_count": len(ranges),
                "loss_reduction": "round_then_chunk_then_bin",
                "gradient_reduction": "round_then_chunk_then_row",
            }
        )

    residual = y - predictions
    residual_std = float(np.sqrt(np.mean(residual * residual)))
    model = StumpBoostingModel(
        base_prediction=base,
        learning_rate=learning_rate,
        stumps=stumps,
        residual_std=model_quantizer.quantize_scalar(residual_std),
        compensated_sum=compensated,
        subnormal_guard=guard,
        feature_count=int(X.shape[1]),
    )
    return model, rounds


def _compare_rounds(
    chunked: list[dict[str, Any]],
    full_batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for index, (left, right) in enumerate(zip(chunked, full_batch)):
        loss_delta = abs(float(left["loss"]) - float(right["loss"]))
        left_vector = np.asarray(left.get("parameter_vector", []), dtype=np.float64)
        right_vector = np.asarray(right.get("parameter_vector", []), dtype=np.float64)
        if left_vector.shape == right_vector.shape and left_vector.size:
            parameter_delta = left_vector - right_vector
            parameter_max = float(np.max(np.abs(parameter_delta), initial=0.0))
            parameter_l2 = float(np.sqrt(np.sum(parameter_delta * parameter_delta)))
        else:
            parameter_max = float("nan")
            parameter_l2 = float("nan")
        comparisons.append(
            {
                "round": index + 1,
                "chunked_loss": float(left["loss"]),
                "full_batch_loss": float(right["loss"]),
                "loss_absolute_deviation": loss_delta,
                "loss_relative_deviation": loss_delta
                / max(abs(float(right["loss"])), np.finfo(np.float64).tiny),
                "chunked_gradient_norm": float(left["gradient_norm"]),
                "full_batch_gradient_norm": float(right["gradient_norm"]),
                "parameter_max_abs_deviation": parameter_max,
                "parameter_l2_deviation": parameter_l2,
            }
        )
    return comparisons


def train_models(
    X: np.ndarray,
    y: np.ndarray,
    config: Mapping[str, Any],
    *,
    feature_quantizer: Quantizer,
    model_quantizer: Quantizer | None = None,
) -> TrainingResult:
    """Train chunked and whole-segment models on already prepared float64 data."""

    if model_quantizer is None:
        model_quantizer = Quantizer.identity()
    X_executed = feature_quantizer.quantize_array(X)
    y_executed = feature_quantizer.quantize_array(y)
    chunk_size = int(config["training"]["chunk_size"])
    training_config = config["training"]
    family = str(config["model"]["family"])

    if family == "linear_autoregressive":
        chunk_model, chunk_rounds = _train_linear_mode(
            X_executed,
            y_executed,
            config,
            chunk_size=chunk_size,
            model_quantizer=model_quantizer,
        )
        full_model, full_rounds = _train_linear_mode(
            X_executed,
            y_executed,
            config,
            chunk_size=int(X_executed.shape[0]),
            model_quantizer=model_quantizer,
        )
        pre_model, _ = _train_linear_mode(
            X_executed,
            y_executed,
            config,
            chunk_size=chunk_size,
            model_quantizer=Quantizer.identity(),
        )
    elif family == "stump_gradient_boosting":
        try:
            chunk_model, chunk_rounds = _train_stumps(
                X_executed,
                y_executed,
                config,
                chunk_size=chunk_size,
                model_quantizer=model_quantizer,
            )
            full_model, full_rounds = _train_stumps(
                X_executed,
                y_executed,
                config,
                chunk_size=int(X_executed.shape[0]),
                model_quantizer=model_quantizer,
            )
            pre_model, _ = _train_stumps(
                X_executed,
                y_executed,
                config,
                chunk_size=chunk_size,
                model_quantizer=Quantizer.identity(),
            )
        except ValueError as exc:
            raise ConfigInvalid("model.stump_min_leaf", str(exc)) from exc
    else:
        raise ValueError(f"unsupported model family: {family}")

    comparisons = _compare_rounds(chunk_rounds, full_rounds)
    return TrainingResult(
        model=chunk_model,
        full_batch_model=full_model,
        pre_model_quantization_model=pre_model,
        rounds=chunk_rounds,
        full_batch_rounds=full_rounds,
        comparisons=comparisons,
        sample_count=int(X.shape[0]),
        chunk_size=chunk_size,
        reduction_order=str(training_config["reduction_order"]),
        compensated_sum=bool(training_config["compensated_sum"]),
        subnormal_guard=bool(training_config["subnormal_guard"]),
    )
