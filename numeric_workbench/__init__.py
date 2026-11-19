"""Local numerical consistency diagnostic workbench."""

from .config import TrainingConfig
from .errors import WorkbenchError

__all__ = ["TrainingConfig", "WorkbenchError"]
__version__ = "1.0.0"
