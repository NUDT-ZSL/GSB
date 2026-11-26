from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import EngineConfig
from .errors import DiagnosticError


@dataclass(frozen=True)
class PrecisionOps:
    name: str
    dtype: Any
    fixed_scale: int | None = None
    rounding_mode: str = "nearest_even"

    @classmethod
    def from_config(cls, config: EngineConfig, precision: str | None = None) -> "PrecisionOps":
        selected = (precision or config.precision).lower()
        if selected == "float64":
            return cls(name=selected, dtype=np.dtype(np.float64))
        if selected == "float32":
            return cls(name=selected, dtype=np.dtype(np.float32))
        if selected == "fixed":
            if config.fixed_scale <= 0:
                raise DiagnosticError(
                    "E_FIXED_SCALE_INVALID",
                    "config",
                    "fixed_scale must be a positive integer",
                )
            return cls(
                name=selected,
                dtype=np.dtype(np.float64),
                fixed_scale=config.fixed_scale,
                rounding_mode=config.rounding_mode,
            )
        raise DiagnosticError(
            "E_CONFIG_INVALID", "config", f"unknown precision {selected!r}"
        )

    @property
    def is_fixed(self) -> bool:
        return self.fixed_scale is not None

    @property
    def quantum(self) -> float:
        if self.fixed_scale is None:
            return float(np.finfo(self.dtype).eps)
        return 1.0 / float(self.fixed_scale)

    @property
    def storage_dtype(self) -> str:
        return "int64" if self.is_fixed else str(self.dtype)

    def round_scaled(self, values: Any) -> np.ndarray:
        if self.fixed_scale is None:
            return np.asarray(values, dtype=self.dtype)
        scaled = np.asarray(values, dtype=np.float64) * float(self.fixed_scale)
        if self.rounding_mode == "nearest_even":
            rounded = np.rint(scaled)
        elif self.rounding_mode == "floor":
            rounded = np.floor(scaled)
        elif self.rounding_mode == "ceil":
            rounded = np.ceil(scaled)
        elif self.rounding_mode == "trunc":
            rounded = np.trunc(scaled)
        else:
            raise DiagnosticError(
                "E_CONFIG_INVALID",
                "config",
                f"unknown rounding mode {self.rounding_mode!r}",
            )
        return rounded

    def quantize_int(self, values: Any) -> np.ndarray:
        return np.asarray(self.round_scaled(values), dtype=np.int64)

    def quantize(self, values: Any) -> np.ndarray:
        if self.fixed_scale is None:
            return np.asarray(values, dtype=self.dtype)
        integers = self.quantize_int(values)
        return integers.astype(np.float64) / float(self.fixed_scale)

    def cast_scalar(self, value: float) -> float:
        if self.is_fixed:
            return float(self.quantize(np.asarray(value)).reshape(()))
        return float(np.asarray(value, dtype=self.dtype))

    def prepare_features(
        self, matrix: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray | None]:
        if self.is_fixed:
            integers = self.quantize_int(matrix)
            return (
                integers.astype(np.float64) / float(self.fixed_scale),
                integers,
            )
        return np.asarray(matrix, dtype=self.dtype), None

    def matmul(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        result = np.matmul(left, right)
        return self.quantize(result)

    def sum(self, values: np.ndarray, axis: int | None = None) -> np.ndarray | float:
        result = np.sum(values, axis=axis, dtype=np.float64 if self.is_fixed else self.dtype)
        return self.quantize(np.asarray(result))

    def sigmoid(self, logits: np.ndarray) -> np.ndarray:
        z = np.asarray(logits, dtype=np.float64)
        positive = z >= 0
        result = np.empty_like(z)
        result[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
        exp_z = np.exp(z[~positive])
        result[~positive] = exp_z / (1.0 + exp_z)
        return self.quantize(result)

    def to_dict(self) -> dict[str, Any]:
        return {
            "precision": self.name,
            "storage_dtype": self.storage_dtype,
            "fixed_scale": self.fixed_scale,
            "rounding_mode": self.rounding_mode if self.is_fixed else None,
            "quantum": self.quantum,
        }

