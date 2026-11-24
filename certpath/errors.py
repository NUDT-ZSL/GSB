"""Stable business errors and rejection records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CertPathError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class Rejection:
    file_id: str
    record_index: int
    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "file_id": self.file_id,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            result["field"] = self.field
        return result


def reject(
    file_id: str,
    record_index: int,
    code: str,
    message: str,
    field: str | None = None,
) -> Rejection:
    return Rejection(file_id, record_index, code, message, field)


E_SCHEMA = "E_SCHEMA"
E_PARSE = "E_PARSE"
E_ID = "E_ID"
E_DUP_CERT_ID = "E_DUP_CERT_ID"
E_ISSUER_UNRESOLVED = "E_ISSUER_UNRESOLVED"
E_TIME_TYPE = "E_TIME_TYPE"
E_TIME_WINDOW = "E_TIME_WINDOW"
E_FINGERPRINT = "E_FINGERPRINT"
E_BOOL = "E_BOOL"
E_V1_CLOCK = "E_V1_CLOCK"
E_V1_CONSTRAINT = "E_V1_CONSTRAINT"
E_ROOT_REF_UNRESOLVED = "E_ROOT_REF_UNRESOLVED"
E_ROOT_REF_NOT_ROOT = "E_ROOT_REF_NOT_ROOT"
E_DUP_ROOT_REF = "E_DUP_ROOT_REF"
E_V2_META = "E_V2_META"
E_GENERATION = "E_GENERATION"
E_PATH_LENGTH = "E_PATH_LENGTH"
E_DUP_ANCHOR_ID = "E_DUP_ANCHOR_ID"
E_SHARD_COUNT = "E_SHARD_COUNT"
E_SHARD_DIGEST = "E_SHARD_DIGEST"
E_CHAIN_FORMAT = "E_CHAIN_FORMAT"
E_CHAIN_LIMIT = "E_CHAIN_LIMIT"
E_DUP_CHAIN_ID = "E_DUP_CHAIN_ID"
E_LOCK = "E_LOCK"
E_SNAPSHOT_INVALID = "E_SNAPSHOT_INVALID"
E_NOT_FOUND = "E_NOT_FOUND"
E_CHECKPOINT = "E_CHECKPOINT"
E_ARGS = "E_ARGS"

E_ISSUER_BREAK = "E_ISSUER_BREAK"
E_MISSING_INTERMEDIATE = "E_MISSING_INTERMEDIATE"
E_ROOT_UNTRUSTED = "E_ROOT_UNTRUSTED"
E_ROOT_REVOKED = "E_ROOT_REVOKED"
E_TRUST_CONFLICT = "E_TRUST_CONFLICT"
E_SIGNATURE_MISMATCH = "E_SIGNATURE_MISMATCH"
E_CERT_EXPIRED = "E_CERT_EXPIRED"
E_CERT_NOT_YET_VALID = "E_CERT_NOT_YET_VALID"
E_LEAF_REVOKED = "E_LEAF_REVOKED"
E_INTERMEDIATE_REVOKED = "E_INTERMEDIATE_REVOKED"
E_CHAIN_CYCLE = "E_CHAIN_CYCLE"
W_PATH_ENUM_TRUNCATED = "W_PATH_ENUM_TRUNCATED"
W_PATH_ENUM_CAPPED = "W_PATH_ENUM_CAPPED"
