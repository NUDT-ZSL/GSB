"""并发提交控制。

同一时刻只允许一个检查点或配置快照提交生效。后到者立即获得
E_STAGE_BUSY，绝不阻塞等待；持有者提交完成后显式释放。
"""

import threading

from .errors import EStageBusy


class CommitGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._holder = None

    def try_acquire(self, owner):
        """非阻塞获取提交权，失败立即抛 E_STAGE_BUSY。"""
        if not self._lock.acquire(blocking=False):
            raise EStageBusy(
                "提交通道忙，后到者立即失败且不等待",
                details={"requester": str(owner), "holder": str(self._holder)},
            )
        self._holder = owner
        return True

    def release(self, owner):
        if self._holder != owner:
            return False
        self._holder = None
        self._lock.release()
        return True

    @property
    def holder(self):
        return self._holder

    def busy(self):
        return self._holder is not None

    class Lease:
        def __init__(self, gate, owner):
            self.gate = gate
            self.owner = owner

        def __enter__(self):
            self.gate.try_acquire(self.owner)
            return self

        def __exit__(self, exc_type, exc, tb):
            self.gate.release(self.owner)
            return False

    def lease(self, owner):
        return self.Lease(self, owner)
