"""Deterministic living-room stereo display assessment kernel.

The package intentionally depends only on the Python standard library.
"""

from .errors import ProjectValidationError, ValidationIssue
from .loader import load_project
from .evaluator import evaluate_project
from .incremental import incremental_evaluate

__all__ = [
    "ProjectValidationError",
    "ValidationIssue",
    "evaluate_project",
    "incremental_evaluate",
    "load_project",
]

__version__ = "1.0.0"
