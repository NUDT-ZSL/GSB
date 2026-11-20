from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


class ErrorCode:
    INVALID_CONFIG = "E_INVALID_CONFIG"
    INVALID_WEIGHT = "E_INVALID_WEIGHT"
    INVALID_SMOOTHING = "E_INVALID_SMOOTHING"
    UNKNOWN_PRECISION = "E_UNKNOWN_PRECISION"
    INVALID_SCALE = "E_INVALID_SCALE"
    INVALID_ROUNDING = "E_INVALID_ROUNDING"
    DUPLICATE_DOC_ID = "E_DUPLICATE_DOC_ID"
    EMPTY_TEXT = "E_EMPTY_TEXT"
    MISSING_FIELD = "E_MISSING_FIELD"
    INVALID_FIELD = "E_INVALID_FIELD"
    INVALID_SENT_NO = "E_INVALID_SENT_NO"
    EMPTY_SELECTION = "E_EMPTY_SELECTION"
    UNATTRIBUTED = "E_UNATTRIBUTED"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    INCONSISTENT = "E_INCONSISTENT"


@dataclass
class PipelineError(Exception):
    code: str
    stage: str
    doc_id: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "doc_id": self.doc_id,
            "message": self.message,
            "detail": self.detail,
        }


def sort_errors(errors: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        errors,
        key=lambda item: (
            str(item.get("doc_id", "")),
            str(item.get("stage", "")),
            str(item.get("code", "")),
            str(item.get("message", "")),
        ),
    )

