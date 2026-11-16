"""Business errors shared by the replication lab.

The project deliberately uses stable business codes because they are part of
the observable contract used by the CLI, the Tkinter frontend and tests.
"""

from __future__ import annotations

from typing import Any


class LabError(Exception):
    """Base class for an error carrying a stable business code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        segment: str | None = None,
        offset: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.segment = segment
        self.offset = offset
        self.details = {
            "business_code": code,
            "message": message,
            "segment": segment,
            "offset": offset,
        }
        if details:
            self.details.update(details)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.details)

    def __str__(self) -> str:
        location = ""
        if self.segment is not None:
            location = f" segment={self.segment}"
        if self.offset is not None:
            location += f" offset={self.offset}"
        return f"{self.code}: {self.message}{location}"


class FormatError(LabError):
    """A malformed, truncated or unsupported log record/segment."""

    def __init__(
        self,
        message: str,
        *,
        segment: str | None = None,
        offset: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            "E_FORMAT",
            message,
            segment=segment,
            offset=offset,
            details=details,
        )


class ReplayError(LabError):
    """Replay or historical verification cannot reproduce the observation."""

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("E_REPLAY", message, details=details)


class StateError(LabError):
    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("E_STATE", message, details=details)


class ParameterError(LabError):
    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("E_PARAM", message, details=details)


class SnapshotError(LabError):
    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("E_SNAPSHOT", message, details=details)


class GuiUnavailableError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("E_GUI", message)

