from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


BUSINESS_CODES = {
    "E_CONFIG_INVALID": "Configuration or metadata violates the analysis contract.",
    "E_CONFIG_MISMATCH": "Snapshot/configuration compatibility cannot be resumed safely.",
    "E_IMAGE_FORMAT": "PNG container, dimensions, pixel mode, or payload is invalid.",
    "E_INTERNAL": "Unexpected internal failure while processing one page.",
    "E_GRACEFUL_STOP": "A cooperative stop was requested; committed work is resumable.",
}


class EngineError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "engine",
        page: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.page = page
        self.details = details or {}

    def as_failure(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class ConfigError(EngineError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_CONFIG_INVALID", message, stage="config", details=details)


class ImageFormatError(EngineError):
    def __init__(self, message: str, *, page: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_IMAGE_FORMAT", message, stage="detect", page=page, details=details)


class SnapshotMismatch(EngineError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            "E_CONFIG_MISMATCH", message, stage="snapshot", details=details
        )


@dataclass
class FailureList:
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, failure: dict[str, Any]) -> None:
        self.items.append(failure)

    def sorted(self) -> list[dict[str, Any]]:
        return sorted(
            self.items,
            key=lambda item: (
                str(item.get("page") or ""),
                str(item.get("code") or ""),
                str(item.get("stage") or ""),
                str(item.get("message") or ""),
            ),
        )
