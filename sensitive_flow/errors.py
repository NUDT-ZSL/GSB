"""Business error types and stable error codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_ARGUMENT = "E_ARGUMENT"
E_INPUT = "E_INPUT"
E_IO = "E_IO"
E_LOCK = "E_LOCK"
E_DOCUMENT = "E_DOCUMENT"
E_SNAPSHOT_VERSION = "E_SNAPSHOT_VERSION"
E_SNAPSHOT_ORDER = "E_SNAPSHOT_ORDER"
E_RULE_VERSION = "E_RULE_VERSION"
E_FIELD_DUPLICATE = "E_FIELD_DUPLICATE"
E_PATH = "E_PATH"
E_PATH_INDEX = "E_PATH_INDEX"
E_ARRAY_BOUNDS = "E_ARRAY_BOUNDS"
E_LEVEL = "E_LEVEL"
E_MASK_STATE = "E_MASK_STATE"
E_DERIVE_VERSION = "E_DERIVE_VERSION"
E_DERIVE_TARGET = "E_DERIVE_TARGET"
E_DERIVE_SOURCE = "E_DERIVE_SOURCE"
E_DERIVE_DUPLICATE = "E_DERIVE_DUPLICATE"
E_DERIVE_CYCLE = "E_DERIVE_CYCLE"
E_MASK_BYPASS = "E_MASK_BYPASS"
E_DANGLING_MASK = "E_DANGLING_MASK"
E_EDIT_TYPE = "E_EDIT_TYPE"
E_EDIT_SEQ = "E_EDIT_SEQ"
E_EDIT_FIELD = "E_EDIT_FIELD"
E_EDIT_DERIVATION = "E_EDIT_DERIVATION"
E_EDIT_DEPENDENCY = "E_EDIT_DEPENDENCY"
E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
E_CHECKPOINT_SCHEMA = "E_CHECKPOINT_SCHEMA"
E_CHECKPOINT_RULE = "E_CHECKPOINT_RULE"
E_CHECKPOINT_STATE = "E_CHECKPOINT_STATE"
E_CHECKPOINT_ORDER = "E_CHECKPOINT_ORDER"
E_CHECKPOINT_TEMP_RESIDUE = "E_CHECKPOINT_TEMP_RESIDUE"
E_FORCED_TERMINATION = "E_FORCED_TERMINATION"


@dataclass
class TraceError(Exception):
    """A failure that is safe to serialize as a business diagnostics error."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class ForcedTermination(Exception):
    """Test/CLI crash boundary after a complete checkpoint was committed."""

    checkpoint_edit_count: int

    def __post_init__(self) -> None:
        super().__init__(f"forced termination after edit {self.checkpoint_edit_count}")
