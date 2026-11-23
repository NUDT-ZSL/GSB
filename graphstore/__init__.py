"""Local two-generation graph snapshot engine.

The public surface intentionally stays small: :class:`GraphStore` owns all
SQLite/file-lock coordination, while the snapshot, merge, index and traversal
modules remain independently usable in tests and integrations.
"""

from .errors import ErrorCode, GraphStoreError
from .models import TraversalRequest
from .service import GraphStore

__all__ = [
    "ErrorCode",
    "GraphStore",
    "GraphStoreError",
    "TraversalRequest",
]

__version__ = "1.0.0"

