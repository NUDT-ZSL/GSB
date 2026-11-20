"""Business error codes and structured exception hierarchy.

Every failure that crosses a module boundary is represented by an
``OcrBenchError`` carrying a stable string error code, the affected
``page_id`` (when applicable), the pipeline ``stage`` and a structured
``details`` mapping.  This makes exception-chain attribution deterministic and
renderable by the diagnostic workbench.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class ErrorCode:
    """Stable business error codes (part of the diagnostic contract)."""

    CACHE_KEY_INVALID = "E_CACHE_KEY_INVALID"
    CACHE_CORRUPT = "E_CACHE_CORRUPT"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    CHUNK_MISMATCH = "E_CHUNK_MISMATCH"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    STAGE_BUSY = "E_STAGE_BUSY"
    INCONSISTENT = "E_INCONSISTENT"
    PARSE_FAILED = "E_PARSE_FAILED"
    DETECT_EMPTY = "E_DETECT_EMPTY"
    MATCH_FAILED = "E_MATCH_FAILED"
    GRAPH_CYCLE = "E_GRAPH_CYCLE"
    GRAPH_CONFIG = "E_GRAPH_CONFIG"


@dataclass
class OcrBenchError(Exception):
    """Structured error with code, stage, page and diagnostic details."""

    code: str
    message: str
    page_id: Optional[str] = None
    stage: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        loc = ""
        if self.page_id is not None:
            loc += f" page={self.page_id}"
        if self.stage is not None:
            loc += f" stage={self.stage}"
        detail = f" details={self.details}" if self.details else ""
        return f"[{self.code}]{loc} {self.message}{detail}"


def make_error(
    code: str,
    message: str,
    *,
    page_id: Optional[str] = None,
    stage: Optional[str] = None,
    **details: Any,
) -> OcrBenchError:
    """Construct a structured :class:`OcrBenchError`."""

    return OcrBenchError(
        code=code,
        message=message,
        page_id=page_id,
        stage=stage,
        details=dict(details),
    )
