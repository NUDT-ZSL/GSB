"""Shared operation and load result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from .codes import Code, Diagnostic
from .models import Utterance

T = TypeVar("T")


@dataclass
class LoadResult:
    format_name: str
    format_version: str | None = None
    utterances: list[Utterance] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    source: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    @property
    def code(self) -> str:
        for item in self.diagnostics:
            if item.severity == "error":
                return item.code
        return Code.OK

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "format": self.format_name,
            "format_version": self.format_version,
            "source": self.source,
            "utterance_count": len(self.utterances),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "details": self.details,
        }


@dataclass
class OperationResult(Generic[T]):
    ok: bool
    code: str = Code.OK
    data: T | None = None
    diagnostic: Diagnostic | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "diagnostic": self.diagnostic.to_dict() if self.diagnostic else None,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }
