"""Training configuration and deterministic RNG stream layout.

All tunable parameters live in :class:`TrainConfig`.  Randomness is never
sourced from a global RNG: :class:`TrainConfig` derives independent,
explicitly named :class:`numpy.random.Generator` instances from the fixed
seed so every sampling sequence is reproducible byte-for-byte.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Tuple

import numpy as np

from .errors import DomainError, ErrorCode


VALID_PRECISION = ("float64", "float32")
VALID_ROUNDING = ("half_even", "half_up", "half_down", "trunc")
VALID_GAIN_STRATEGY = ("strict", "tolerance")


@dataclass(frozen=True)
class TrainConfig:
    """Configuration of a GBT regression run.

    Parameters
    ----------
    n_trees:
        Number of boosting rounds (one tree per round).
    max_depth:
        Maximum tree depth, root is depth ``0``.
    learning_rate:
        Shrinkage applied to every tree contribution.
    subsample:
        Row sampling fraction in ``(0, 1]``.
    colsample:
        Column sampling fraction in ``(0, 1]``.
    min_leaf_samples:
        Minimum number of rows required in a leaf.
    n_thresholds:
        Maximum number of quantile split candidates per numeric feature.
    precision:
        ``"float64"`` keeps full precision arithmetic; ``"float32"``
        simulates a reduced precision pipeline at the documented stages.
    seed:
        Master random seed driving every derived RNG stream.
    min_gain:
        Minimum gain required for a split to be accepted.
    gain_strategy:
        ``"strict"`` uses exact comparison against ``min_gain``;
        ``"tolerance"`` accepts candidates whose gap is within
        ``gain_tol`` of the best candidate.
    gain_tol:
        Absolute tolerance band used by the tolerance strategy.
    hessian_floor:
        Lower bound applied to leaf second-derivative sums.
    hessian_floor_default:
        Fallback floor used by the degradation path when an injected leaf
        stage fault is detected.
    rounding:
        Rounding mode used when ``precision == "float32"``.
    subnormal_protection:
        When true, subnormal leaf weights are flushed to zero.
    base_score:
        Initial (round zero) prediction shared by every row.
    stage_tolerances:
        Per-stage relative deviation limits consumed by the error analyser.
    """

    n_trees: int = 20
    max_depth: int = 3
    learning_rate: float = 0.1
    subsample: float = 0.8
    colsample: float = 0.9
    min_leaf_samples: int = 5
    n_thresholds: int = 8
    precision: str = "float64"
    seed: int = 20261124
    min_gain: float = 0.0
    gain_strategy: str = "strict"
    gain_tol: float = 1e-9
    hessian_floor: float = 1.0
    hessian_floor_default: float = 1.0
    rounding: str = "half_even"
    subnormal_protection: bool = True
    base_score: float = 0.0
    # Declared per-stage relative-deviation limits.  They accommodate the
    # float32 precision tier (whose machine epsilon is ~1.2e-7) while still
    # catching genuine regressions; float64 runs stay far below them.
    stage_tolerances: Tuple[float, ...] = (
        1e-6,  # quantization
        1e-6,  # gain
        1e-6,  # leaf division
        1e-5,  # learning-rate scaling
        1e-5,  # accumulation
    )

    def validate_basic(self) -> None:
        """Validate fields that do not depend on a specific tree node."""
        ranges = {
            "n_trees": (self.n_trees, lambda v: v >= 1),
            "max_depth": (self.max_depth, lambda v: v >= 1),
            "subsample": (self.subsample, lambda v: 0.0 < v <= 1.0),
            "colsample": (self.colsample, lambda v: 0.0 < v <= 1.0),
            "min_leaf_samples": (self.min_leaf_samples, lambda v: v >= 1),
            "n_thresholds": (self.n_thresholds, lambda v: v >= 2),
            "min_gain": (self.min_gain, lambda v: v >= 0.0),
            "gain_tol": (self.gain_tol, lambda v: v >= 0.0),
        }
        for field, (value, ok) in ranges.items():
            if not ok(value):
                raise DomainError(
                    ErrorCode.CONFIG_INVALID,
                    f"invalid value {value!r} for config field '{field}'",
                    stage="config",
                    field=field,
                )
        if self.precision not in VALID_PRECISION:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                f"precision must be one of {VALID_PRECISION}, got {self.precision!r}",
                stage="config",
                field="precision",
            )
        if self.rounding not in VALID_ROUNDING:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                f"rounding must be one of {VALID_ROUNDING}, got {self.rounding!r}",
                stage="config",
                field="rounding",
            )
        if self.gain_strategy not in VALID_GAIN_STRATEGY:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                f"gain_strategy must be one of {VALID_GAIN_STRATEGY}, "
                f"got {self.gain_strategy!r}",
                stage="config",
                field="gain_strategy",
            )
        if len(self.stage_tolerances) != 5 or any(
            t <= 0.0 for t in self.stage_tolerances
        ):
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                "stage_tolerances must contain five positive values",
                stage="config",
                field="stage_tolerances",
            )
        self.validate_floor("hessian_floor", self.hessian_floor)
        self.validate_floor(
            "hessian_floor_default", self.hessian_floor_default
        )

    @staticmethod
    def validate_floor(field: str, value: float, node_path: str = "<root>") -> None:
        """Validate a hessian floor, optionally with a concrete leaf path."""
        if not np.isfinite(value) or value < 0.0:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                f"illegal hessian floor {value!r} for field '{field}' "
                f"at node path '{node_path}'",
                stage="leaf",
                field=field,
                node_path=node_path,
            )

    def rng(self, name: str) -> np.random.Generator:
        """Derive an independent named RNG stream from the master seed.

        Stream names are part of the reproducibility contract: changing a
        name changes the sequence, so they are never generated dynamically.
        """
        streams = {
            "data": 1,
            "row_sample": 2,
            "col_sample": 3,
            "verify": 4,
            "injection": 5,
        }
        if name not in streams:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                f"unknown RNG stream '{name}'",
                stage="config",
                field="seed",
            )
        ss = np.random.SeedSequence([self.seed, streams[name]])
        return np.random.default_rng(ss)

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["stage_tolerances"] = list(self.stage_tolerances)
        return data
