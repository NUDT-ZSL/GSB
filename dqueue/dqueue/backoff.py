"""Exponential backoff with deterministic, reproducible jitter."""

import numpy as np


def _stable_unit_noise(key):
    """Map (task_id, attempt) to a deterministic value in [0, 1).

    A fixed-seed NumPy PCG64 generator keyed by a stable integer hash of the
    pair makes the noise independent of Python's per-process hash seed and
    reproducible across runs and machines.
    """
    raw = key.encode("utf-8")
    padded = np.pad(np.frombuffer(raw, dtype=np.uint8),
                    (0, max(0, 16 - len(raw))), mode="constant")
    seed = int(np.frombuffer(padded[:16].tobytes(), dtype=np.uint64)[0])
    rng = np.random.Generator(np.random.PCG64(seed))
    return float(rng.random())


def backoff_delay_ms(policy, attempt, task_id):
    """Delay before retry ``attempt`` (attempt 1 is the first retry).

    raw   = min(cap_ms, base_ms * multiplier ** (attempt - 1))
    jitter is symmetric within +/- jitter_pct percent of raw; the result is
    always >= 1 ms and capped at cap_ms.  Same task + same attempt always
    yields the same integer delay.
    """
    if attempt < 1:
        raise ValueError("retry attempt must be >= 1")
    raw = min(
        int(policy["cap_ms"]),
        int(round(policy["base_ms"] *
                  float(policy["multiplier"]) ** (attempt - 1))),
    )
    pct = policy["jitter_pct"] / 100.0
    noise = _stable_unit_noise(f"{policy['label']}|{task_id}|{attempt}")
    factor = 1.0 + (noise * 2.0 - 1.0) * pct
    delay = int(round(raw * factor))
    return max(1, min(int(policy["cap_ms"]), delay))


def next_fire_ms(task_id, policy, attempt, now_ms):
    """Deterministic next trigger clock for a failed task."""
    return int(now_ms) + backoff_delay_ms(policy, attempt, task_id)
