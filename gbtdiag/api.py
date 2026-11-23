"""Public domain component facade: train / predict / inject / validate."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np

from .boosting import (
    BoostingModel,
    predict_with_model,
    recompute_prediction,
    train_boosting,
)
from .config import TrainConfig
from .data import Dataset, make_regression_dataset
from .diagnostics import run_error_analysis, run_full_diagnostic, summarise_records
from .injection import InjectionSpec
from .io_utils import prepare_run_dir, snapshot, stable_hash
from .verifier import verify_reproducibility


def train(
    dataset: Dataset,
    config: TrainConfig,
    *,
    run_dir: Optional[str] = None,
    injection_specs: Optional[Sequence[InjectionSpec]] = None,
) -> BoostingModel:
    """Entry point 1: deterministic training with round-level details."""
    if run_dir is not None:
        prepare_run_dir(run_dir)
    model = train_boosting(dataset, config, injection_specs=injection_specs)
    if run_dir is not None:
        snapshot(Path(run_dir), "training_report", model.to_report())
    return model


def predict(model: BoostingModel, X: np.ndarray) -> np.ndarray:
    """Entry point 2: inference from stored tree contributions."""
    return predict_with_model(model, X)


def inject_diagnose(
    dataset: Dataset,
    config: TrainConfig,
    injection_specs: Sequence[InjectionSpec],
    *,
    run_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Entry point 3: run the four-stage injection + attribution pipeline."""
    if run_dir is not None:
        prepare_run_dir(run_dir)
    report = run_full_diagnostic(
        dataset, config, injection_specs=injection_specs
    )
    if run_dir is not None:
        snapshot(Path(run_dir), "injection_report", report)
    return report


def validate(
    dataset: Dataset,
    config: TrainConfig,
    *,
    injection_specs: Optional[Sequence[InjectionSpec]] = None,
    run_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Entry point 4: reproducibility, recomputation and tolerance checks."""
    if run_dir is not None:
        prepare_run_dir(run_dir)
    error_report = run_error_analysis(
        dataset, config, injection_specs=injection_specs
    )
    verification = verify_reproducibility(
        dataset,
        config,
        injection_specs=injection_specs,
        error_report=error_report,
    )
    model = train_boosting(dataset, config, injection_specs=injection_specs)
    report = {
        "verification": verification.to_dict(),
        "error_analysis": error_report,
        "attribution": summarise_records(model),
        "recomputed_prediction_hash": stable_hash(
            recompute_prediction(model)
        ),
        "final_prediction_hash": stable_hash(model.final_prediction),
    }
    if run_dir is not None:
        snapshot(Path(run_dir), "validation_report", report)
    return report


__all__ = [
    "train",
    "predict",
    "inject_diagnose",
    "validate",
    "make_regression_dataset",
    "TrainConfig",
    "Dataset",
    "InjectionSpec",
    "BoostingModel",
]
