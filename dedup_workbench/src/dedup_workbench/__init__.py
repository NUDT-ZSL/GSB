"""Deterministic LLM pretraining corpus dedup diagnostic workbench."""

__version__ = "1.0.0"

from .engine import DedupWorkbench
from .errors import WorkbenchError
from .query import QueryService

__all__ = ["DedupWorkbench", "QueryService", "WorkbenchError", "__version__"]
