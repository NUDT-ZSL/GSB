"""Structured business error codes shared across the monitoring pipeline.

Every failure that originates from business rules (as opposed to ordinary
Python programming errors) is raised as :class:`DriftError` carrying a stable
string code, a human readable message and a structured ``context`` mapping so
that the CLI and the interactive application can surface "the most recent
business error code" deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


class ErrorCode:
    # Configuration readers / migrations
    VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    ROUNDTRIP_MISMATCH = "E_ROUNDTRIP_MISMATCH"
    CONFIG_INVALID = "E_CONFIG_INVALID"
    # Batch ingestion / statistics
    TYPE_UNSUPPORTED = "E_TYPE_UNSUPPORTED"
    EMPTY_BATCH = "E_EMPTY_BATCH"
    ILLEGAL_VALUE = "E_ILLEGAL_VALUE"
    # Windowing
    WINDOW_INVALID = "E_WINDOW_INVALID"
    # Content addressed cache
    CACHE_KEY_INVALID = "E_CACHE_KEY_INVALID"
    # Incremental dependency propagation
    INCONSISTENT = "E_INCONSISTENT"
    # Snapshot / alert governance
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    ALERT_STALE = "E_ALERT_STALE"
    # Resource governance
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"


@dataclass
class DriftError(Exception):
    code: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        loc = ""
        if self.context:
            loc = " | " + ",".join(f"{k}={v}" for k, v in sorted(self.context.items()))
        return f"[{self.code}] {self.message}{loc}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "context": dict(sorted(self.context.items())),
        }
