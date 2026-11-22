"""Local content-addressed block snapshots with crash-safe rollback."""

from .errors import BlockStoreError, ErrorCode

__all__ = ["BlockStoreError", "ErrorCode"]
__version__ = "0.1.0"
