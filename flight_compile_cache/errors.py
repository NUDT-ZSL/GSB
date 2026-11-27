"""Business error codes and structured errors used by the project.

The codes are deliberately stable strings: they are part of generated reports and
are used by the edge-case fixtures to verify that malformed inputs are skipped
instead of crashing the scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


E_PROJECT_SCHEMA = "E_PROJECT_SCHEMA"
E_PROJECT_NOT_FOUND = "E_PROJECT_NOT_FOUND"
E_MODEL_NOT_FOUND = "E_MODEL_NOT_FOUND"
E_MODEL_VERSION_MISSING = "E_MODEL_VERSION_MISSING"
E_VERSION_NOT_FOUND = "E_VERSION_NOT_FOUND"
E_STALE_VERSION = "E_STALE_VERSION"
E_GRAPH_SCHEMA = "E_GRAPH_SCHEMA"
E_GRAPH_DUPLICATE_NODE = "E_GRAPH_DUPLICATE_NODE"
E_GRAPH_REFERENCE_MISSING = "E_GRAPH_REFERENCE_MISSING"
E_GRAPH_CYCLE = "E_GRAPH_CYCLE"
E_GRAPH_OUTPUT_MISSING = "E_GRAPH_OUTPUT_MISSING"
E_OP_UNKNOWN = "E_OP_UNKNOWN"
E_OP_UNSUPPORTED = "E_OP_UNSUPPORTED"
E_OP_SHAPE_NOT_CLOSED = "E_OP_SHAPE_NOT_CLOSED"
E_INVALID_DIMENSION = "E_INVALID_DIMENSION"
E_CONST_SHAPE_MISMATCH = "E_CONST_SHAPE_MISMATCH"
E_REQUEST_SCHEMA = "E_REQUEST_SCHEMA"
E_REQUEST_DUPLICATE_ID = "E_REQUEST_DUPLICATE_ID"
E_MISSING_INPUT = "E_MISSING_INPUT"
E_UNKNOWN_INPUT = "E_UNKNOWN_INPUT"
E_INPUT_RANK_MISMATCH = "E_INPUT_RANK_MISMATCH"
E_INPUT_SHAPE_MISMATCH = "E_INPUT_SHAPE_MISMATCH"
E_SHAPE_SYMBOL_CONFLICT = "E_SHAPE_SYMBOL_CONFLICT"
E_EVENT_INVALID = "E_EVENT_INVALID"
E_EVENT_OUT_OF_ORDER = "E_EVENT_OUT_OF_ORDER"
E_SNAPSHOT_SCHEMA = "E_SNAPSHOT_SCHEMA"
E_SNAPSHOT_INTEGRITY = "E_SNAPSHOT_INTEGRITY"
E_SNAPSHOT_PROJECT_MISMATCH = "E_SNAPSHOT_PROJECT_MISMATCH"
E_SNAPSHOT_VERSION_CHANGED = "E_SNAPSHOT_VERSION_CHANGED"


REQUEST_SKIP_CODES = frozenset(
    {
        E_MODEL_NOT_FOUND,
        E_MODEL_VERSION_MISSING,
        E_VERSION_NOT_FOUND,
        E_STALE_VERSION,
        E_GRAPH_SCHEMA,
        E_GRAPH_DUPLICATE_NODE,
        E_GRAPH_REFERENCE_MISSING,
        E_GRAPH_CYCLE,
        E_GRAPH_OUTPUT_MISSING,
        E_OP_UNKNOWN,
        E_OP_UNSUPPORTED,
        E_OP_SHAPE_NOT_CLOSED,
        E_INVALID_DIMENSION,
        E_CONST_SHAPE_MISMATCH,
        E_REQUEST_SCHEMA,
        E_MISSING_INPUT,
        E_UNKNOWN_INPUT,
        E_INPUT_RANK_MISMATCH,
        E_INPUT_SHAPE_MISMATCH,
        E_SHAPE_SYMBOL_CONFLICT,
    }
)


@dataclass(frozen=True)
class BusinessError:
    """A machine-readable validation or execution error."""

    code: str
    message: str
    path: str = ""
    context: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path:
            out["path"] = self.path
        if self.context:
            out["context"] = self.context
        return out


class BusinessRuleError(Exception):
    """Raised internally when a business rule is violated.

    The caller should turn this exception into a :class:`BusinessError` rather
    than exposing a Python traceback.  Keeping the code attached makes edge-case
    handling deterministic.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = BusinessError(code, message, path, context or {})


def first_error(errors: Iterable[BusinessError]) -> BusinessError | None:
    for error in errors:
        return error
    return None
