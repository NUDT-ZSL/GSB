"""Local deterministic dialog emotion analysis application."""

from .config import AnalysisConfig
from .engine import AnalysisEngine, ExecutionResult
from .errors import BusinessError

__all__ = ["AnalysisConfig", "AnalysisEngine", "BusinessError", "ExecutionResult"]
__version__ = "1.0.0"

