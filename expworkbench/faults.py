from __future__ import annotations

import os


def maybe_fault(point: str) -> None:
    """Deterministic crash hook used by integration tests."""
    configured = os.environ.get("EXPWORKBENCH_FAULT")
    if configured == point:
        os._exit(86)

