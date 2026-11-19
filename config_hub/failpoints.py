"""Environment-controlled hard-stop hooks used by crash recovery tests."""

from __future__ import annotations

import os
import sys


def failpoint(name: str) -> None:
    """Exit the process immediately without unwinding SQLite transactions."""

    selected = os.environ.get("CONFIG_HUB_FAILPOINT", "")
    if selected == name:
        sys.stderr.write(f"CONFIG_HUB_FAILPOINT hit: {name}\n")
        sys.stderr.flush()
        os._exit(90)
