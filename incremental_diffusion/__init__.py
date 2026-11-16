"""Deterministic incremental diffusion workbench.

Public API:
    Workbench(workspace).run(config_path)
    Workbench(workspace).query(config_path)
"""

from .errors import WorkbenchError
from .workbench import Workbench

__all__ = ["Workbench", "WorkbenchError"]
__version__ = "0.1.0"

