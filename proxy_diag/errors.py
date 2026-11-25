"""Business errors with stable ``E_*`` codes."""

from __future__ import annotations

import json
from typing import Any


class WorkbenchError(Exception):
    """An expected, machine-readable workbench failure."""

    exit_code = 2

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


E_SCHEMA = "E_SCHEMA"
E_V1_CONNECTION_DUPLICATE = "E_V1_CONNECTION_ID_NOT_UNIQUE"
E_V1_DIRECTION = "E_V1_DIRECTION_INVALID"
E_V1_SEQUENCE = "E_V1_SEQ_NOT_STRICTLY_INCREASING"
E_V1_RELATIVE_SECOND = "E_V1_RELATIVE_SECOND_INVALID"
E_V1_TABLE_MISMATCH = "E_V1_TABLE_ENTRY_MISMATCH"
E_V2_SHARD_HASH = "E_V2_SHARD_HASH_MISMATCH"
E_V2_GENERATION_ROLLBACK = "E_V2_GENERATION_ROLLBACK"
E_V2_ABSOLUTE_MS = "E_V2_ABSOLUTE_MS_INVALID"
E_V2_TABLE_VERSION = "E_V2_TABLE_VERSION_INVALID"
E_V2_TOMBSTONE = "E_V2_TOMBSTONE_INVALID"
E_VERSION_ROLLBACK = "E_VERSION_ROLLBACK"
E_PACKAGE_CONFLICT = "E_PACKAGE_CONTENT_CONFLICT"
E_PATCH_INVALID = "E_PATCH_INVALID"
E_STATE = "E_STATE_INVALID"
E_HTTP_REPLAY = "E_HTTP_REPLAY_FAILED"
E_LOCK = "E_LOCK_BUSY"
E_NOT_FOUND = "E_NOT_FOUND"
