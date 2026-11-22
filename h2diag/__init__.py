"""Deterministic HTTP/2 multiplexing diagnostic workbench."""

from .canonical import canonical_json, digest_json
from .engine import Engine
from .service import DiagnosticService

__all__ = [
    "DiagnosticService",
    "Engine",
    "canonical_json",
    "digest_json",
]

__version__ = "1.0.0"
