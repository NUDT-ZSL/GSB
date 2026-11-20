"""Business errors used by the tunnel kernel.

The error code is intentionally part of the public API. Callers should branch on
``code`` rather than parsing human-readable messages.
"""

from __future__ import annotations

from typing import Any


E_BAD_CONFIG = "E_BAD_CONFIG"
E_BAD_ENDPOINT = "E_BAD_ENDPOINT"
E_BAD_MTU = "E_BAD_MTU"
E_BAD_PAYLOAD = "E_BAD_PAYLOAD"
E_DUPLICATE_PEER = "E_DUPLICATE_PEER"
E_BAD_LINK = "E_BAD_LINK"
E_BAD_POLICY = "E_BAD_POLICY"
E_BAD_TIMELINE = "E_BAD_TIMELINE"
E_NO_PROFILE = "E_NO_PROFILE"

E_BAD_FRAGMENT = "E_BAD_FRAGMENT"
E_OUT_OF_ORDER = "E_OUT_OF_ORDER"
E_MISSING_FRAGMENT = "E_MISSING_FRAGMENT"
E_DUPLICATE_FRAGMENT = "E_DUPLICATE_FRAGMENT"
E_CONFLICTING_FRAGMENT = "E_CONFLICTING_FRAGMENT"
E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"

E_RETRY_EXHAUSTED = "E_RETRY_EXHAUSTED"
E_TUNNEL_BUSY = "E_TUNNEL_BUSY"
E_TUNNEL_LOCKED = "E_TUNNEL_LOCKED"
E_SEQUENCE_INFLIGHT = "E_SEQUENCE_INFLIGHT"
E_ABORTED = "E_ABORTED"
E_ROUND_BUDGET = "E_ROUND_BUDGET"
E_NOT_FOUND = "E_NOT_FOUND"
E_STATE_CORRUPT = "E_STATE_CORRUPT"
E_ECHO_MISMATCH = "E_ECHO_MISMATCH"
E_IO = "E_IO"
E_UNSUPPORTED = "E_UNSUPPORTED"


class BusinessError(Exception):
    """A deterministic, machine-readable kernel failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def fail(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    raise BusinessError(code, message, details=details)
