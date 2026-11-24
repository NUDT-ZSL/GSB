"""Deterministic local haptic command-stream synthesis engine."""

from .engine import HapticEngine, RunResult, load_bundle_from_workspace
from .errors import HapticError
from .models import Bundle, EngineConfig, parse_bundle
from .samples import DEFAULT_SEED, generate_workspace

__all__ = [
    "DEFAULT_SEED",
    "Bundle",
    "EngineConfig",
    "HapticEngine",
    "HapticError",
    "RunResult",
    "generate_workspace",
    "load_bundle_from_workspace",
    "parse_bundle",
]
