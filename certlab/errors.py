from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    BAD_SNAPSHOT = "E_BAD_SNAPSHOT"
    HASH_MISMATCH = "E_HASH_MISMATCH"
    GENERATION_ROLLBACK = "E_GENERATION_ROLLBACK"
    CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
    CERT_EXPIRED = "E_CERT_EXPIRED"
    CERT_NOT_YET_VALID = "E_CERT_NOT_YET_VALID"
    SIGNATURE_INVALID = "E_SIGNATURE_INVALID"
    BASIC_CONSTRAINTS = "E_BASIC_CONSTRAINTS"
    PATH_LENGTH = "E_PATH_LENGTH"
    KEY_USAGE = "E_KEY_USAGE"
    HOSTNAME_MISMATCH = "E_HOSTNAME_MISMATCH"
    REVOCATION_UNKNOWN = "E_REVOCATION_UNKNOWN"
    REVOKED = "E_REVOKED"
    NO_TRUST_PATH = "E_NO_TRUST_PATH"
    ANCHOR_NOT_EFFECTIVE = "E_ANCHOR_NOT_EFFECTIVE"
    PATCH_CONFLICT = "E_PATCH_CONFLICT"
    BAD_PATCH = "E_BAD_PATCH"
    INVALID_ARGUMENT = "E_INVALID_ARGUMENT"
    NO_STATE = "E_NO_STATE"
    JOB_NOT_FOUND = "E_JOB_NOT_FOUND"
    DB_ERROR = "E_DB_ERROR"
    VALIDATION_INTERNAL = "E_VALIDATION_INTERNAL"
    INTERNAL = "E_INTERNAL"


@dataclass
class CertLabError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def as_error_dict(exc: CertLabError) -> dict[str, Any]:
    return exc.to_dict()

