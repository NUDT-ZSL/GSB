"""Training configuration validation and normalized representation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .errors import config_error

PRECISIONS = ("float64", "float32", "fixed_point")
ROUNDING_MODES = (
    "nearest_even",
    "half_away_from_zero",
    "toward_zero",
    "floor",
    "ceil",
)
MODEL_FAMILIES = ("logistic_regression", "stump_ensemble")
STANDARDIZATIONS = ("zscore", "minmax")
CONVERGENCE_STRATEGIES = ("strict", "tolerance_band")


@dataclass(frozen=True)
class TrainingConfig:
    job_id: str
    model_family: str = "logistic_regression"
    regularization_strength: float = 0.01
    max_iterations: int = 30
    learning_rate: float = 0.2
    convergence_tolerance: float = 1e-7
    min_improvement: float = 1e-9
    convergence_strategy: str = "tolerance_band"
    standardization: str = "zscore"
    precision: str = "float64"
    fixed_scale: int = 65536
    rounding_mode: str = "nearest_even"
    chunk_size: int = 32
    consistency_tolerance: float = 1e-6
    validation_fraction: float = 0.25
    subnormal_floor: float = 1e-300
    tolerance_band_factor: float = 1.0
    numeric_columns: tuple[str, ...] = ("num_0", "num_1", "num_2")
    categorical_columns: tuple[str, ...] = ("cat_0", "cat_1")
    label_column: str = "label"
    positive_label: str = "yes"
    reference_precision: str = "float64"

    MAX_REGULARIZATION: ClassVar[float] = 10.0
    MAX_LEARNING_RATE: ClassVar[float] = 10.0
    MAX_FIXED_SCALE: ClassVar[int] = 1 << 24
    MAX_ITERATIONS: ClassVar[int] = 10000

    @classmethod
    def from_dict(cls, raw: Any) -> "TrainingConfig":
        if not isinstance(raw, dict):
            raise config_error("$", "configuration must be a JSON object")

        def take(name: str, default: Any = None, required: bool = False) -> Any:
            if name in raw:
                return raw[name]
            if required:
                raise config_error(name, f"missing required field {name!r}")
            return default

        numeric_columns = take("numeric_columns", ["num_0", "num_1", "num_2"])
        categorical_columns = take("categorical_columns", ["cat_0", "cat_1"])
        if isinstance(numeric_columns, list):
            numeric_columns = tuple(numeric_columns)
        if isinstance(categorical_columns, list):
            categorical_columns = tuple(categorical_columns)

        config = cls(
            job_id=take("job_id", required=True),
            model_family=take("model_family", "logistic_regression"),
            regularization_strength=take("regularization_strength", 0.01),
            max_iterations=take("max_iterations", 30),
            learning_rate=take("learning_rate", 0.2),
            convergence_tolerance=take("convergence_tolerance", 1e-7),
            min_improvement=take("min_improvement", 1e-9),
            convergence_strategy=take("convergence_strategy", "tolerance_band"),
            standardization=take("standardization", "zscore"),
            precision=take("precision", "float64"),
            fixed_scale=take("fixed_scale", 65536),
            rounding_mode=take("rounding_mode", "nearest_even"),
            chunk_size=take("chunk_size", 32),
            consistency_tolerance=take("consistency_tolerance", 1e-6),
            validation_fraction=take("validation_fraction", 0.25),
            subnormal_floor=take("subnormal_floor", 1e-300),
            tolerance_band_factor=take("tolerance_band_factor", 1.0),
            numeric_columns=numeric_columns,
            categorical_columns=categorical_columns,
            label_column=take("label_column", "label"),
            positive_label=take("positive_label", "yes"),
            reference_precision=take("reference_precision", "float64"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise config_error("job_id", "job_id must be a non-empty string")
        if self.model_family not in MODEL_FAMILIES:
            raise config_error(
                "model_family",
                f"model_family must be one of {list(MODEL_FAMILIES)}",
                self.model_family,
            )
        self._finite_range(
            "regularization_strength",
            self.regularization_strength,
            0.0,
            self.MAX_REGULARIZATION,
        )
        if (
            isinstance(self.max_iterations, bool)
            or not isinstance(self.max_iterations, int)
            or self.max_iterations < 1
            or self.max_iterations > self.MAX_ITERATIONS
        ):
            raise config_error(
                "max_iterations",
                f"max_iterations must be an integer in [1, {self.MAX_ITERATIONS}]",
                self.max_iterations,
            )
        self._finite_range(
            "learning_rate", self.learning_rate, 0.0, self.MAX_LEARNING_RATE, strict_min=True
        )
        self._positive("convergence_tolerance", self.convergence_tolerance)
        self._nonnegative("min_improvement", self.min_improvement)
        if self.convergence_strategy not in CONVERGENCE_STRATEGIES:
            raise config_error(
                "convergence_strategy",
                f"convergence_strategy must be one of {list(CONVERGENCE_STRATEGIES)}",
                self.convergence_strategy,
            )
        if self.standardization not in STANDARDIZATIONS:
            raise config_error(
                "standardization",
                f"standardization must be one of {list(STANDARDIZATIONS)}",
                self.standardization,
            )
        if self.precision not in PRECISIONS:
            raise config_error(
                "precision", f"precision must be one of {list(PRECISIONS)}", self.precision
            )
        if (
            isinstance(self.fixed_scale, bool)
            or not isinstance(self.fixed_scale, int)
            or self.fixed_scale <= 0
        ):
            raise config_error(
                "fixed_scale", "fixed_scale must be a positive integer", self.fixed_scale
            )
        if self.fixed_scale > self.MAX_FIXED_SCALE:
            raise config_error(
                "fixed_scale",
                f"fixed_scale must not exceed {self.MAX_FIXED_SCALE}",
                self.fixed_scale,
            )
        if self.rounding_mode not in ROUNDING_MODES:
            raise config_error(
                "rounding_mode",
                f"rounding_mode must be one of {list(ROUNDING_MODES)}",
                self.rounding_mode,
            )
        if (
            isinstance(self.chunk_size, bool)
            or not isinstance(self.chunk_size, int)
            or self.chunk_size <= 0
        ):
            raise config_error("chunk_size", "chunk_size must be a positive integer", self.chunk_size)
        self._nonnegative("consistency_tolerance", self.consistency_tolerance)
        if not self._is_finite(self.validation_fraction) or not 0 < self.validation_fraction < 1:
            raise config_error(
                "validation_fraction",
                "validation_fraction must be strictly between 0 and 1",
                self.validation_fraction,
            )
        self._nonnegative("subnormal_floor", self.subnormal_floor)
        self._positive("tolerance_band_factor", self.tolerance_band_factor)
        if self.reference_precision != "float64":
            raise config_error(
                "reference_precision",
                "the reference precision must be float64",
                self.reference_precision,
            )
        if not isinstance(self.label_column, str) or not self.label_column:
            raise config_error("label_column", "label_column must be a non-empty string")
        if not isinstance(self.positive_label, str) or not self.positive_label:
            raise config_error("positive_label", "positive_label must be a non-empty string")
        self._validate_columns()

    def _validate_columns(self) -> None:
        for field_name, columns in (
            ("numeric_columns", self.numeric_columns),
            ("categorical_columns", self.categorical_columns),
        ):
            if not isinstance(columns, tuple) or not columns:
                raise config_error(field_name, f"{field_name} must be a non-empty list")
            if any(not isinstance(item, str) or not item for item in columns):
                raise config_error(field_name, f"{field_name} entries must be non-empty strings")
            if len(set(columns)) != len(columns):
                raise config_error(field_name, f"{field_name} must not contain duplicates")
        overlap = set(self.numeric_columns) & set(self.categorical_columns)
        if overlap:
            raise config_error(
                "columns", "numeric_columns and categorical_columns must be disjoint", sorted(overlap)
            )
        if self.label_column in self.numeric_columns or self.label_column in self.categorical_columns:
            raise config_error(
                "label_column", "label_column must not also be a feature column", self.label_column
            )

    def _positive(self, field_name: str, value: Any) -> None:
        if not self._is_finite(value) or value <= 0:
            raise config_error(field_name, f"{field_name} must be a positive finite number", value)

    def _nonnegative(self, field_name: str, value: Any) -> None:
        if not self._is_finite(value) or value < 0:
            raise config_error(
                field_name, f"{field_name} must be a non-negative finite number", value
            )

    def _finite_range(
        self,
        field_name: str,
        value: Any,
        lower: float,
        upper: float,
        *,
        strict_min: bool = False,
    ) -> None:
        if not self._is_finite(value):
            raise config_error(field_name, f"{field_name} must be finite", value)
        lower_ok = value > lower if strict_min else value >= lower
        if not lower_ok or value > upper:
            interval = f"({lower}, {upper}]" if strict_min else f"[{lower}, {upper}]"
            raise config_error(field_name, f"{field_name} must be in {interval}", value)

    @staticmethod
    def _is_finite(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "model_family": self.model_family,
            "regularization_strength": self.regularization_strength,
            "max_iterations": self.max_iterations,
            "learning_rate": self.learning_rate,
            "convergence_tolerance": self.convergence_tolerance,
            "min_improvement": self.min_improvement,
            "convergence_strategy": self.convergence_strategy,
            "standardization": self.standardization,
            "precision": self.precision,
            "fixed_scale": self.fixed_scale,
            "rounding_mode": self.rounding_mode,
            "chunk_size": self.chunk_size,
            "consistency_tolerance": self.consistency_tolerance,
            "validation_fraction": self.validation_fraction,
            "subnormal_floor": self.subnormal_floor,
            "tolerance_band_factor": self.tolerance_band_factor,
            "numeric_columns": list(self.numeric_columns),
            "categorical_columns": list(self.categorical_columns),
            "label_column": self.label_column,
            "positive_label": self.positive_label,
            "reference_precision": self.reference_precision,
        }

    def replace(self, **changes: Any) -> "TrainingConfig":
        payload = self.to_dict()
        payload.update(changes)
        return TrainingConfig.from_dict(payload)

    @property
    def feature_column_count(self) -> int:
        return len(self.numeric_columns) + len(self.categorical_columns) + 1
