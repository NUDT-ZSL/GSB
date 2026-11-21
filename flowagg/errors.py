from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class FlowAggError(Exception):
    code: str
    message: str
    exit_code: int = 2
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_json(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "details": self.details,
                "message": self.message,
            },
            "exit_code": self.exit_code,
            "ok": False,
        }


ERRORS: dict[str, tuple[int, str]] = {
    "E_ARGUMENT": (2, "invalid command line argument"),
    "E_BATCH_TOO_LARGE": (2, "batch exceeds max_batch_rows"),
    "E_BATCH_EMPTY": (2, "batch contains no records"),
    "E_CLOCK_REGRESSION": (4, "logical clock regression exceeds threshold"),
    "E_CONCURRENCY": (6, "concurrent state conflict"),
    "E_DB": (7, "SQLite persistence failure"),
    "E_DIGEST_MISMATCH": (4, "batch digest does not match committed digest"),
    "E_DIRECTION_INVALID": (2, "direction is outside the closed enum"),
    "E_ENDPOINT_INVALID": (2, "endpoint is not a valid loopback endpoint"),
    "E_INTERNAL": (7, "unexpected internal failure"),
    "E_LOCK_TIMEOUT": (6, "state file lock could not be acquired"),
    "E_MEMORY_BUDGET": (5, "active group table cannot fit within budget"),
    "E_NAN_OR_OVERFLOW": (2, "NaN or numeric overflow is not permitted"),
    "E_NO_DATA": (3, "no committed aggregate data"),
    "E_OVERFLOW": (2, "signed 64-bit aggregate overflow"),
    "E_PLAN_INVALID": (2, "aggregation plan is invalid"),
    "E_PLAN_MISMATCH": (4, "plan does not match the initialized store"),
    "E_PROTOCOL_INVALID": (2, "protocol is outside the closed enum"),
    "E_READ_BUFFER_DEFERRED": (5, "read buffer was exhausted and readback was delayed"),
    "E_RECORD_INVALID": (2, "traffic record is invalid"),
    "E_RECORD_SEQ": (2, "record sequence is not strictly increasing"),
    "E_SAMPLE_MISSING": (2, "sample or batch file is missing"),
    "E_SINGLE_GROUP_TOO_LARGE": (5, "one group state cannot fit in total memory budget"),
    "E_SPILL_CONFLICT": (6, "spill path was already materialized by another writer"),
    "E_SPILL_TAMPERED": (3, "spill file digest or metadata verification failed"),
    "E_STATE_CORRUPT": (3, "persisted aggregation state is inconsistent"),
    "E_VERIFY_MISMATCH": (3, "aggregate state differs from full truth"),
}


def fail(code: str, message: str | None = None, **details: Any) -> FlowAggError:
    exit_code, default_message = ERRORS.get(code, (7, "business failure"))
    return FlowAggError(
        code=code,
        message=message or default_message,
        exit_code=exit_code,
        details=details,
    )

