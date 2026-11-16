"""Deterministic regularized linear-model experiment laboratory."""

from .config import ExperimentConfig, load_experiment
from .data import Dataset, build_dataset

__all__ = ["ExperimentConfig", "Dataset", "load_experiment", "build_dataset"]
__version__ = "1.0.0"
