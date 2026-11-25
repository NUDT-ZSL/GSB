from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


BUSINESS_CODES = {
    "SHARD_MISSING": "MWB-1001",
    "SHARD_DIGEST_MISMATCH": "MWB-1002",
    "LAYER_REFERENCE_MISSING": "MWB-1003",
    "CONFIG_REFERENCE_CYCLE": "MWB-1004",
    "GRAY_RATIO_INVALID": "MWB-1005",
    "CLOCK_ROLLBACK": "MWB-1006",
    "VALIDATION_ERROR": "MWB-1100",
    "VERSION_DUPLICATE": "MWB-1101",
    "VERSION_ORDER_INVALID": "MWB-1102",
    "CONFIG_REFERENCE_MISSING": "MWB-1103",
    "DEPENDENCY_REFERENCE_MISSING": "MWB-1104",
    "DEPENDENCY_TOMBSTONED": "MWB-1105",
    "TIME_CONVERSION_INVALID": "MWB-1106",
    "PATCH_FULL_REPLACEMENT_MISMATCH": "MWB-1201",
    "PATCH_UNDECLARED_SHARD_CHANGE": "MWB-1202",
    "PATCH_DIGEST_MISMATCH": "MWB-1203",
    "INVALID_STATE": "MWB-1301",
    "NOT_FOUND": "MWB-1404",
    "CONFLICT": "MWB-1409",
    "IO_ERROR": "MWB-1500",
}

HTTP_STATUS = {
    "SHARD_MISSING": 422,
    "SHARD_DIGEST_MISMATCH": 422,
    "LAYER_REFERENCE_MISSING": 422,
    "CONFIG_REFERENCE_CYCLE": 422,
    "GRAY_RATIO_INVALID": 422,
    "CLOCK_ROLLBACK": 409,
    "VALIDATION_ERROR": 422,
    "VERSION_DUPLICATE": 422,
    "VERSION_ORDER_INVALID": 422,
    "CONFIG_REFERENCE_MISSING": 422,
    "DEPENDENCY_REFERENCE_MISSING": 422,
    "DEPENDENCY_TOMBSTONED": 422,
    "TIME_CONVERSION_INVALID": 422,
    "PATCH_FULL_REPLACEMENT_MISMATCH": 422,
    "PATCH_UNDECLARED_SHARD_CHANGE": 422,
    "PATCH_DIGEST_MISMATCH": 422,
    "INVALID_STATE": 409,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "IO_ERROR": 500,
}


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    diagnostics: dict[str, Any] = field(default_factory=dict)
    http_status: int | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.http_status is None:
            self.http_status = HTTP_STATUS.get(self.code, 400)

    @property
    def business_code(self) -> str:
        return BUSINESS_CODES.get(self.code, "MWB-1999")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "business_code": self.business_code,
                "message": self.message,
                "diagnostics": self.diagnostics,
            },
        }


def fail(
    code: str,
    message: str,
    *,
    http_status: int | None = None,
    **diagnostics: Any,
) -> None:
    raise WorkbenchError(
        code=code,
        message=message,
        diagnostics=diagnostics,
        http_status=http_status,
    )
