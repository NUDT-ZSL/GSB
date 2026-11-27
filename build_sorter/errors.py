"""Business errors for the external build-task sorter."""

from __future__ import annotations

from typing import Any


class SorterError(Exception):
    """An error with a stable machine-readable code and CLI exit status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = 2,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class RecordTooLargeError(SorterError):
    def __init__(self, *, input_seq: int, record_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            "RECORD_TOO_LARGE",
            (
                "a single serialized input record exceeds the memory limit; "
                "refusing to split or truncate it"
            ),
            exit_code=3,
            details={
                "input_seq": input_seq,
                "record_bytes": record_bytes,
                "mem_limit_bytes": limit_bytes,
            },
        )


class SegmentIntegrityError(SorterError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            "SEGMENT_INTEGRITY_ERROR",
            message,
            exit_code=4,
            details=details,
        )


class MergeMemoryError(SorterError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            "MERGE_MEMORY_EXCEEDED",
            message,
            exit_code=3,
            details=details,
        )


class ResumeError(SorterError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            "RESUME_ERROR",
            message,
            exit_code=4,
            details=details,
        )
