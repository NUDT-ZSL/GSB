"""Signal handling for graceful, snapshot-safe termination."""

from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from typing import Iterator


class StopController:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.signal_name: str | None = None
        self.installed = False
        self._previous: dict[int, object] = {}

    def request_stop(self, signal_name: str = "SIMULATED") -> None:
        if self.signal_name is None:
            self.signal_name = signal_name
        self.event.set()

    @property
    def requested(self) -> bool:
        return self.event.is_set()

    def check(self) -> bool:
        return self.event.is_set()

    @contextmanager
    def installed_handlers(self) -> Iterator["StopController"]:
        def handler(signum: int, frame: object) -> None:
            name = signal.Signals(signum).name
            self.request_stop(name)

        signals = [signal.SIGTERM, signal.SIGINT]
        self.installed = True
        try:
            for signum in signals:
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, handler)
            yield self
        finally:
            for signum, previous in self._previous.items():
                signal.signal(signum, previous)
            self._previous.clear()
            self.installed = False
