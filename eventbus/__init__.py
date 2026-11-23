"""Deterministic local event-bus bridge middleware."""

from .errors import BusinessError
from .engine import EventBus
from .validation import load_dataset

__all__ = ["BusinessError", "EventBus", "load_dataset"]
__version__ = "1.0.0"
