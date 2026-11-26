from __future__ import annotations

from typing import Any


class ErrorCode:
    OK = "DIAG-0000"

    # Input validation: 1xxx
    MANIFEST_INVALID = "DIAG-1001"
    OBJECT_KEY_INVALID = "DIAG-1002"
    OBJECT_KEY_DUPLICATE = "DIAG-1003"
    CONTENT_VERSION_INVALID = "DIAG-1004"
    SIGNATURE_INVALID = "DIAG-1005"
    EVENT_STREAM_INVALID = "DIAG-1006"
    EVENT_SEQ_NOT_STRICT = "DIAG-1007"
    LOGICAL_CLOCK_NEGATIVE = "DIAG-1008"
    EVENT_OP_INVALID = "DIAG-1009"
    EVENT_KEY_UNDECLARED = "DIAG-1010"
    V1_SNAPSHOT_INVALID = "DIAG-1101"
    V1_RELATIVE_SECONDS_INVALID = "DIAG-1102"
    V1_FLAT_VALUE_INVALID = "DIAG-1103"
    V2_SNAPSHOT_INVALID = "DIAG-1201"
    V2_SHARD_HASH_MISMATCH = "DIAG-1202"
    V2_GENERATION_NOT_MONOTONIC = "DIAG-1203"
    V2_DATABASE_INVALID = "DIAG-1204"
    INJECTION_MANIFEST_INVALID = "DIAG-1301"
    INJECTION_CASE_UNKNOWN = "DIAG-1302"
    CLOCK_PROFILE_INVALID = "DIAG-1401"

    # Injected runtime faults: 2xxx
    FORCE_KILL = "DIAG-2001"
    WRITE_INTERRUPT = "DIAG-2002"
    GENERATION_ROLLBACK = "DIAG-2003"
    LOG_HALF_WRITTEN = "DIAG-2004"
    LOG_LOST = "DIAG-2005"
    SIGNATURE_MISMATCH = "DIAG-2006"
    UNSUPPORTED_STAGE = "DIAG-2007"

    # Ledger/concurrency/comparison: 3xxx
    LEDGER_ERROR = "DIAG-3001"
    CLAIM_CONFLICT = "DIAG-3002"
    REPLAY_CONFLICT = "DIAG-3003"
    DIVERGENCE = "DIAG-3004"
    VERIFY_FAILED = "DIAG-3005"
    STATE_NOT_FOUND = "DIAG-3006"

    # CLI/environment: 4xxx
    CLI_INVALID_ARGUMENT = "DIAG-4001"
    FIXTURES_NOT_FOUND = "DIAG-4002"


class DiagnosticError(Exception):
    """Business error carrying a stable code and evidence-safe details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        stage: str | None = None,
        object_key: str | None = None,
        logical_clock: int | None = None,
        expected_signature: str | None = None,
        injection_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.stage = stage
        self.object_key = object_key
        self.logical_clock = logical_clock
        self.expected_signature = expected_signature
        self.injection_id = injection_id

    def evidence(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.object_key is not None:
            payload["object_key"] = self.object_key
        if self.logical_clock is not None:
            payload["logical_clock"] = self.logical_clock
        if self.expected_signature is not None:
            payload["expected_signature"] = self.expected_signature
        if self.injection_id is not None:
            payload["injection_id"] = self.injection_id
        if self.details:
            payload["details"] = self.details
        return payload

    def to_cli_json(self, *, ok: bool = False) -> dict[str, Any]:
        return {"ok": ok, "error": self.evidence()}


class InjectedForceKill(DiagnosticError):
    """Raised only for in-process/hard-kill simulation mode."""


FAULT_ERROR_CODES = {
    "force_kill": ErrorCode.FORCE_KILL,
    "write_interrupt": ErrorCode.WRITE_INTERRUPT,
    "generation_rollback": ErrorCode.GENERATION_ROLLBACK,
    "log_lost": ErrorCode.LOG_LOST,
}


def fault_error_code(fault_type: str) -> str:
    return FAULT_ERROR_CODES.get(fault_type, ErrorCode.FORCE_KILL)
