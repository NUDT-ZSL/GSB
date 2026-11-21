"""Injectable logical clock. All TTL math uses integer microseconds so the
sub-millisecond remainder discarded by ms truncation is exactly recordable."""

import json


class LogicalClock:
    def now_us(self):
        raise NotImplementedError

    def now_ms(self):
        return self.now_us() // 1000


class FixedClock(LogicalClock):
    """A settable clock; the CLI persists its value as the clock watermark."""

    def __init__(self, now_us=0):
        self._now_us = int(now_us)

    def set_us(self, now_us):
        self._now_us = int(now_us)

    def now_us(self):
        return self._now_us


class StepClock(LogicalClock):
    """Clock driven by a deterministic step table (samples/clock_steps.json)."""

    def __init__(self, steps, index=0):
        self.steps = list(steps)
        self.index = index

    @classmethod
    def from_file(cls, path, index=0):
        with open(path, "r", encoding="utf-8") as fh:
            table = json.load(fh)
        return cls(table["steps"], index)

    def now_us(self):
        if not self.steps:
            return 0
        return int(self.steps[min(self.index, len(self.steps) - 1)]["now_us"])
