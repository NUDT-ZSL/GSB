"""Deterministic local operator verification service."""

__version__ = "1.0.0"

from .errors import ERROR_CODES, OpVerifyError, error_response
from .engine import evaluate_job, run_unified

execute_operator = run_unified
execute_task = run_unified

__all__ = [
    "ERROR_CODES",
    "OpVerifyError",
    "error_response",
    "evaluate_job",
    "run_unified",
    "execute_operator",
    "execute_task",
    "__version__",
]
