"""Deterministic local voice-emotion and dialogue-understanding component."""

from .pipeline import Pipeline
from .synthetic import generate_demo

__all__ = ["Pipeline", "generate_demo"]
__version__ = "1.0.0"
