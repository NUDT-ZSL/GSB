"""Stable business error codes.

Every validation or runtime failure is reported with one of the codes below;
the CLI maps them to a non-zero process exit code and writes the code into the
JSON report instead of raising an unstructured traceback.
"""

from __future__ import annotations

from dataclasses import dataclass


class ErrorCode:
    E_CONFIG_INVALID = "E_CONFIG_INVALID"
    E_FRAME_MISMATCH = "E_FRAME_MISMATCH"
    E_SPEAKER_MISMATCH = "E_SPEAKER_MISMATCH"
    E_SPEAKER_UNASSIGNED = "E_SPEAKER_UNASSIGNED"
    E_INTERVAL_INVALID = "E_INTERVAL_INVALID"
    E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    E_INCONSISTENT = "E_INCONSISTENT"
    E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    E_DUPLICATE_DIALOG = "E_DUPLICATE_DIALOG"
    E_IO = "E_IO"


@dataclass
class BusinessError(Exception):
    """Raised for all expected, business-level failures."""

    code: str
    message: str
    dialog_id: str | None = None
    field: str | None = None
    details: dict | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        loc = ""
        if self.dialog_id is not None:
            loc = f"[dialog={self.dialog_id}] "
        fld = f"[field={self.field}] " if self.field else ""
        return f"{self.code}: {loc}{fld}{self.message}"

    def to_dict(self) -> dict:
        out = {
            "code": self.code,
            "message": self.message,
        }
        if self.dialog_id is not None:
            out["dialog_id"] = self.dialog_id
        if self.field is not None:
            out["field"] = self.field
        if self.details:
            out["details"] = self.details
        return out
