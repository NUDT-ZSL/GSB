"""固定内存预算记账器。

组件按分块处理状态，任何时刻只驻留一个块大小的数组。预算器对显式
申报的 ndarray 占用字节进行高水位记账，超出预算立即抛 E_BUDGET_EXCEEDED，
从而在固定内存预算下可复现地运行。
"""

import numpy as np

from .errors import EBudgetExceeded


class MemoryBudget:
    def __init__(self, limit_bytes):
        if limit_bytes <= 0:
            raise ValueError("内存预算必须为正数")
        self.limit_bytes = int(limit_bytes)
        self._used = 0
        self._high_water = 0

    @staticmethod
    def nbytes(array):
        if isinstance(array, np.ndarray):
            return int(array.nbytes)
        return int(array)

    def charge(self, array, label=""):
        """为一个 ndarray（或显式字节数）申报内存。"""
        size = self.nbytes(array)
        if self._used + size > self.limit_bytes:
            raise EBudgetExceeded(
                "内存预算超限",
                details={
                    "limit_bytes": self.limit_bytes,
                    "used_bytes": self._used,
                    "request_bytes": size,
                    "label": label,
                },
            )
        self._used += size
        self._high_water = max(self._high_water, self._used)
        return size

    def refund(self, array, label=""):
        size = self.nbytes(array)
        self._used = max(0, self._used - size)
        return size

    class Scope:
        """作用域记账：进入时申报，退出时精确释放同一笔字节。"""

        def __init__(self, budget, size, label=""):
            self.budget = budget
            self.size = int(size)
            self.label = label

        def __enter__(self):
            self.budget.charge(self.size, self.label)
            return self

        def __exit__(self, exc_type, exc, tb):
            self.budget._used = max(0, self.budget._used - self.size)
            return False

    def scope(self, array, label=""):
        return MemoryBudget.Scope(self, self.nbytes(array), label)

    @property
    def used_bytes(self):
        return self._used

    @property
    def high_water_bytes(self):
        return self._high_water

    def remaining(self):
        return self.limit_bytes - self._used
