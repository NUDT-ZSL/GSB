from __future__ import annotations

import os


def crash_point(name: str) -> None:
    requested = os.environ.get("WADIAG_CRASH_POINT", "")
    if requested and requested == name:
        os._exit(137)
