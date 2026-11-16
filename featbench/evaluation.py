"""Deterministic downstream evaluation on reduced feature matrices."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from .config import Config
from .data import SyntheticDataset
from .hashing import digest_iterable
from .reduction import ReductionResult


@dataclass
class EvaluationResult:
    scale_id: str
    method: str
    metrics: dict
    evaluation_fingerprint: str


def evaluate(config: Config, dataset: SyntheticDataset,
             reduced: ReductionResult) -> EvaluationResult:
    features = reduced.reduced
    target = dataset.target
    n_samples = features.shape[0]
    if features.shape[1] == 0 or n_samples < 4:
        metrics = {"r2": None, "rmse": None, "mae": None,
                   "train_size": n_samples, "test_size": 0,
                   "note": "insufficient samples or features for evaluation"}
    else:
        seed = int(digest_iterable([config.config_fingerprint, dataset.scale_id,
                                    "split"])[:16], 16)
        rng = np.random.default_rng(seed)
        permutation = rng.permutation(n_samples)
        test_size = max(1, n_samples // 5)
        test_idx = permutation[:test_size]
        train_idx = permutation[test_size:]
        model = Ridge(alpha=1.0, random_state=config.seed)
        model.fit(features[train_idx], target[train_idx])
        prediction = model.predict(features[test_idx])
        truth = target[test_idx]
        metrics = {
            "r2": float(r2_score(truth, prediction)),
            "rmse": float(np.sqrt(mean_squared_error(truth, prediction))),
            "mae": float(mean_absolute_error(truth, prediction)),
            "train_size": int(train_idx.size),
            "test_size": int(test_idx.size),
            "model_coef_digest": digest_iterable(
                [model.coef_.tolist(), float(model.intercept_)]),
            "note": "ok",
        }
    evaluation_fingerprint = digest_iterable([
        config.config_fingerprint, reduced.reduction_fingerprint, metrics,
    ])
    return EvaluationResult(dataset.scale_id, reduced.method, metrics,
                            evaluation_fingerprint)
