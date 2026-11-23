"""Structured business error codes and exceptions for the xrel pipeline.

Every business failure is represented by an :class:`XrelError` carrying a
stable machine readable code, a human readable message and a ``context``
dictionary that always names the offending entity (for example ``query_id``
and ``term``) so callers can diagnose failures without parsing free text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Stable business error codes (part of the public contract).
# ---------------------------------------------------------------------------
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIDENCE_OUT_OF_RANGE = "E_CONFIDENCE_OUT_OF_RANGE"
E_DIRECTION_MISSING = "E_DIRECTION_MISSING"
E_BRIDGE_ENTRY_MISSING = "E_BRIDGE_ENTRY_MISSING"
E_ERR_SPECTRUM_INVALID = "E_ERR_SPECTRUM_INVALID"
E_THRESHOLD_STRATEGY_INVALID = "E_THRESHOLD_STRATEGY_INVALID"
E_THRESHOLD_TOLERANCE_INVALID = "E_THRESHOLD_TOLERANCE_INVALID"
E_STALE_RESULT = "E_STALE_RESULT"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_VERSION_ROLLBACK = "E_VERSION_ROLLBACK"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"


@dataclass
class XrelError(Exception):
    """Base class for all structured business errors."""

    code: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        suffix = " ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.code}: {self.message} {suffix}".rstrip()

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


class ConfigInvalid(XrelError):
    def __init__(self, message: str, field_name: Optional[str] = None, **context: Any) -> None:
        ctx = dict(context)
        if field_name is not None:
            ctx["field"] = field_name
        super().__init__(E_CONFIG_INVALID, message, ctx)


class ConfidenceOutOfRange(XrelError):
    def __init__(self, query_id: str, term: str, value: Any) -> None:
        super().__init__(
            E_CONFIDENCE_OUT_OF_RANGE,
            "bridge confidence must lie within the inclusive [0.0, 1.0] interval",
            {"query_id": query_id, "term": term, "value": value},
        )


class DirectionMissing(XrelError):
    def __init__(self, query_id: str, term: str, source_lang: str, target_lang: str) -> None:
        super().__init__(
            E_DIRECTION_MISSING,
            "language pair direction is not declared in the direction table",
            {
                "query_id": query_id,
                "term": term,
                "source_lang": source_lang,
                "target_lang": target_lang,
            },
        )


class ErrorSpectrumInvalid(XrelError):
    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(E_ERR_SPECTRUM_INVALID, message, context)


class StaleResult(XrelError):
    def __init__(self, query_id: str, stale_dependencies: Dict[str, Any]) -> None:
        super().__init__(
            E_STALE_RESULT,
            "cached result references dependency versions that are no longer current",
            {"query_id": query_id, "stale_dependencies": stale_dependencies},
        )


class LineageBroken(XrelError):
    def __init__(self, query_id: str, message: str, **context: Any) -> None:
        ctx = {"query_id": query_id}
        ctx.update(context)
        super().__init__(E_LINEAGE_BROKEN, message, ctx)


class StageBusy(XrelError):
    def __init__(self, stage: str) -> None:
        super().__init__(
            E_STAGE_BUSY,
            "another commit is already in progress for this stage; requester is rejected",
            {"stage": stage},
        )


class VersionRollback(XrelError):
    def __init__(self, resource: str, current_version: int, rejected_version: int) -> None:
        super().__init__(
            E_VERSION_ROLLBACK,
            "an already published version cannot be overwritten by an older version",
            {
                "resource": resource,
                "current_version": current_version,
                "rejected_version": rejected_version,
            },
        )


class ResourceLimitExceeded(XrelError):
    def __init__(self, limit_name: str, used: Any, limit: Any, **context: Any) -> None:
        ctx = {"limit_name": limit_name, "used": used, "limit": limit}
        ctx.update(context)
        super().__init__(E_RESOURCE_LIMIT, "configurable resource budget was exceeded", ctx)
