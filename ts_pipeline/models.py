"""Serializable deterministic models sharing one prediction operator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from .precision import Quantizer
from .utils import NeumaierSum, array_digest, guard_subnormal, sha256_json


def _ordered_dot(
    values: Sequence[float],
    coefficients: Sequence[float],
    compensated: bool,
) -> float:
    if compensated:
        accumulator = NeumaierSum()
        for value, coefficient in zip(values, coefficients):
            accumulator.add(float(value) * float(coefficient))
        return accumulator.value
    total = 0.0
    for value, coefficient in zip(values, coefficients):
        total += float(value) * float(coefficient)
    return total


@dataclass
class LinearARModel:
    coefficients: np.ndarray
    intercept: float
    residual_std: float
    compensated_sum: bool = True
    subnormal_guard: bool = True
    feature_count: int = 0
    family: str = "linear_autoregressive"

    def __post_init__(self) -> None:
        self.coefficients = np.ascontiguousarray(
            self.coefficients,
            dtype=np.float64,
        )
        if not self.feature_count:
            self.feature_count = int(self.coefficients.size)

    def predict_one(self, features: np.ndarray) -> float:
        values = np.asarray(features, dtype=np.float64)
        result = _ordered_dot(
            values,
            self.coefficients,
            self.compensated_sum,
        ) + float(self.intercept)
        return guard_subnormal(result, self.subnormal_guard)

    def quantized(self, quantizer: Quantizer) -> "LinearARModel":
        return LinearARModel(
            coefficients=quantizer.quantize_array(self.coefficients),
            intercept=quantizer.quantize_scalar(self.intercept),
            residual_std=quantizer.quantize_scalar(self.residual_std),
            compensated_sum=self.compensated_sum,
            subnormal_guard=self.subnormal_guard,
            feature_count=self.feature_count,
        )

    def parameter_vector(self) -> np.ndarray:
        return np.ascontiguousarray(
            np.concatenate(
                [
                    self.coefficients,
                    np.asarray([self.intercept, self.residual_std], dtype=np.float64),
                ]
            ),
            dtype=np.float64,
        )

    def parameter_digest(self) -> str:
        return array_digest(self.parameter_vector())

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "coefficients": [float(value) for value in self.coefficients],
            "intercept": float(self.intercept),
            "residual_std": float(self.residual_std),
            "compensated_sum": self.compensated_sum,
            "subnormal_guard": self.subnormal_guard,
            "feature_count": self.feature_count,
            "parameter_digest": self.parameter_digest(),
        }


@dataclass
class Stump:
    feature_index: int
    threshold: float
    left_value: float
    right_value: float

    def predict(self, value: float) -> float:
        return self.left_value if float(value) <= self.threshold else self.right_value

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_index": int(self.feature_index),
            "threshold": float(self.threshold),
            "left_value": float(self.left_value),
            "right_value": float(self.right_value),
        }


@dataclass
class StumpBoostingModel:
    base_prediction: float
    learning_rate: float
    stumps: list[Stump] = field(default_factory=list)
    residual_std: float = 0.0
    compensated_sum: bool = True
    subnormal_guard: bool = True
    feature_count: int = 0
    family: str = "stump_gradient_boosting"

    def predict_one(self, features: np.ndarray) -> float:
        values = np.asarray(features, dtype=np.float64)
        total = float(self.base_prediction)
        for stump in self.stumps:
            total += float(self.learning_rate) * stump.predict(
                float(values[stump.feature_index])
            )
            total = guard_subnormal(total, self.subnormal_guard)
        return total

    def quantized(self, quantizer: Quantizer) -> "StumpBoostingModel":
        return StumpBoostingModel(
            base_prediction=quantizer.quantize_scalar(self.base_prediction),
            learning_rate=quantizer.quantize_scalar(self.learning_rate),
            stumps=[
                Stump(
                    feature_index=stump.feature_index,
                    threshold=quantizer.quantize_scalar(stump.threshold),
                    left_value=quantizer.quantize_scalar(stump.left_value),
                    right_value=quantizer.quantize_scalar(stump.right_value),
                )
                for stump in self.stumps
            ],
            residual_std=quantizer.quantize_scalar(self.residual_std),
            compensated_sum=self.compensated_sum,
            subnormal_guard=self.subnormal_guard,
            feature_count=self.feature_count,
        )

    def parameter_vector(self) -> np.ndarray:
        values = [float(self.base_prediction), float(self.learning_rate)]
        for stump in self.stumps:
            values.extend(
                [
                    float(stump.feature_index),
                    float(stump.threshold),
                    float(stump.left_value),
                    float(stump.right_value),
                ]
            )
        values.append(float(self.residual_std))
        return np.ascontiguousarray(values, dtype=np.float64)

    def parameter_digest(self) -> str:
        return array_digest(self.parameter_vector())

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "base_prediction": float(self.base_prediction),
            "learning_rate": float(self.learning_rate),
            "stumps": [stump.to_dict() for stump in self.stumps],
            "residual_std": float(self.residual_std),
            "compensated_sum": self.compensated_sum,
            "subnormal_guard": self.subnormal_guard,
            "feature_count": self.feature_count,
            "parameter_digest": self.parameter_digest(),
        }


Model = LinearARModel | StumpBoostingModel


def model_from_dict(payload: dict[str, Any]) -> Model:
    family = payload.get("family")
    if family == "linear_autoregressive":
        return LinearARModel(
            coefficients=np.asarray(payload["coefficients"], dtype=np.float64),
            intercept=float(payload["intercept"]),
            residual_std=float(payload["residual_std"]),
            compensated_sum=bool(payload.get("compensated_sum", True)),
            subnormal_guard=bool(payload.get("subnormal_guard", True)),
            feature_count=int(payload.get("feature_count", 0)),
        )
    if family == "stump_gradient_boosting":
        return StumpBoostingModel(
            base_prediction=float(payload["base_prediction"]),
            learning_rate=float(payload["learning_rate"]),
            stumps=[
                Stump(
                    feature_index=int(item["feature_index"]),
                    threshold=float(item["threshold"]),
                    left_value=float(item["left_value"]),
                    right_value=float(item["right_value"]),
                )
                for item in payload.get("stumps", [])
            ],
            residual_std=float(payload["residual_std"]),
            compensated_sum=bool(payload.get("compensated_sum", True)),
            subnormal_guard=bool(payload.get("subnormal_guard", True)),
            feature_count=int(payload.get("feature_count", 0)),
        )
    raise ValueError(f"unknown model family: {family!r}")


def model_parameter_deviation(left: Model, right: Model) -> dict[str, float]:
    left_vector = left.parameter_vector()
    right_vector = right.parameter_vector()
    if left_vector.shape != right_vector.shape:
        return {
            "parameter_max_abs_deviation": float("nan"),
            "parameter_l2_deviation": float("nan"),
        }
    delta = left_vector - right_vector
    return {
        "parameter_max_abs_deviation": float(np.max(np.abs(delta), initial=0.0)),
        "parameter_l2_deviation": float(np.sqrt(np.sum(delta * delta))),
    }


def model_parameter_sha256(model: Model) -> str:
    return sha256_json(
        {
            "family": model.family,
            "parameters": [float(value) for value in model.parameter_vector()],
        }
    )
