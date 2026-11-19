"""Deterministic local replication-lag diagnostics.

The package intentionally depends only on the Python standard library.  NumPy is
allowed by the exercise but is not needed for the exact integer/rational
arithmetic used here.
"""

from .errors import DiagnosticsError

__all__ = ["DiagnosticsError"]
__version__ = "1.0.0"
