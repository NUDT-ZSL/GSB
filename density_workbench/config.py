from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .canonical import sha256_obj
from .errors import E_CONFIG_INVALID, E_PRECISION_UNKNOWN, BusinessError


KNOWN_PRECISIONS = ("float64", "float32", "fixed")
KNOWN_FAMILIES = ("gmm", "kde")
KNOWN_NORMALIZATIONS = ("zscore", "minmax")
KNOWN_COVARIANCE_TYPES = ("full", "diag", "spherical")
KNOWN_CONVERGENCE_POLICIES = ("strict", "tolerance_band")
KNOWN_ROUNDING_MODES = (
    "round_half_even",
    "round_half_away_from_zero",
    "floor",
    "ceil",
    "trunc",
)


@dataclass
class WorkbenchConfig:
    model_family: str = "gmm"
    n_components: int = 3
    bandwidth: float = 0.65
    covariance_type: str = "diag"
    max_iter: int = 30
    tol: float = 1e-7
    min_improvement: float = 1e-10
    convergence_policy: str = "tolerance_band"
    convergence_rel_band: float = 1e-8
    convergence_abs_band: float = 1e-12
    normalization: str = "zscore"
    chunk_size: int = 8
    precision: str = "float64"
    fixed_scale: int = 1_000_000
    fixed_rounding: str = "round_half_even"
    seed: int = 20260101
    variance_floor: float = 1e-12
    covariance_floor: float = 1e-8
    subnormal_floor: float = 0.0
    latency_budget_ms: float = 1_000.0
    max_responsibility_memory_mb: float = 64.0
    max_batch_samples: int = 10_000
    max_parallel_queries: int = 4
    consistency_tolerance: float | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.model_family not in KNOWN_FAMILIES:
            self._invalid("model_family", f"must be one of {KNOWN_FAMILIES}")
        if self.precision not in KNOWN_PRECISIONS:
            raise BusinessError(
                code=E_PRECISION_UNKNOWN,
                message=f"unknown precision mode: {self.precision}",
                details={
                    "field": "precision",
                    "value": self.precision,
                    "allowed": list(KNOWN_PRECISIONS),
                },
            )
        if self.normalization not in KNOWN_NORMALIZATIONS:
            self._invalid("normalization", f"must be one of {KNOWN_NORMALIZATIONS}")
        if self.covariance_type not in KNOWN_COVARIANCE_TYPES:
            self._invalid(
                "covariance_type", f"must be one of {KNOWN_COVARIANCE_TYPES}"
            )
        if self.convergence_policy not in KNOWN_CONVERGENCE_POLICIES:
            self._invalid(
                "convergence_policy",
                f"must be one of {KNOWN_CONVERGENCE_POLICIES}",
            )
        if isinstance(self.n_components, bool) or not isinstance(self.n_components, int):
            self._invalid("n_components", "must be a positive integer")
        if self.n_components <= 0:
            self._invalid("n_components", "must be a positive integer")
        if not _positive_finite(self.bandwidth):
            self._invalid("bandwidth", "must be a positive finite number")
        if isinstance(self.max_iter, bool) or not isinstance(self.max_iter, int):
            self._invalid("max_iter", "must be a positive integer")
        if self.max_iter <= 0:
            self._invalid("max_iter", "must be a positive integer")
        if not _positive_finite(self.tol):
            self._invalid("tol", "must be a positive finite number")
        if not _nonnegative_finite(self.min_improvement):
            self._invalid("min_improvement", "must be a non-negative finite number")
        if not _nonnegative_finite(self.convergence_rel_band):
            self._invalid(
                "convergence_rel_band", "must be a non-negative finite number"
            )
        if not _nonnegative_finite(self.convergence_abs_band):
            self._invalid(
                "convergence_abs_band", "must be a non-negative finite number"
            )
        if isinstance(self.chunk_size, bool) or not isinstance(self.chunk_size, int):
            self._invalid("chunk_size", "must be a positive integer")
        if self.chunk_size <= 0:
            self._invalid("chunk_size", "must be a positive integer")
        if isinstance(self.fixed_scale, bool) or not isinstance(self.fixed_scale, int):
            self._invalid("fixed_scale", "must be a positive integer")
        if self.fixed_scale <= 0:
            self._invalid("fixed_scale", "must be a positive integer")
        if self.fixed_rounding not in KNOWN_ROUNDING_MODES:
            self._invalid(
                "fixed_rounding", f"must be one of {KNOWN_ROUNDING_MODES}"
            )
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            self._invalid("seed", "must be an integer")
        if not _positive_finite(self.variance_floor):
            self._invalid("variance_floor", "must be a positive finite number")
        if not _positive_finite(self.covariance_floor):
            self._invalid("covariance_floor", "must be a positive finite number")
        if not _nonnegative_finite(self.subnormal_floor):
            self._invalid("subnormal_floor", "must be a non-negative finite number")
        if not _positive_finite(self.latency_budget_ms):
            self._invalid("latency_budget_ms", "must be a positive finite number")
        if not _positive_finite(self.max_responsibility_memory_mb):
            self._invalid(
                "max_responsibility_memory_mb", "must be a positive finite number"
            )
        if (
            isinstance(self.max_batch_samples, bool)
            or not isinstance(self.max_batch_samples, int)
        ):
            self._invalid("max_batch_samples", "must be a positive integer")
        if self.max_batch_samples <= 0:
            self._invalid("max_batch_samples", "must be a positive integer")
        if (
            isinstance(self.max_parallel_queries, bool)
            or not isinstance(self.max_parallel_queries, int)
        ):
            self._invalid("max_parallel_queries", "must be a positive integer")
        if self.max_parallel_queries <= 0:
            self._invalid("max_parallel_queries", "must be a positive integer")
        if self.consistency_tolerance is not None and not _positive_finite(
            self.consistency_tolerance
        ):
            self._invalid(
                "consistency_tolerance", "must be a positive finite number"
            )

    def _invalid(self, field_name: str, reason: str) -> None:
        raise BusinessError(
            code=E_CONFIG_INVALID,
            message=f"invalid config field {field_name}: {reason}",
            details={"field": field_name, "reason": reason},
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkbenchConfig":
        if not isinstance(data, dict):
            raise BusinessError(
                code=E_CONFIG_INVALID,
                message="config must be a JSON object",
                details={"field": "$"},
            )
        aliases = {
            "model": "model_family",
            "family": "model_family",
            "components": "n_components",
            "n_components": "n_components",
            "covariance": "covariance_type",
            "tolerance": "tol",
            "init_seed": "seed",
            "scaler": "normalization",
            "scale_factor": "fixed_scale",
            "rounding_mode": "fixed_rounding",
            "batch_size": "chunk_size",
        }
        value_aliases = {
            "model_family": {
                "gaussian_mixture": "gmm",
                "gmm": "gmm",
                "kernel_density": "kde",
                "kde": "kde",
            },
            "precision": {
                "float64": "float64",
                "double": "float64",
                "float32": "float32",
                "single": "float32",
                "fixed": "fixed",
                "fixed_point": "fixed",
                "scaled_int": "fixed",
                "scaled_integer": "fixed",
                "int": "fixed",
            },
            "normalization": {
                "zscore": "zscore",
                "z-score": "zscore",
                "z_score": "zscore",
                "minmax": "minmax",
                "min-max": "minmax",
                "min_max": "minmax",
            },
            "fixed_rounding": {
                "round_half_even": "round_half_even",
                "half_even": "round_half_even",
                "nearest": "round_half_even",
                "round_half_away_from_zero": "round_half_away_from_zero",
                "half_away_from_zero": "round_half_away_from_zero",
                "floor": "floor",
                "ceil": "ceil",
                "trunc": "trunc",
            },
        }
        values: dict[str, Any] = {}
        for key, value in data.items():
            target = aliases.get(key, key)
            if target in cls.__dataclass_fields__:
                if target in value_aliases and isinstance(value, str):
                    value = value_aliases[target].get(value.lower(), value)
                values[target] = value
        for int_field in (
            "n_components",
            "max_iter",
            "chunk_size",
            "fixed_scale",
            "seed",
            "max_batch_samples",
            "max_parallel_queries",
        ):
            value = values.get(int_field)
            if isinstance(value, float) and value.is_integer():
                values[int_field] = int(value)
        return cls(**values)

    @classmethod
    def from_json_file(cls, path: str) -> "WorkbenchConfig":
        import json
        from pathlib import Path

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BusinessError(
                code=E_CONFIG_INVALID,
                message=f"cannot read config: {exc}",
                details={"field": "config_path", "path": str(path)},
            ) from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def semantic_dict(self) -> dict[str, Any]:
        """Configuration that changes numerical outputs or cache identity."""

        return {
            "model_family": self.model_family,
            "n_components": self.n_components,
            "bandwidth": self.bandwidth,
            "covariance_type": self.covariance_type,
            "max_iter": self.max_iter,
            "tol": self.tol,
            "min_improvement": self.min_improvement,
            "convergence_policy": self.convergence_policy,
            "convergence_rel_band": self.convergence_rel_band,
            "convergence_abs_band": self.convergence_abs_band,
            "normalization": self.normalization,
            "chunk_size": self.chunk_size,
            "precision": self.precision,
            "fixed_scale": self.fixed_scale,
            "fixed_rounding": self.fixed_rounding,
            "seed": self.seed,
            "variance_floor": self.variance_floor,
            "covariance_floor": self.covariance_floor,
            "subnormal_floor": self.subnormal_floor,
            "consistency_tolerance": self.consistency_tolerance,
        }

    def semantic_hash(self) -> str:
        return sha256_obj(self.semantic_dict())

    def with_precision(self, precision: str) -> "WorkbenchConfig":
        return replace(self, precision=precision)

    def declared_consistency_tolerance(self) -> float:
        if self.consistency_tolerance is not None:
            return float(self.consistency_tolerance)
        if self.precision == "float64":
            return 1e-12
        if self.precision == "float32":
            return 2e-5
        return max(4.0 / float(self.fixed_scale), 1e-12)


def _positive_finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _nonnegative_finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )
