"""Deterministic local tabular sample generation.

No external data files are used: :func:`make_regression_dataset` builds a
fixed numeric/categorical feature matrix and a regression target from a
seeded RNG.  The same ``seed`` always yields byte-identical arrays.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

from .config import TrainConfig


@dataclass(frozen=True)
class Dataset:
    """Generated sample bundle.

    ``X`` keeps raw values: numeric columns are float64, categorical
    columns are non-negative integer codes.  ``categorical_mask`` marks
    which feature indices are categorical.
    """

    X: np.ndarray
    y: np.ndarray
    categorical_mask: np.ndarray
    numeric_names: List[str]
    categorical_names: List[str]
    category_cardinalities: List[int]
    seed: int

    @property
    def n_rows(self) -> int:
        return self.X.shape[0]

    @property
    def n_features(self) -> int:
        return self.X.shape[1]

    def to_dict(self) -> dict:
        return {
            "n_rows": self.n_rows,
            "n_features": self.n_features,
            "categorical_mask": self.categorical_mask.astype(bool),
            "numeric_names": self.numeric_names,
            "categorical_names": self.categorical_names,
            "category_cardinalities": self.category_cardinalities,
            "seed": self.seed,
            "X": self.X,
            "y": self.y,
        }


def make_regression_dataset(
    n_rows: int = 512,
    n_numeric: int = 5,
    n_categorical: int = 3,
    *,
    seed: int | None = None,
    config: TrainConfig | None = None,
    noise_std: float = 0.05,
) -> Dataset:
    """Generate the deterministic regression sample.

    The target is a known additive function of the features plus a seeded
    Gaussian noise term, which gives the trees a learnable but non-trivial
    signal.
    """
    if n_rows < 32:
        raise ValueError("n_rows must be at least 32 for stable quantiles")
    if n_numeric < 1 or n_categorical < 1:
        raise ValueError("both numeric and categorical columns are required")

    effective_seed = seed if seed is not None else (
        config.seed if config is not None else 0
    )
    rng = np.random.default_rng(
        np.random.SeedSequence([effective_seed, 101])
    )

    numeric = np.empty((n_rows, n_numeric), dtype=np.float64)
    scales = np.linspace(0.5, 3.0, n_numeric)
    for j in range(n_numeric):
        # Different distributions per column while staying fully seeded.
        raw = rng.normal(loc=0.0, scale=scales[j], size=n_rows)
        numeric[:, j] = raw + rng.uniform(-0.25, 0.25, size=n_rows)

    cardinalities = [3 + (j % 4) for j in range(n_categorical)]
    categorical = np.empty((n_rows, n_categorical), dtype=np.float64)
    cat_effects: List[np.ndarray] = []
    for j, card in enumerate(cardinalities):
        codes = rng.integers(0, card, size=n_rows).astype(np.float64)
        categorical[:, j] = codes
        effects = rng.normal(0.0, 0.8, size=card)
        cat_effects.append(effects)

    # Additive target: alternating signed numeric contributions + cat effects.
    target = np.zeros(n_rows, dtype=np.float64)
    weights = rng.uniform(-1.5, 1.5, size=n_numeric)
    for j in range(n_numeric):
        target += weights[j] * numeric[:, j]
    for j, card in enumerate(cardinalities):
        codes = categorical[:, j].astype(np.int64)
        target += cat_effects[j][codes]
    target += rng.normal(0.0, noise_std, size=n_rows)

    X = np.concatenate([numeric, categorical], axis=1).astype(np.float64)
    mask = np.array(
        [False] * n_numeric + [True] * n_categorical, dtype=bool
    )
    numeric_names = [f"num_{j}" for j in range(n_numeric)]
    categorical_names = [f"cat_{j}" for j in range(n_categorical)]

    return Dataset(
        X=X,
        y=target,
        categorical_mask=mask,
        numeric_names=numeric_names,
        categorical_names=categorical_names,
        category_cardinalities=cardinalities,
        seed=effective_seed,
    )
