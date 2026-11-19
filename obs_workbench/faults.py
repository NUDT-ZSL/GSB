from __future__ import annotations

import os


def failpoint(name: str) -> None:
    """Deterministic crash injection used only by recovery tests.

    The process is terminated without unwinding Python context managers, which
    mirrors SIGKILL and lets SQLite roll back any open transaction.
    """

    configured = os.environ.get("OBS_FAILPOINT", "")
    if configured == name:
        os._exit(97)
