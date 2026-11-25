"""Deterministic precision diagnostics for multivariate time series."""

from .engine import DiagnosticEngine
from .errors import DiagnosticError
from .generator import generate_fixture
from .precision import Precision

__version__ = "1.0.0"

__all__ = ["DiagnosticEngine", "DiagnosticError", "Precision", "generate_fixture"]
