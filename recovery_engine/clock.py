"""注入式逻辑时钟。

所有生效判定、动作窗口与回退时刻换算只依赖注入的逻辑时钟毫秒值，
绝不读取系统墙钟。时钟回拨超过容差时抛出 E_CLOCK_REGRESS 且状态不变。
"""

from .errors import BizError, ErrCode


class LogicalClock:
    """由外部注入的确定性逻辑时钟（毫秒）。

    - values: 注入的毫秒序列；advance() 前进一步，now() 读当前值。
    - 序列耗尽后保持最后一个值（幂等读取）。
    - tolerance_ms: 允许的最大回拨量；超过则抛 E_CLOCK_REGRESS。
    """

    def __init__(self, values, tolerance_ms=0):
        if not values:
            raise BizError(ErrCode.E_BAD_CONFIG, "逻辑时钟序列不能为空")
        self._values = [int(v) for v in values]
        self._idx = 0
        self._last = None
        self.tolerance_ms = int(tolerance_ms)
        # 校验首值并建立水位
        self._check(self._values[0])

    def _check(self, value):
        if self._last is not None and value < self._last - self.tolerance_ms:
            raise BizError(
                ErrCode.E_CLOCK_REGRESS,
                "逻辑时钟回拨超过容差",
                last=self._last,
                observed=value,
                tolerance_ms=self.tolerance_ms,
            )
        self._last = value

    def now(self):
        return self._values[min(self._idx, len(self._values) - 1)]

    def advance(self):
        """步进到下一个注入值；回拨超容差时抛错且内部状态不变。"""
        nxt = self._values[min(self._idx + 1, len(self._values) - 1)]
        self._check(nxt)  # 先校验，抛错时 _idx 不变，状态不变
        self._idx = min(self._idx + 1, len(self._values) - 1)
        return self.now()

    @classmethod
    def from_file(cls, path, tolerance_ms=0):
        import json

        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return cls(payload["steps_ms"], tolerance_ms=payload.get("tolerance_ms", tolerance_ms))
