"""Offline, deterministic multi-factor authentication decision component."""

from .engine import EngineConfig, MFAEngine, load_engine_from_samples
from .errors import MFAError
from .samplegen import generate_samples

__all__ = [
    "MFAEngine",
    "EngineConfig",
    "MFAError",
    "generate_samples",
    "load_engine_from_samples",
]

__version__ = "0.1.0"

