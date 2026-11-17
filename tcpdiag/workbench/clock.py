"""时钟基准抽象。

时钟基准声明：
- MonotonicClock：真实单调时钟（time.monotonic），用于 GUI 在线诊断。
- DeterministicClock：确定性虚拟时钟，仅向前推进，用于演示、快照重放与测试。
  对端报文携带 delay_ms（虚拟延迟），到达时刻 = max(当前, 发送时刻 + delay_ms)，
  因此同一份对端脚本重放时所有观测时间戳完全一致。
"""

import time


class MonotonicClock:
    base_name = "monotonic"

    def now_ms(self):
        return time.monotonic_ns() // 1_000_000

    def note_arrival(self, base_ts, delay_ms):
        return self.now_ms()

    def advance_to(self, ts):
        pass

    def advance(self, ms):
        pass


class DeterministicClock:
    base_name = "deterministic"

    def __init__(self, start_ms=0):
        self._now = start_ms

    def now_ms(self):
        return self._now

    def advance_to(self, ts):
        if ts > self._now:
            self._now = ts

    def advance(self, ms):
        if ms > 0:
            self._now += ms

    def note_arrival(self, base_ts, delay_ms):
        target = base_ts + max(0, delay_ms)
        if target > self._now:
            self._now = target
        return self._now
