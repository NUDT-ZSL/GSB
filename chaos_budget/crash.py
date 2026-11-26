from __future__ import annotations

import os
import signal


def maybe_kill(stage: str) -> None:
    """Deterministic SIGKILL hook used by crash-recovery tests.

    Set ``CHAOS_CRASH_STAGE`` to one of the named commit boundaries.  The
    process kills itself with SIGKILL, exercising both SQLite atomic rollback
    and kernel release of the advisory file lock.
    """

    wanted = os.environ.get("CHAOS_CRASH_STAGE")
    if wanted and wanted == stage:
        os.kill(os.getpid(), signal.SIGKILL)
