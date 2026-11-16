"""Business error codes raised by the migration engine.

All engine failures raise :class:`MigrationError` carrying a stable business
code so the API layer, console and demo script can report deterministic
results without parsing free-text messages.
"""

from __future__ import annotations

from enum import Enum


class BusinessCode(str, Enum):
    """Stable business codes required by the specification."""

    E_RULE_ORDER = "E_RULE_ORDER"
    E_CYCLE = "E_CYCLE"
    E_TARGET_MISSING = "E_TARGET_MISSING"
    E_STALE_SOURCE = "E_STALE_SOURCE"
    E_VERSION_REGRESS = "E_VERSION_REGRESS"
    E_INTERNAL = "E_INTERNAL"


class MigrationError(Exception):
    """Raised when a version cannot be applied.

    The surrounding SQLite transaction is rolled back, so the database stays
    at the state of the last successfully committed version.
    """

    def __init__(self, code: BusinessCode, message: str, *, rule_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.rule_id = rule_id

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "message": self.message,
            "rule_id": self.rule_id,
        }
