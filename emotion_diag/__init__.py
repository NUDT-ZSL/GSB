"""Deterministic speech emotion and dialogue-turn diagnostic engine."""

from .errors import ErrorCode, DiagnosticError, error_envelope
from .config import EngineConfig, load_config, DEFAULT_CONFIG

__all__ = [
    "ErrorCode",
    "DiagnosticError",
    "error_envelope",
    "EngineConfig",
    "load_config",
    "DEFAULT_CONFIG",
]

__version__ = "1.0.0"
