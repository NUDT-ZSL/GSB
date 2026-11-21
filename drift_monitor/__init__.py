"""Local, deterministic feature-drift monitoring application.

The package is deliberately dependency-light: it only needs the Python
standard library and NumPy.  Nothing here performs network access or reads
data from any external service; every number is derived from fixed seeds.
"""

from .errors import DriftError, ErrorCode

__all__ = ["DriftError", "ErrorCode"]
__version__ = "1.0.0"
