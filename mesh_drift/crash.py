"""Test-only hard-kill injection points.

The hooks are dormant unless MESH_DRIFT_CRASH_POINT (and optionally
MESH_DRIFT_CRASH_AFTER_WRITES) is explicitly set in the environment.
"""

from __future__ import annotations

import os
import signal


def crash_after_writes() -> int | None:
    value = os.environ.get("MESH_DRIFT_CRASH_AFTER_WRITES")
    if value is None:
        return None
    return int(value)


def hard_kill(point: str) -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def maybe_crash(point: str) -> None:
    if os.environ.get("MESH_DRIFT_CRASH_POINT") == point:
        hard_kill(point)
