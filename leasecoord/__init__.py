"""Deterministic local lease election and failover engine."""

from .engine import Engine
from .errors import BusinessError

__all__ = ["Engine", "BusinessError"]
__version__ = "1.0.0"
