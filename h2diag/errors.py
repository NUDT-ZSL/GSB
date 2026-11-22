from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DiagnosticError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int = 422
    category: str = "validation"

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category": self.category,
            "details": self.details,
        }


def error(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    http_status: int = 422,
    category: str = "validation",
) -> DiagnosticError:
    return DiagnosticError(
        code=code,
        message=message,
        details=details or {},
        http_status=http_status,
        category=category,
    )


# Scenario / event validation.
SCENARIO_JSON_001 = "SCENARIO_JSON_001"
SCENARIO_SCHEMA_001 = "SCENARIO_SCHEMA_001"
STREAM_ID_001 = "STREAM_ID_001"
STREAM_PARITY_001 = "STREAM_PARITY_001"
FLOW_INITIAL_001 = "FLOW_INITIAL_001"
WEIGHT_001 = "WEIGHT_001"
CLOCK_001 = "CLOCK_001"
EVENT_SEQ_001 = "EVENT_SEQ_001"
HEADER_ORDER_001 = "HEADER_ORDER_001"
HEADER_INCOMPLETE_001 = "HEADER_INCOMPLETE_001"
WINDOW_UPDATE_001 = "WINDOW_UPDATE_001"
SETTINGS_001 = "SETTINGS_001"
SETTINGS_ACK_001 = "SETTINGS_ACK_001"
PING_001 = "PING_001"
GOAWAY_001 = "GOAWAY_001"
FRAME_001 = "FRAME_001"
FRAME_STATE_001 = "FRAME_STATE_001"
FLOW_001 = "FLOW_001"
EXPECTED_STATE_001 = "EXPECTED_STATE_001"

# Patch validation.
PATCH_JSON_001 = "PATCH_JSON_001"
PATCH_SCHEMA_001 = "PATCH_SCHEMA_001"
PATCH_DIGEST_001 = "PATCH_DIGEST_001"
PATCH_REF_001 = "PATCH_REF_001"
PATCH_VERSION_001 = "PATCH_VERSION_001"
PATCH_BASE_001 = "PATCH_BASE_001"
PATCH_CONFLICT_001 = "PATCH_CONFLICT_001"
PATCH_OP_001 = "PATCH_OP_001"
PATCH_REPLAY_001 = "PATCH_REPLAY_001"
PATCH_REPLACE_001 = "PATCH_REPLACE_001"
PATCH_SETTINGS_001 = "PATCH_SETTINGS_001"

# Commit / recovery.
CONNECTION_EXISTS_001 = "CONNECTION_EXISTS_001"
CONNECTION_NOT_FOUND_001 = "CONNECTION_NOT_FOUND_001"
IDEMPOTENCY_001 = "IDEMPOTENCY_001"
DB_001 = "DB_001"
LOCK_001 = "LOCK_001"
RESUME_001 = "RESUME_001"
VERIFY_001 = "VERIFY_001"
