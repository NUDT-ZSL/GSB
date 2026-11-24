from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Issue:
    field: str
    message: str
    code: str = "E_SCHEMA"

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


class FormBenchError(Exception):
    """Base error with a stable business code and field location."""

    code = "E_WORKBENCH"
    category = "E_WORKBENCH"
    exit_code = 1

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        reason_code: str | None = None,
        issues: list[Issue] | None = None,
        image_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.reason_code = reason_code or self.code
        self.issues = issues or []
        self.image_id = image_id
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "code": self.code,
            "category": self.category,
            "reason_code": self.reason_code,
            "message": self.message,
        }
        if self.field is not None:
            data["field"] = self.field
        if self.image_id is not None:
            data["image_id"] = self.image_id
        if self.issues:
            data["issues"] = [issue.to_dict() for issue in self.issues]
        if self.details:
            data["details"] = self.details
        return data


class SchemaError(FormBenchError):
    code = "E_SCHEMA"
    category = "E_SCHEMA"
    exit_code = 2


class ImageValidationError(FormBenchError):
    code = "E_IMAGE"
    category = "E_IMAGE"
    exit_code = 3


class CorruptPixelsError(ImageValidationError):
    code = "E_CORRUPT_PIXELS"
    reason_code = "E_CORRUPT_PIXELS"


class GridDivisibilityError(SchemaError):
    code = "E_GRID_NOT_DIVISIBLE"
    reason_code = "E_GRID_NOT_DIVISIBLE"


class VocabularyMissingError(FormBenchError):
    code = "E_VOCAB_MISSING"
    category = "E_VOCAB"
    exit_code = 1


class LedgerDigestError(FormBenchError):
    code = "E_LEDGER_DIGEST"
    category = "E_LEDGER"
    exit_code = 1


class MemoryBudgetError(FormBenchError):
    code = "E_MEMORY_BUDGET"
    category = "E_BUDGET"
    exit_code = 1


class InterruptedBySignal(FormBenchError):
    code = "E_INTERRUPTED"
    category = "E_INTERRUPT"
    exit_code = 130


class BlockProcessingError(FormBenchError):
    code = "E_BLOCK_PROCESSING"
    category = "E_PROCESSING"
    exit_code = 1
