"""Deterministic local OCR detection/recognition pipeline.

The package is deliberately built on the Python standard library plus NumPy.
Every artifact is produced from explicit inputs and fixed-seed pseudo random
number generators; wall-clock time never feeds into a decision or a hash.
"""

from .errors import PipelineError

__all__ = ["PipelineError"]

__version__ = "1.0.0"
