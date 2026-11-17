"""Observable monotonic clock used for real elapsed-time measurement.

Budget *decisions* use the deterministic billed-cost ledger (see cost.py)
so that results are reproducible across concurrency levels; the monotonic
clock readings recorded here provide the real, observable elapsed time of
every task for audit. Termination is never faked via thread timeouts.
"""

import time


def ensure_monotonic():
    info = time.get_clock_info("monotonic")
    if not info.monotonic:
        raise RuntimeError("system monotonic clock is not available")
    return info


def monotonic_ns():
    return time.monotonic_ns()


def elapsed_ms(start_ns, end_ns):
    return (end_ns - start_ns) / 1_000_000.0
