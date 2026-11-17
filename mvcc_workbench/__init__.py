"""Local MVCC visibility workbench.

The package has no third-party runtime dependencies.  The Tkinter frontend is
imported lazily so command-line use and tests also work on Python builds where
Tkinter is not installed.
"""

from .engine import Workbench
from .errors import BusinessError

__all__ = ["BusinessError", "Workbench"]
__version__ = "1.0.0"
