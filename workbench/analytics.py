from __future__ import annotations

from typing import Any

import numpy as np


def latency_summary(latencies_ms: list[int]) -> dict[str, Any]:
    if not latencies_ms:
        return {"count": 0, "min_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None, "mean_ms": None}
    values = np.asarray(latencies_ms, dtype=np.float64)
    return {
        "count": int(values.size),
        "min_ms": int(np.min(values)),
        "p50_ms": int(np.percentile(values, 50, method="linear")),
        "p95_ms": int(np.percentile(values, 95, method="linear")),
        "max_ms": int(np.max(values)),
        "mean_ms": round(float(np.mean(values)), 3),
    }
