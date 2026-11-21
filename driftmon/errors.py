"""Stable business error codes used by the CLI, API, and tests."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_CHECKSUM_MISMATCH = "E_CHECKSUM_MISMATCH"
E_BATCH_INVALID = "E_BATCH_INVALID"
E_STATE_INVALID = "E_STATE_INVALID"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"


@dataclass
class AppError(Exception):
    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        return payload


def config_invalid(field: str, message: str, **details: Any) -> AppError:
    return AppError(E_CONFIG_INVALID, message, field=field, details=details)


def resource_limit(field: str, message: str, **details: Any) -> AppError:
    return AppError(E_RESOURCE_LIMIT, message, field=field, details=details)


def lineage_broken(missing: list[str], message: str = "告警追溯链不完整") -> AppError:
    return AppError(
        E_LINEAGE_BROKEN,
        message,
        details={"missing": sorted(set(missing))},
    )

