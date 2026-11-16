from __future__ import annotations

import signal
import threading
from typing import Any


class StopController:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.signal_name: str | None = None

    @property
    def requested(self) -> bool:
        return self.event.is_set()

    def request(self, signal_name: str = "programmatic") -> None:
        if self.signal_name is None:
            self.signal_name = signal_name
        self.event.set()


def install_signal_handlers(controller: StopController) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        controller.request(name)

    candidates = [getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)]
    signal_break = getattr(signal, "SIGBREAK", None)
    if signal_break is not None:
        candidates.append(signal_break)
    for signum in candidates:
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        except (OSError, ValueError):
            continue
    return previous


def restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            continue

