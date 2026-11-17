"""ColPipe: a deterministic, local columnar query pipeline.

The package intentionally keeps its core on the Python standard library.  A
locally installed NumPy is detected and used for numeric statistics when it is
available; it is never required to load, plan, execute, verify or replay a
session.
"""

from __future__ import annotations

__version__ = "1.0.0"

