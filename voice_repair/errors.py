from __future__ import annotations

from dataclasses import dataclass


EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_TASK_INVALID = 2
EXIT_UNSCHEDULABLE = 3
EXIT_LEDGER = 4
EXIT_GRACEFUL_INTERRUPT = 130
EXIT_FORCED_TEST_TERMINATION = 137


@dataclass(frozen=True)
class BusinessIssue:
    recording_id: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "recording_id": self.recording_id,
            "code": self.code,
            "message": self.message,
        }


class TaskValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LedgerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RepairError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UnschedulableError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GracefulInterruption(Exception):
    def __init__(self, signal_number: int | None) -> None:
        super().__init__(f"interrupted by signal {signal_number}")
        self.signal_number = signal_number
