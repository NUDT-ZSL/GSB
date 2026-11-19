"""Deterministic customer-service dialogue sentiment and risk judging."""

from .errors import AppError
from .service import DomainService

__all__ = ["AppError", "DomainService"]
__version__ = "1.0.0"

