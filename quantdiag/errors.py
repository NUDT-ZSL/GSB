from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_STALE_RESULT = "E_STALE_RESULT"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"


@dataclass
class DiagnosticError(Exception):
    code: str
    message: str
    format_version: str | None = None
    field: str | None = None
    layer: str | None = None
    source_version: Any = None
    current_version: Any = None
    details: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        for name in (
            "format_version",
            "field",
            "layer",
            "source_version",
            "current_version",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        if self.details:
            payload["details"] = self.details
        return payload


def format_error(
    *,
    format_version: str | None,
    field: str,
    message: str,
    line: int | None = None,
) -> DiagnosticError:
    details = {"line": line} if line is not None else {}
    return DiagnosticError(
        E_FORMAT_CORRUPT,
        message,
        format_version=format_version,
        field=field,
        details=details,
    )

