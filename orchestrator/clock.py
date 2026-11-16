"""Deterministic dual clock: monotonic milliseconds plus wall-clock ISO time."""
from __future__ import annotations

import time
from datetime import datetime, timezone


class Clock:
    def mono_ms(self) -> int:
        return int(round(time.monotonic() * 1000))

    def iso_now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class FrozenClock(Clock):
    """Injection point for tests."""

    def __init__(self, start_mono_ms: int = 0, start_iso: str | None = None):
        self._mono = start_mono_ms
        self._iso = start_iso or "2026-01-01T00:00:00.000+00:00"

    def advance_ms(self, delta_ms: int) -> None:
        self._mono += delta_ms
        base = datetime.fromisoformat(self._iso)
        self._iso = datetime.fromtimestamp(
            base.timestamp() + delta_ms / 1000, tz=timezone.utc
        ).isoformat(timespec="milliseconds")

    def mono_ms(self) -> int:
        return self._mono

    def iso_now(self) -> str:
        return self._iso
