"""Deterministic elastic-scaling workbench for local ML inference services."""

from .declaration import load_declaration, load_declaration_text
from .errors import BusinessError
from .workbench import Workbench

__all__ = [
    "BusinessError",
    "Workbench",
    "load_declaration",
    "load_declaration_text",
]

__version__ = "1.0.0"
