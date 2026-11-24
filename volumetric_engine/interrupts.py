"""Signal handling that is observed only at tile boundaries."""

from __future__ import annotations

import signal
import threading
from types import FrameType


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.signal_name: str | None = None

    def request(self, signal_name: str) -> None:
        if self.signal_name is None:
            self.signal_name = signal_name
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def exit_code(self) -> int:
        return 130 if self.signal_name == "SIGINT" else 143


class SignalGuard:
    """Install SIGINT/SIGTERM handlers without doing I/O in signal context."""

    def __init__(self, token: CancellationToken) -> None:
        self.token = token
        self._previous: dict[int, object] = {}

    def __enter__(self) -> "SignalGuard":
        for signum, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
            try:
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._make_handler(name))
            except (ValueError, OSError):
                # Signal handlers can only be installed in the main thread.
                pass
        return self

    def _make_handler(self, name: str):
        def handler(signum: int, frame: FrameType | None) -> None:
            _ = (signum, frame)
            self.token.request(name)

        return handler

    def __exit__(self, exc_type, exc, tb) -> None:
        for signum, previous in self._previous.items():
            try:
                signal.signal(signum, previous)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
        self._previous.clear()

