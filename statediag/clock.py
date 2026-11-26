"""可注入的逻辑时钟。

逻辑时钟不读取系统时间，只按确定性的步长单调推进，保证同一种子下
多次运行产生完全一致的毫秒时间戳，且严格单调（用于检查点排序）。
"""


class LogicalClock:
    def __init__(self, start_ms=0, tick_ms=1):
        if tick_ms <= 0:
            raise ValueError("tick_ms 必须为正数")
        self._now = int(start_ms)
        self._tick = int(tick_ms)

    def now_ms(self):
        """读取当前逻辑毫秒，不推进时钟。"""
        return self._now

    def advance(self, ticks=1):
        """推进若干个步长并返回推进后的毫秒值。"""
        if ticks < 0:
            raise ValueError("ticks 不能为负")
        self._now += self._tick * ticks
        return self._now

    def tick(self):
        """推进一个步长，返回新的毫秒值。"""
        return self.advance(1)

    def to_seconds(self):
        return self._now / 1000.0
