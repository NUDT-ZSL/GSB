"""Deterministic, budget-aware unsupervised anomaly detection engine."""

from .engine import run_experiment, run_scenario
from .errors import EngineError

__all__ = ["EngineError", "run_experiment", "run_scenario"]
__version__ = "1.0.0"
