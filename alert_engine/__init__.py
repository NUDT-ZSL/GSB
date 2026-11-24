"""Offline alert correlation and de-noising engine.

The package deliberately depends only on the Python standard library.  The
public entry point used by the command line is :class:`Engine`.
"""

from .engine import Engine
from .models import EngineConfig

__all__ = ["Engine", "EngineConfig"]
__version__ = "1.0.0"
