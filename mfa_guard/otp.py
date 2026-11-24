"""Deterministic TOTP-like code derivation with explicit boundary uncertainty."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from .errors import Codes


def derive_code(shared_secret_digest: str, counter: int) -> int:
    if counter < 0:
        raise ValueError("counter must be non-negative")
    key = bytes.fromhex(shared_secret_digest.split(":", 1)[1])
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return value % 1_000_000


def format_code(code: int) -> str:
    return f"{code:06d}"


@dataclass(frozen=True)
class OtpEvaluation:
    result: str
    code: str | None
    supplied_code: int
    expected_codes: tuple[int, ...]
    primary_counter: int
    candidate_counters: tuple[int, ...]
    window_ms: int
    window_start_ms: int | None
    window_end_ms: int | None
    timestamp_error_us: int
    boundary_margin_us: int
    boundary_crossed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "code": self.code,
            "supplied_code": self.supplied_code,
            "expected_codes": [format_code(value) for value in self.expected_codes],
            "primary_counter": self.primary_counter,
            "candidate_counters": list(self.candidate_counters),
            "window_ms": self.window_ms,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "timestamp_error_us": self.timestamp_error_us,
            "timestamp_error_ms": self.timestamp_error_us / 1000,
            "boundary_margin_us": self.boundary_margin_us,
            "boundary_crossed": self.boundary_crossed,
        }


def evaluate_otp(
    shared_secret_digest: str,
    submitted_at_ms: int,
    timestamp_error_us: int,
    window_ms: int,
    supplied_code: int,
) -> OtpEvaluation:
    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    if submitted_at_ms < 0 or timestamp_error_us < 0:
        raise ValueError("time values must be non-negative")

    submitted_us = submitted_at_ms * 1000
    lower_us = max(0, submitted_us - timestamp_error_us)
    upper_us = submitted_us + timestamp_error_us
    window_us = window_ms * 1000
    first_counter = lower_us // window_us
    last_counter = upper_us // window_us
    counters = tuple(range(first_counter, last_counter + 1))
    expected = tuple(derive_code(shared_secret_digest, counter) for counter in counters)
    boundary_crossed = first_counter != last_counter

    primary = submitted_us // window_us
    distance_to_boundary = min(
        submitted_us - primary * window_us,
        (primary + 1) * window_us - submitted_us,
    )
    boundary_margin = distance_to_boundary - timestamp_error_us

    if supplied_code not in expected:
        result = "REJECT"
        code = Codes.OTP_MISMATCH
    elif boundary_crossed:
        result = "AMBIGUOUS"
        code = Codes.OTP_AMBIGUOUS
    else:
        result = "PASS"
        code = None

    return OtpEvaluation(
        result=result,
        code=code,
        supplied_code=supplied_code,
        expected_codes=expected,
        primary_counter=primary,
        candidate_counters=counters,
        window_ms=window_ms,
        window_start_ms=primary * window_ms,
        window_end_ms=(primary + 1) * window_ms,
        timestamp_error_us=timestamp_error_us,
        boundary_margin_us=boundary_margin,
        boundary_crossed=boundary_crossed,
    )

