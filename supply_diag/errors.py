"""Stable business error and warning codes."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field


E_INVALID_JSON = "E_INVALID_JSON"
E_SCHEMA = "E_SCHEMA"
E_PROJECT_ID = "E_PROJECT_ID"
E_DUPLICATE_PROJECT = "E_DUPLICATE_PROJECT"
E_MANIFEST_VERSION = "E_MANIFEST_VERSION"
E_COMPONENT_ID = "E_COMPONENT_ID"
E_DUPLICATE_COMPONENT = "E_DUPLICATE_COMPONENT"
E_VERSION_EXPR = "E_VERSION_EXPR"
E_INVALID_CONSTRAINT = "E_INVALID_CONSTRAINT"
E_HASH_ALGO = "E_HASH_ALGO"
E_HASH_MISMATCH = "E_HASH_MISMATCH"
E_SIGNATURE_STATUS = "E_SIGNATURE_STATUS"
E_UNKNOWN_DEPENDENCY = "E_UNKNOWN_DEPENDENCY"
E_CYCLE = "E_CYCLE"
E_CONSTRAINT_UNSAT = "E_CONSTRAINT_UNSAT"
E_IO = "E_IO"
E_CHECKPOINT = "E_CHECKPOINT"
E_TRUTH_MISMATCH = "E_TRUTH_MISMATCH"
W_BOUND_UNCERTAIN = "W_BOUND_UNCERTAIN"
W_SIGNATURE_MISSING = "W_SIGNATURE_MISSING"


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    entry_index: int | None = None
    component_id: str | None = None
    field: str | None = None
    details: dict = dataclass_field(default_factory=dict, compare=False, hash=False)

    def to_dict(self) -> dict:
        result: dict = {"code": self.code, "message": self.message}
        if self.entry_index is not None:
            result["entry_index"] = self.entry_index
        if self.component_id is not None:
            result["component_id"] = self.component_id
        if self.field is not None:
            result["field"] = self.field
        if self.details:
            result["details"] = self.details
        return result


class InputError(ValueError):
    """A validation error that can be surfaced as a business issue."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

