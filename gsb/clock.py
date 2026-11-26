"""可注入逻辑时钟。

引擎不读取墙钟；裁决时刻由调用方注入（CLI ``--now`` 或固定种子常量），
因此同一逻辑时钟序列下重复运行得到逐位一致的结果。
"""
from __future__ import annotations

from typing import Dict, List


class LogicalClock:
    def now_ms(self) -> int:  # pragma: no cover - 接口
        raise NotImplementedError

    def watermark(self) -> Dict[str, object]:
        return {"kind": self.__class__.__name__, "now_ms": self.now_ms()}


class FixedClock(LogicalClock):
    """始终返回同一逻辑时刻（重放/恢复时使用持久化的同一 now）。"""

    def __init__(self, now_ms: int) -> None:
        self._now = int(now_ms)

    def now_ms(self) -> int:
        return self._now

    def watermark(self) -> Dict[str, object]:
        return {"kind": "fixed", "now_ms": self._now}


class SequenceClock(LogicalClock):
    """按确定性序列推进的时钟；序列耗尽后停在最后一个值。

    相同序列 + 相同调用次数产生相同取值，测试据此验证逐位一致性。
    """

    def __init__(self, sequence_ms: List[int]) -> None:
        if not sequence_ms:
            raise ValueError("clock sequence must not be empty")
        self._seq = [int(x) for x in sequence_ms]
        self._idx = 0
        self.calls = 0

    def now_ms(self) -> int:
        self.calls += 1
        value = self._seq[min(self._idx, len(self._seq) - 1)]
        if self._idx < len(self._seq) - 1:
            self._idx += 1
        return value

    def watermark(self) -> Dict[str, object]:
        return {
            "kind": "sequence",
            "now_ms": self.now_ms(),
            "index": self._idx,
            "calls": self.calls,
            "sequence": list(self._seq),
        }
