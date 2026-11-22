"""Deterministic local columnar aggregation and sliding-window engine."""

from .errors import EngineError
from .model import load_experiment

__all__ = ["EngineError", "load_experiment"]
