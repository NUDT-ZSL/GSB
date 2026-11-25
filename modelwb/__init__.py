"""Offline model hot-update and gray-release workbench."""

from .errors import WorkbenchError
from .workbench import Workbench

__all__ = ["Workbench", "WorkbenchError"]
__version__ = "1.0.0"
