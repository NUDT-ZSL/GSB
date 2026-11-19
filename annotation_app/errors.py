"""Structured business errors used across every application path."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping


class ErrorCode:
    SCHEMA_MISSING = "E_SCHEMA_MISSING"
    SCHEMA_TYPE = "E_SCHEMA_TYPE"
    OFFSET_TYPE = "E_OFFSET_TYPE"
    SPAN_INVALID = "E_SPAN_INVALID"
    DUP_ID = "E_DUP_ID"
    FORMAT_INVALID = "E_FORMAT_INVALID"
    VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    ROUNDTRIP_MISMATCH = "E_ROUNDTRIP_MISMATCH"
    DANGLING_REF = "E_DANGLING_REF"
    ROLE_INVALID = "E_ROLE_INVALID"
    TRIGGER_CONFLICT = "E_TRIGGER_CONFLICT"
    COREF_UNRESOLVED = "E_COREF_UNRESOLVED"
    ONTOLOGY_CONTRACT = "E_ONTOLOGY_CONTRACT"
    INCONSISTENT = "E_INCONSISTENT"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"


@dataclass(frozen=True)
class ErrorLocation:
    """Location data needed to attribute an error to a concrete artifact."""

    stage: str | None = None
    format: str | None = None
    version: str | None = None
    field_path: str | None = None
    doc_id: str | None = None
    annotation_id: str | None = None
    line: int | None = None

    def merge(self, **changes: Any) -> "ErrorLocation":
        clean = {key: value for key, value in changes.items() if value is not None}
        return replace(self, **clean)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "stage": self.stage,
                "format": self.format,
                "version": self.version,
                "field_path": self.field_path,
                "doc_id": self.doc_id,
                "annotation_id": self.annotation_id,
                "line": self.line,
            }.items()
            if value is not None
        }


class AppError(Exception):
    """A business error that is safe to serialize and display."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        location: ErrorLocation | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        loc = location or ErrorLocation()
        if path is not None:
            loc = loc.merge(field_path=path)
        self.location = loc
        self.details = dict(details or {})
        self.cause = cause

    def attributed(self, **location: Any) -> "AppError":
        """Return the same business error with enriched attribution."""

        self.location = self.location.merge(**location)
        return self

    @property
    def attributed_ok(self) -> bool:
        return bool(self.location.stage or self.location.format or self.location.field_path)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "location": self.location.to_dict(),
            "details": self.details,
            "attributed": self.attributed_ok,
        }
        if self.cause is not None:
            result["cause"] = f"{type(self.cause).__name__}: {self.cause}"
        return result


def fail(
    code: str,
    message: str,
    *,
    path: str | None = None,
    stage: str | None = None,
    format_name: str | None = None,
    version: str | None = None,
    doc_id: str | None = None,
    annotation_id: str | None = None,
    line: int | None = None,
    details: Mapping[str, Any] | None = None,
    cause: BaseException | None = None,
) -> None:
    raise AppError(
        code,
        message,
        path=path,
        location=ErrorLocation(
            stage=stage,
            format=format_name,
            version=version,
            doc_id=doc_id,
            annotation_id=annotation_id,
            line=line,
        ),
        details=details,
        cause=cause,
    )


def unexpected_error(exc: BaseException, *, stage: str) -> dict[str, Any]:
    """Report an exception that has no business attribution."""

    return {
        "code": "E_UNATTRIBUTED",
        "message": f"{type(exc).__name__}: {exc}",
        "attributed": False,
        "stage": stage,
        "location": {},
    }

