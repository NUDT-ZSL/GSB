"""Local deterministic cross-document event coreference workbench."""

from .errors import BusinessError
from .config import AppConfig
from .generator import DocumentGenerator
from .engine import IncrementalEngine

__all__ = [
    "AppConfig",
    "BusinessError",
    "DocumentGenerator",
    "IncrementalEngine",
]
