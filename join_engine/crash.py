from __future__ import annotations

import os
from typing import Callable


class CrashInjector:
    def __init__(self, specification: str | None = None) -> None:
        raw = specification if specification is not None else os.environ.get(
            "JOIN_ENGINE_CRASH_POINT"
        )
        self.specification = raw or ""
        self.label = ""
        self.target = -1
        if self.specification:
            label, separator, target = self.specification.rpartition(":")
            if separator and target.isdigit():
                self.label = label
                self.target = int(target)
            else:
                self.label = self.specification
                self.target = 0

    def maybe(self, label: str, count: int, *, flush: Callable[[], None] | None = None) -> None:
        if not self.label or label != self.label or count < self.target:
            return
        if flush is not None:
            flush()
        os._exit(137)

