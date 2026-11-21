"""Precision execution layer with shared operator semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .errors import E_CONFIG_INVALID, E_RESOURCE_LIMIT, BusinessError


@dataclass(frozen=True)
class PrecisionSpec:
    mode: str
    fixed_scale: int = 1024
    rounding: str = "half_even"

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "PrecisionSpec":
        precision = config["precision"]
        return cls(
            mode=str(precision["mode"]),
            fixed_scale=int(precision["fixed_scale"]),
            rounding=str(precision["rounding"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fixed_scale": self.fixed_scale,
            "rounding": self.rounding,
        }

    @property
    def storage_dtype(self) -> np.dtype:
        if self.mode == "float64":
            return np.dtype("<f8")
        if self.mode == "float32":
            return np.dtype("<f4")
        if self.mode == "fixed":
            return np.dtype("<i8")
        raise BusinessError(
            E_CONFIG_INVALID,
            "unsupported precision mode",
            {"field": "precision.mode", "value": self.mode},
        )

    @property
    def bytes_per_element(self) -> int:
        return int(self.storage_dtype.itemsize)


class PrecisionOps:
    """Operations whose storage and rounding are explicitly precision-bound."""

    def __init__(self, spec: PrecisionSpec):
        self.spec = spec

    @property
    def mode(self) -> str:
        return self.spec.mode

    @property
    def storage_dtype(self) -> np.dtype:
        return self.spec.storage_dtype

    def _round_values(self, values: np.ndarray) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        if not np.all(np.isfinite(array)):
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "fixed-point conversion produced a non-finite value",
                {"reason": "precision_overflow"},
            )
        if self.spec.rounding == "half_even":
            rounded = np.rint(array)
        elif self.spec.rounding == "half_away_from_zero":
            rounded = np.where(array >= 0.0, np.floor(array + 0.5), np.ceil(array - 0.5))
        elif self.spec.rounding == "truncate":
            rounded = np.trunc(array)
        else:
            raise BusinessError(
                E_CONFIG_INVALID,
                "unsupported fixed-point rounding mode",
                {"field": "precision.rounding", "value": self.spec.rounding},
            )
        limit = np.iinfo(np.int64).max
        if np.any(np.abs(rounded) > limit):
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "fixed-point value exceeds int64 range",
                {"reason": "precision_overflow", "limit": int(limit)},
            )
        return rounded.astype(np.int64)

    def quantize(self, values: np.ndarray) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        return self._round_values(array * float(self.spec.fixed_scale))

    def cast(self, values: np.ndarray) -> np.ndarray:
        array = np.asarray(values)
        if self.mode == "fixed":
            return self.quantize(array)
        return np.array(array, dtype=self.storage_dtype, copy=True)

    def to_float(self, values: np.ndarray, dtype: np.dtype | str = np.float64) -> np.ndarray:
        array = np.asarray(values)
        if self.mode == "fixed":
            return array.astype(np.float64) / float(self.spec.fixed_scale)
        return array.astype(dtype)

    def numeric_stats(
        self, numeric: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return stored mean, stored scale, centered numeric, constant mask."""

        source = self.cast(numeric)
        if self.mode == "fixed":
            scale_factor = float(self.spec.fixed_scale)
            mean_float = np.mean(source.astype(np.float64), axis=0) / scale_factor
            std_float = np.std(
                source.astype(np.float64) / scale_factor, axis=0, ddof=0
            )
            mean = self.quantize(mean_float)
            scale = self.quantize(std_float)
            constant = std_float <= 0.0
            scale = np.where(scale == 0, int(self.spec.fixed_scale), scale).astype(
                np.int64
            )
        else:
            mean = np.mean(source, axis=0, dtype=self.storage_dtype)
            scale = np.std(source, axis=0, dtype=self.storage_dtype, ddof=0)
            constant = np.asarray(scale) == 0
            scale = np.where(scale == 0, 1.0, scale).astype(self.storage_dtype)
        return mean, scale, source, constant

    def standardize_numeric(self, numeric: np.ndarray) -> dict[str, np.ndarray]:
        mean, scale, source, constant = self.numeric_stats(numeric)
        if self.mode == "fixed":
            scale_factor = float(self.spec.fixed_scale)
            centered = source.astype(np.int64) - mean.astype(np.int64)
            standardized = self._round_values(
                centered.astype(np.float64) * scale_factor / scale.astype(np.float64)
            )
        else:
            standardized = ((source - mean) / scale).astype(self.storage_dtype)
        return {
            "mean": mean,
            "scale": scale,
            "source": source,
            "standardized": standardized,
            "constant": constant,
        }

    def center_columns(
        self, values: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        source = np.asarray(values)
        if self.mode == "fixed":
            mean = self._round_values(np.mean(source.astype(np.float64), axis=0))
            centered = source.astype(np.int64) - mean.astype(np.int64)
            return centered.astype(np.int64), mean.astype(np.int64)
        mean = np.mean(source, axis=0, dtype=self.storage_dtype)
        centered = (source - mean).astype(self.storage_dtype)
        return centered, mean.astype(self.storage_dtype)

    def subtract_center(self, values: np.ndarray, center: np.ndarray) -> np.ndarray:
        source = np.asarray(values)
        centered = np.asarray(center)
        if self.mode == "fixed":
            return (
                source.astype(np.int64) - centered.astype(np.int64)
            ).astype(np.int64)
        return (source - centered).astype(self.storage_dtype)

    def matmul(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        left_array = np.asarray(left)
        right_array = np.asarray(right)
        if left_array.shape[1] != right_array.shape[0]:
            raise ValueError(
                f"matmul shape mismatch: {left_array.shape} @ {right_array.shape}"
            )
        if self.mode == "fixed":
            left_int = left_array.astype(np.int64)
            right_int = right_array.astype(np.int64)
            inner = int(left_array.shape[1])
            max_left = int(np.max(np.abs(left_int))) if left_int.size else 0
            max_right = int(np.max(np.abs(right_int))) if right_int.size else 0
            if max_left and max_right and max_left * max_right * inner > np.iinfo(np.int64).max:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "fixed-point matmul would overflow int64",
                    {
                        "reason": "precision_overflow",
                        "inner_dimension": inner,
                        "max_left": max_left,
                        "max_right": max_right,
                    },
                )
            products = np.matmul(left_int, right_int)
            return self._round_values(
                products.astype(np.float64) / float(self.spec.fixed_scale)
            )
        return np.matmul(left_array, right_array)

    def concatenate_features(
        self, numeric: np.ndarray, categorical_one_hot: np.ndarray
    ) -> np.ndarray:
        if numeric.shape[0] != categorical_one_hot.shape[0]:
            raise ValueError("numeric and categorical row counts differ")
        combined = np.concatenate([numeric, categorical_one_hot], axis=1)
        return combined.astype(self.storage_dtype)

    def one_hot(
        self,
        categorical_values: np.ndarray,
        categorical_columns: list[str],
        category_levels: Mapping[str, list[str]],
    ) -> tuple[np.ndarray, list[str]]:
        blocks: list[np.ndarray] = []
        feature_names: list[str] = []
        row_count = int(categorical_values.shape[0])
        one_value = (
            int(self.spec.fixed_scale) if self.mode == "fixed" else 1
        )
        for column_index, column in enumerate(categorical_columns):
            levels = list(category_levels[column])
            lookup = {level: index for index, level in enumerate(levels)}
            block = np.zeros((row_count, len(levels)), dtype=self.storage_dtype)
            for row_index, value in enumerate(categorical_values[:, column_index]):
                key = str(value)
                if key not in lookup:
                    raise BusinessError(
                        E_RESOURCE_LIMIT,
                        "category value is absent from the frozen encoding map",
                        {"field": f"category_levels.{column}", "value": key},
                    )
                block[row_index, lookup[key]] = one_value
            blocks.append(block)
            feature_names.extend(f"{column}={level}" for level in levels)
        if blocks:
            return np.concatenate(blocks, axis=1), feature_names
        return np.zeros((row_count, 0), dtype=self.storage_dtype), feature_names
