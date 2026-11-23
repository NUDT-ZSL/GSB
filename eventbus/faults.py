"""Test-only process termination points for recovery verification."""

from __future__ import annotations

import os
import sys
import time


def hit(point: str) -> None:
    if os.environ.get("EVENTBUS_FAULT_POINT") == point:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(86)


def hold_after_bind() -> None:
    raw = os.environ.get("EVENTBUS_TEST_BRIDGE_HOLD_MS")
    if raw:
        time.sleep(max(0, int(raw)) / 1000.0)
