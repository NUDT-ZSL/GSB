from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    E_DUP_DOC = "E_DUP_DOC"
    E_LINE_NO = "E_LINE_NO"
    E_TEXT_EMPTY = "E_TEXT_EMPTY"
    E_DICT_FORMAT = "E_DICT_FORMAT"
    E_LABEL_INVALID = "E_LABEL_INVALID"
    E_OFFSET_LEN = "E_OFFSET_LEN"
    E_UNATTRIBUTED = "E_UNATTRIBUTED"
    E_INCONSISTENT = "E_INCONSISTENT"
    E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    E_STAGE_BUSY = "E_STAGE_BUSY"
    E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    E_STAGE_INCOMPLETE = "E_STAGE_INCOMPLETE"
    E_STAGE_ORDER = "E_STAGE_ORDER"
    E_STAGE_CONFLICT = "E_STAGE_CONFLICT"
    E_INPUT_FORMAT = "E_INPUT_FORMAT"
    E_DATA_CORRUPT = "E_DATA_CORRUPT"
    E_PROCESS_TERMINATED = "E_PROCESS_TERMINATED"
    OK = "OK"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    doc_id: str | None = None
    line_no: int | None = None
    stage: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "doc_id": self.doc_id,
            "line_no": self.line_no,
            "stage": self.stage,
        }
        if self.details:
            result["details"] = self.details
        return result


class ErrorCollector:
    def __init__(self) -> None:
        self.errors: list[BusinessError] = []

    def add(
        self,
        code: str,
        message: str,
        *,
        doc_id: str | None = None,
        line_no: int | None = None,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> BusinessError:
        error = BusinessError(
            code=code,
            message=message,
            doc_id=doc_id,
            line_no=line_no,
            stage=stage,
            details=details or {},
        )
        self.errors.append(error)
        return error

    def extend(self, errors: list[BusinessError]) -> None:
        self.errors.extend(errors)

    def has_errors(self) -> bool:
        return bool(self.errors)

    def primary_code(self) -> str:
        return self.errors[0].code if self.errors else ErrorCode.OK

    def to_list(self) -> list[dict[str, Any]]:
        return [error.to_dict() for error in self.errors]
