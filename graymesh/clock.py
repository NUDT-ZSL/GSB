"""Injectable logical clock. All time-dependent decisions use this only."""


class LogicalClock:
    def __init__(self, start_ms=0, step_ms=1):
        self._now = int(start_ms)
        self._step = int(step_ms)

    def now_ms(self):
        value = self._now
        self._now += self._step
        return value

    def set_ms(self, value):
        self._now = int(value)

    def peek_ms(self):
        return self._now

    def fork(self):
        """Fork a child clock at the current logical time.

        Used for async mirror records: the fork happens on the calling thread,
        so the child clock's readings are deterministic regardless of how
        worker threads interleave afterwards.
        """
        return LogicalClock(start_ms=self.now_ms(), step_ms=1)
