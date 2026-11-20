"""并发提交控制。

同一时刻只允许一个调度计划提交生效。:class:`CommitGate` 使用非阻塞
``acquire(blocking=False)``：后到的提交立即收到 :class:`EStageBusy`，
不排队、不阻塞等待；也支持上下文管理器形式的临界区。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from .errors import EStageBusy


class CommitGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_owner: str | None = None

    def try_begin(self, owner: str) -> None:
        if not self._lock.acquire(blocking=False):
            raise EStageBusy(
                "调度计划提交通道正忙，后到提交立即拒绝",
                active_owner=self._active_owner,
                requesting_owner=owner,
            )
        self._active_owner = owner

    def end(self, owner: str) -> None:
        if self._active_owner != owner:
            raise RuntimeError(f"非法释放：{owner} 并非当前提交持有者")
        self._active_owner = None
        self._lock.release()

    @contextmanager
    def transaction(self, owner: str):
        self.try_begin(owner)
        try:
            yield
        finally:
            self.end(owner)

    @property
    def busy(self) -> bool:
        return self._active_owner is not None
