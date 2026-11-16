"""Minimal integration example for the adaptive component."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gemm_ops import AdaptiveEngine, bmm_reference


def main() -> None:
    rng = np.random.default_rng(2026)
    batch, M, N, K = 4, 19, 23, 17
    A = rng.standard_normal((batch, M, K), dtype=np.float32)
    B = rng.standard_normal((batch, K, N), dtype=np.float32)
    C = rng.standard_normal((batch, M, N), dtype=np.float32)
    engine = AdaptiveEngine(max_workers=4)
    result = engine.adaptive_bmm(
        A,
        B,
        C,
        alpha=1.25,
        beta=0.5,
        cache_budget_bytes=256 * 1024,
    )
    decision = result.decision
    reference = bmm_reference(A, B, C, alpha=1.25, beta=0.5)
    max_abs = float(np.max(np.abs(result.output.astype(np.float64) - reference)))
    print("strategy:", decision["strategy_id"])
    print("tile:", decision["tile"])
    print("loop_order:", decision["loop_order"])
    print("batch_schedule:", decision["batch_schedule"])
    print("reason_code:", decision["reason_code"])
    print("cache_status:", decision["cache_status"])
    print("max_absolute_error_vs_float64:", max_abs)
    print("execution_statistics:", result.execution.statistics)


if __name__ == "__main__":
    main()
