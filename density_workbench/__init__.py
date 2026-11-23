"""Deterministic clustering and density-estimation diagnostics workbench."""

from .config import WorkbenchConfig
from .errors import BusinessError
from .workbench import Workbench, ensure_workspace

__all__ = ["BusinessError", "Workbench", "WorkbenchConfig", "ensure_workspace"]

__version__ = "1.0.0"
