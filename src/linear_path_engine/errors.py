"""Structured business errors shared by every engine layer."""

from __future__ import annotations

from typing import Any


E_CONFIG = "E_CONFIG"
E_DATA = "E_DATA"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_MONOTONICITY = "E_MONOTONICITY"
E_OBJECTIVE_MISMATCH = "E_OBJECTIVE_MISMATCH"
E_VERSION_MISMATCH = "E_VERSION_MISMATCH"
E_WORKER = "E_WORKER"
E_STATE = "E_STATE"
E_ARGUMENT = "E_ARGUMENT"


class EngineError(Exception):
    """An error that can cross process boundaries without losing attribution."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        version: int | None = None,
        strength: float | None = None,
        strength_index: int | None = None,
        block_index: int | None = None,
        chunk_index: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.version = version
        self.strength = strength
        self.strength_index = strength_index
        self.block_index = block_index
        self.chunk_index = chunk_index
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        optional = {
            "stage": self.stage,
            "version": self.version,
            "strength": self.strength,
            "strength_index": self.strength_index,
            "block_index": self.block_index,
            "chunk_index": self.chunk_index,
        }
        for key, value in optional.items():
            if value is not None:
                result[key] = value
        if self.details:
            result["details"] = self.details
        return result

    def clone_with(self, **updates: Any) -> "EngineError":
        values: dict[str, Any] = {
            "stage": self.stage,
            "version": self.version,
            "strength": self.strength,
            "strength_index": self.strength_index,
            "block_index": self.block_index,
            "chunk_index": self.chunk_index,
            "details": dict(self.details),
        }
        values.update(updates)
        return EngineError(self.code, self.message, **values)


def exit_code_for(error: EngineError) -> int:
    return {
        E_CONFIG: 10,
        E_DATA: 11,
        E_RESOURCE_LIMIT: 12,
        E_STAGE_BUSY: 13,
        E_SNAPSHOT_CORRUPT: 14,
        E_SNAPSHOT_ROLLBACK: 15,
        E_MONOTONICITY: 16,
        E_OBJECTIVE_MISMATCH: 17,
        E_VERSION_MISMATCH: 18,
        E_WORKER: 19,
        E_STATE: 20,
        E_ARGUMENT: 21,
    }.get(error.code, 1)

