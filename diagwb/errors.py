"""Business error codes used across the diagnostic workbench.

Every failure raised by the domain modules is represented by an
:class:`DiagError` carrying a stable, greppable error code plus structured
context. The workbench surface renders the most recent code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


E_FORMAT_VERSION_UNSUPPORTED = "E_FORMAT_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_MERGE_CONFLICT = "E_MERGE_CONFLICT"
E_RESULT_STALE = "E_RESULT_STALE"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"


@dataclass
class DiagError(Exception):
    """An error carrying a stable code plus machine-readable context."""

    code: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [f"[{self.code}] {self.message}"]
        if self.context:
            parts.append("context=" + repr(self.context))
        return " ".join(parts)


def raise_error(code: str, message: str, **context: Any) -> None:
    """Raise a :class:`DiagError` with the given code and context."""
    raise DiagError(code=code, message=message, context=context)
