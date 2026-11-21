"""Deterministic low-precision inference diagnostics workbench."""

from .config import EngineConfig, Injection
from .errors import BusinessError

__all__ = ["BusinessError", "EngineConfig", "Injection"]

__version__ = "1.0.0"
