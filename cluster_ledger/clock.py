"""Clock bases used throughout the diagnostic console."""

from __future__ import annotations

import time


CLIENT_CLOCK_BASIS = "Python time.perf_counter_ns (per-process monotonic origin)"
SERVER_CLOCK_BASIS = "Python time.perf_counter_ns (independent per-service-process monotonic origin)"
LEDGER_CLOCK_BASIS = "int milliseconds floored from the client process monotonic clock"
LATENCY_FIXED_SCALE = 1000
LATENCY_CALIBER_VERSION = "latency-mono-us-to-fixed-ms-v1"
LATENCY_QUANTIZATION_ERROR_MS = 0.001


def mono_ns() -> int:
    return time.perf_counter_ns()


def mono_ms() -> int:
    return time.perf_counter_ns() // 1_000_000


def fixed_ms_from_ns(duration_ns: int) -> int:
    """Represent milliseconds as integer thousandths (one microsecond)."""

    return duration_ns // 1_000

