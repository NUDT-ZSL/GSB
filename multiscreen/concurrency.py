from __future__ import annotations

import threading
from typing import Iterable

from .errors import E_BUDGET, ScreenError


class OrderedMemoryGate:
    """按给定键顺序排队的内存预算闸门。

    即使线程池以不同调度顺序进入，键较小的接收端/分片也先获得预算；
    队首预算不足时，后续键不会插队，保证等待顺序确定。
    """

    def __init__(self, total_bytes: int, ordered_keys: Iterable[str]):
        if not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes <= 0:
            raise ScreenError(E_BUDGET, "内存预算必须是正整数", {"budget_bytes": total_bytes})
        self.total_bytes = total_bytes
        self.used_bytes = 0
        keys = list(ordered_keys)
        self.order = {key: index for index, key in enumerate(keys)}
        self.next_order = 0
        self._condition = threading.Condition()

    def acquire(self, key: str, need_bytes: int) -> None:
        if key not in self.order:
            raise ScreenError(E_BUDGET, "未知资源键，无法按确定顺序等待", {"key": key})
        if not isinstance(need_bytes, int) or need_bytes <= 0:
            need_bytes = 1
        if need_bytes > self.total_bytes:
            raise ScreenError(
                E_BUDGET,
                "单个在途数据块超过内存预算",
                {"key": key, "need_bytes": need_bytes, "budget_bytes": self.total_bytes},
            )
        my_order = self.order[key]
        with self._condition:
            while True:
                if my_order == self.next_order and self.used_bytes + need_bytes <= self.total_bytes:
                    self.used_bytes += need_bytes
                    self.next_order += 1
                    return
                self._condition.wait(timeout=0.05)

    def release(self, key: str, need_bytes: int) -> None:
        with self._condition:
            self.used_bytes = max(0, self.used_bytes - need_bytes)
            self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "total_bytes": self.total_bytes,
                "used_bytes": self.used_bytes,
                "next_order": self.next_order,
                "keys": len(self.order),
            }
