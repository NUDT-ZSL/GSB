"""Deterministic sample file generation.

The default sample is fixed at 1 MiB of reproducible bytes so digest values
and timing observations are repeatable across machines and runs.
"""

import os

DEFAULT_SAMPLE_SIZE = 1024 * 1024
DEFAULT_SAMPLE_NAME = "sample-1MiB.bin"


def deterministic_byte(index):
    """A fixed byte pattern with a 256-byte period and a slow ramp."""
    return (index * 31 + index // 256) & 0xFF


def generate_sample(path, size=DEFAULT_SAMPLE_SIZE):
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("sample size must be a non-negative integer")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    buf = bytearray(min(size, 64 * 1024))
    written = 0
    with open(path, "wb") as handle:
        while written < size:
            step = min(len(buf), size - written)
            for pos in range(step):
                buf[pos] = deterministic_byte(written + pos)
            handle.write(bytes(buf[:step]))
            written += step
    return path
