"""FeatureLab: deterministic, interactive tabular feature engineering."""

from .app import FeatureEngineeringApp
from .config import ExperimentConfig, PlanConfig, load_experiment_config
from .data import Dataset, load_dataset
from .errors import FeatureLabError

__all__ = [
    "Dataset",
    "ExperimentConfig",
    "FeatureEngineeringApp",
    "FeatureLabError",
    "PlanConfig",
    "load_dataset",
    "load_experiment_config",
]

__version__ = "1.0.0"
