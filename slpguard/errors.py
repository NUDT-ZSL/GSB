"""Stable SLP business error codes.

The integer IDs are part of the on-disk sample format (truth masks) and must
never be renumbered.  New codes may only be appended.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ErrorCode(IntEnum):
    E_OK = 0
    E_MAGIC = 1
    E_VERSION = 2
    E_FORMAT = 3
    E_BOUNDS = 4
    E_PHASE = 5
    E_EXT = 6
    E_TRANSITION = 7
    E_AFTER_CLOSE = 8
    E_INCOMPLETE = 9
    E_MIXUP = 10
    E_STORM = 11
    E_STEPS = 12
    E_TIMEOUT = 13
    E_LOCK = 14
    E_IO = 15
    E_ARG = 16
    E_CHECKPOINT = 17
    E_TRUTH = 18


ERROR_CODE_BY_NAME = {code.name: code for code in ErrorCode}
ERROR_CODE_BY_ID = {int(code): code for code in ErrorCode}


def code_from_id(value: int) -> ErrorCode | None:
    """Return a code for a stable numeric ID, or None for an unknown ID."""

    return ERROR_CODE_BY_ID.get(int(value))


def mask_for(*codes: ErrorCode) -> int:
    mask = 0
    for code in codes:
        mask |= 1 << int(code)
    return mask


def codes_in_mask(mask: int) -> tuple[ErrorCode, ...]:
    result: list[ErrorCode] = []
    for code in ErrorCode:
        if code is ErrorCode.E_OK:
            continue
        if mask & (1 << int(code)):
            result.append(code)
    return tuple(result)


KNOWN_TRUTH_MASK = mask_for(*(code for code in ErrorCode if code is not ErrorCode.E_OK))


class SLPError(Exception):
    """A deterministic business failure with an optional byte offset."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        offset: int | None = None,
        session_id: int | None = None,
        sequence: int | None = None,
        sync_ids: tuple[int, ...] = (),
    ) -> None:
        super().__init__(f"{code.name}: {detail}")
        self.code = code
        self.detail = detail
        self.offset = offset
        self.session_id = session_id
        self.sequence = sequence
        self.sync_ids = sync_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.name,
            "code_id": int(self.code),
            "detail": self.detail,
            "offset": self.offset,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "sync_ids": list(self.sync_ids),
        }


class ResourceStop(Exception):
    """Raised by the resource meter to stop one file without affecting peers."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        processed_messages: int,
        total_messages: int | None = None,
        session_id: int | None = None,
        sequence: int | None = None,
    ) -> None:
        super().__init__(f"{code.name}: {detail}")
        self.code = code
        self.detail = detail
        self.processed_messages = processed_messages
        self.total_messages = total_messages
        self.session_id = session_id
        self.sequence = sequence

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.name,
            "code_id": int(self.code),
            "detail": self.detail,
            "processed_messages": self.processed_messages,
            "total_messages": self.total_messages,
            "session_id": self.session_id,
            "sequence": self.sequence,
        }
