from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from typing import Any


@dataclass
class DiagError(Exception):
    code: str
    message: str
    line: int | None = None
    field: str | None = None
    detail: dict[str, Any] = dc_field(default_factory=dict)

    def __str__(self) -> str:
        location = []
        if self.line is not None:
            location.append(f"line={self.line}")
        if self.field is not None:
            location.append(f"field={self.field}")
        suffix = f" ({', '.join(location)})" if location else ""
        return f"{self.code}: {self.message}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.code,
            "message": self.message,
        }
        if self.line is not None:
            payload["line"] = self.line
        if self.field is not None:
            payload["field"] = self.field
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


class FormatCorrupt(DiagError):
    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        field: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            "E_FORMAT_CORRUPT",
            message,
            line=line,
            field=field,
            detail=detail or {},
        )


class CandidateDrop(DiagError):
    def __init__(self, message: str, *, doc_id: str | None = None) -> None:
        detail = {"doc_id": doc_id} if doc_id is not None else {}
        super().__init__("E_CANDIDATE_DROP", message, detail=detail)


class ResourceLimit(DiagError):
    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__("E_RESOURCE_LIMIT", message, detail=detail or {})


class StageBusy(DiagError):
    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__("E_STAGE_BUSY", message, detail=detail or {})


class ConfigurationError(DiagError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__("E_CONFIG", message, field=field)

