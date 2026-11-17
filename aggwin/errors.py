"""Business error codes and structured error type.

Every validation or runtime failure is mapped to a stable business code so
that the CLI can emit deterministic exit codes and structured log records.
"""

from __future__ import annotations


class BusinessError(Exception):
    """Error carrying a stable business code plus structured context."""

    def __init__(self, code: str, message: str, context: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "context": self.context}


E_SCHEMA = "E_SCHEMA"
E_EXPR = "E_EXPR"
E_SNAPSHOT = "E_SNAPSHOT"
E_BATCH = "E_BATCH"
E_ORDER = "E_ORDER"
E_DIVERGE = "E_DIVERGE"


EXIT_CODES = {
    E_SCHEMA: 2,
    E_EXPR: 3,
    E_SNAPSHOT: 4,
    E_BATCH: 5,
    E_ORDER: 6,
    E_DIVERGE: 7,
}
