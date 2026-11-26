"""Deterministic diagnostics for regularized tabular binary classification."""

from .config import EngineConfig
from .engine import DiagnosticEngine, train_precision
from .errors import DiagnosticError, DiagnosticIssue

__all__ = [
    "DiagnosticEngine",
    "DiagnosticError",
    "DiagnosticIssue",
    "EngineConfig",
    "train_precision",
]

__version__ = "0.1.0"

