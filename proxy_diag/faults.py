"""Deterministic crash hooks used only by verification."""

from __future__ import annotations

import os
import signal


def maybe_kill(point: str) -> None:
    """Kill the current process with SIGKILL when a test requests this point."""

    requested = os.environ.get("PROXY_DIAG_KILL_AT", "")
    if requested and requested == point:
        os.kill(os.getpid(), signal.SIGKILL)
