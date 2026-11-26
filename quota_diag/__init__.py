"""Deterministic multi-level directory quota diagnostic component."""

from .errors import DiagError
from .service import QuotaDiagnostic

__all__ = ["DiagError", "QuotaDiagnostic"]
__version__ = "1.0.0"
