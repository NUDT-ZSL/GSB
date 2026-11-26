"""Typed error codes for the keyframe diffusion studio.

Every business failure raises :class:`KFError` with one of the stable
``E_*`` codes so CLI / HTTP / test callers can branch on the code instead of
parsing human readable messages.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


E_CLIP_SHAPE = "E_CLIP_SHAPE"
E_BLOCK_MISMATCH = "E_BLOCK_MISMATCH"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_INCONSISTENT = "E_INCONSISTENT"
E_NOT_FOUND = "E_NOT_FOUND"
E_BAD_REQUEST = "E_BAD_REQUEST"
E_CHECKSUM_MISMATCH = "E_CHECKSUM_MISMATCH"


class KFError(Exception):
    """Business exception carrying a stable error code and structured details."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_envelope(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"
