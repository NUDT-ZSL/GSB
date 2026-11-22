from __future__ import annotations

from typing import Any


E_INPUT_INVALID = "E_INPUT_INVALID"
E_DUP_SAMPLE = "E_DUP_SAMPLE"
E_LANG_INVALID = "E_LANG_INVALID"
E_TEMPLATE_INVALID = "E_TEMPLATE_INVALID"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_ORDER = "E_STAGE_ORDER"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_STATE_INCONSISTENT = "E_STATE_INCONSISTENT"
E_EXPORT_VERIFY = "E_EXPORT_VERIFY"


class BusinessError(Exception):
    """A stable, machine-readable application error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        stage: str | None = None,
        sample_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.stage = stage
        self.sample_id = sample_id
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            result["field"] = self.field
        if self.stage is not None:
            result["stage"] = self.stage
        if self.sample_id is not None:
            result["sample_id"] = self.sample_id
        if self.details:
            result["details"] = self.details
        return result

    def __str__(self) -> str:
        suffix = f" field={self.field}" if self.field else ""
        if self.stage:
            suffix += f" stage={self.stage}"
        if self.sample_id:
            suffix += f" sample_id={self.sample_id}"
        if self.details.get("version") is not None:
            suffix += f" version={self.details['version']}"
        return f"{self.code}: {self.message}{suffix}"
