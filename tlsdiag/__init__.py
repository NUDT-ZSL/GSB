"""Local TLS chain validation and trust-time precision diagnostic engine.

The package deliberately never performs network access and never reads the
system wall clock for validation decisions.  Every time comparison is driven
by an injected deterministic logical clock expressed in milliseconds.
"""

from .errors import DiagError, ERROR_CODES

__all__ = ["DiagError", "ERROR_CODES"]
__version__ = "1.0.0"
