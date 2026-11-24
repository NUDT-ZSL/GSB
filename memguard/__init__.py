"""Deterministic memory-boundary guard.

The package intentionally has no third-party runtime dependencies.
"""

from .codes import POLICY_VERSION
from .engine import EngineResult, RunOptions, run

__all__ = ["EngineResult", "POLICY_VERSION", "RunOptions", "run"]
__version__ = "0.1.0"
