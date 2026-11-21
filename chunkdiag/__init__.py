"""Deterministic chunked convolution/interpolation diagnostics."""

from .api import (
    ExecutionResult,
    adjudicate_deviations,
    conv2d,
    inject_diagnose,
    interpolate,
    run_task,
)
from .errors import DiagnosticError

__all__ = [
    "DiagnosticError",
    "ExecutionResult",
    "adjudicate_deviations",
    "conv2d",
    "inject_diagnose",
    "interpolate",
    "run_task",
]

__version__ = "1.0.0"
