"""Deterministic ordered-delivery gateway."""

from .engine import GatewayEngine
from .errors import GatewayError

__all__ = ["GatewayEngine", "GatewayError"]
__version__ = "1.0.0"
