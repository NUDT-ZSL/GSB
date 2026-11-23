"""Business error codes shared by every engine layer.

Validation failures raise :class:`DiagError` carrying a stable string code so
that callers (CLI and tests) can branch on machine readable outcomes instead
of parsing human readable messages.
"""

from __future__ import annotations


ERROR_CODES = {
    "E1001": "v1 alias duplicated",
    "E1002": "v1 fingerprint does not match PEM certificate digest",
    "E1003": "v1 relative trust seconds must be a non-negative integer",
    "E1004": "v1 entry missing a required field",
    "E1005": "v1 PEM certificate cannot be parsed",
    "E1006": "v1 enabled flag must be boolean",
    "E2001": "v2 shard file hash mismatch",
    "E2002": "v2 manifest references a missing shard",
    "E2003": "v2 trust generation is not monotonic for an alias",
    "E2004": "v2 effective millisecond timestamp must be an integer",
    "E2005": "v2 alert tombstone has an illegal type/value",
    "E2006": "v2 revocation flag has an illegal type/value",
    "E2007": "v2 SQLite metadata is missing or malformed",
    "E2008": "v2 shard alias missing from SQLite metadata",
    "E2009": "v2 shard payload is malformed",
    "E3001": "snapshot version rollback rejected",
    "E3002": "snapshot patch already committed (idempotent replay)",
    "E3003": "persistent state is corrupt or from an unsupported schema",
    "E4001": "certificate chain cannot be built",
    "E4002": "certificate signature verification failed",
    "E4003": "basic constraints or path length violation",
    "E4004": "key/extended key usage mismatch",
    "E4005": "certificate expired or not yet valid at logical instant",
    "E4006": "hostname verification failed",
    "E4007": "certificate is revoked",
    "E5001": "precision configuration is invalid",
}


class DiagError(Exception):
    """Engine error carrying a stable business ``code``."""

    def __init__(self, code: str, detail: str = ""):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {ERROR_CODES[code]} ({detail})" if detail else f"{code}: {ERROR_CODES[code]}")

    def to_dict(self) -> dict:
        return {"code": self.code, "message": ERROR_CODES[self.code], "detail": self.detail}
