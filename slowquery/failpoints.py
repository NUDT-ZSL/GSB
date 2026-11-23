"""Deterministic process termination hooks used by crash-recovery tests."""

from __future__ import annotations

import os


_counters: dict[str, int] = {}


def process_exit_at(name: str) -> None:
    raw = os.environ.get("SLOWQUERY_FAILPOINT", "")
    if not raw:
        return
    parts = raw.split(":", 1)
    if parts[0] != name:
        return
    target = int(parts[1]) if len(parts) == 2 else 1
    _counters[name] = _counters.get(name, 0) + 1
    if _counters[name] >= target:
        os._exit(86)
