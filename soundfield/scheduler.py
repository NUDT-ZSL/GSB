"""并发分块重建调度：有界线程池 + 确定性资源预算门 + 首错取消。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import errors as E
from .config import FP_SCALE
from .reconstruction import BlockId, BlockResult, compute_block
from .scene import Scene
from .images import ImageSource


@dataclass
class BudgetGate:
    """按 BlockId 字典序发放内存许可的有界门。

    预算不足时，后续块按确定顺序等待；绝不通过降低定点标度或缩短分数延迟
    滤波器来迁就预算。许可严格 FIFO，避免高序号块饥饿。
    """

    budget_bytes: int
    condition: threading.Condition = threading.Condition()
    used_bytes: int = 0
    ticket: int = 0
    served: int = 0

    def block_cost_bytes(self, ir_length: int) -> int:
        # 定点路径为 Python 大整数 object 数组（引用 8B + 整数约 28B），
        # 浮点路径为 float64；再预留一份残差记录空间。
        return ir_length * (8 + 28 + 8) * 2

    def check_block_fits(self, cost: int) -> None:
        """单块超过总预算属于场景声明不合法，快速失败且不进入 FIFO 队列。"""

        if cost > self.budget_bytes:
            raise E.BusinessError(
                E.E_BUDGET,
                "单个分块内存需求超过场景声明预算",
                {"block_cost_bytes": cost, "budget_bytes": self.budget_bytes},
            )

    def acquire(self, order_index: int, cost: int) -> None:
        with self.condition:
            my_ticket = self.ticket
            self.ticket += 1
            while self.served != my_ticket or self.used_bytes + cost > self.budget_bytes:
                self.condition.wait()
            self.used_bytes += cost
            self.served += 1
            self.condition.notify_all()

    def release(self, cost: int) -> None:
        with self.condition:
            self.used_bytes -= cost
            self.condition.notify_all()


class InterruptFlag:
    """可处理终止信号标志，只在分块边界被观察。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self, reason: str = "signal") -> None:
        self._reason = reason
        self._event.set()

    @property
    def reason(self) -> str:
        return getattr(self, "_reason", "signal")

    def is_set(self) -> bool:
        return self._event.is_set()


def run_scheduler(
    scene: Scene,
    images: list[ImageSource],
    blocks: list[BlockId],
    ir_length: int,
    amp_tol_q: int,
    energy_tol_q2: int,
    completed: dict[str, dict],
    state_writer,
    interrupt: InterruptFlag,
    block_observer=None,
) -> tuple[list[BlockResult], list[str]]:
    """调度所有分块。

    * ``max_workers`` 有界线程池；
    * 预算门按 ``(频段, 阶数, 扬声器)`` 确定顺序发放；
    * 任一分块异常立即取消其余待处理分块并抛出首个错误；
    * 中断标志仅在分块边界检查，已完成分块持久化、未完成分块无落盘痕迹。
    """

    gate = BudgetGate(scene.memory_budget_bytes)
    cost = gate.block_cost_bytes(ir_length)
    pending = [block for block in blocks if block.name() not in completed]
    finished_names = sorted(completed.keys())
    results: list[BlockResult] = []
    results_lock = threading.Lock()
    first_error: list[BaseException] = []
    cancel = threading.Event()

    def worker(order_index: int, block: BlockId) -> None:
        if cancel.is_set():
            return
        # 分块边界：开始前检查中断。
        if interrupt.is_set():
            return
        gate.check_block_fits(cost)
        gate.acquire(order_index, cost)
        try:
            if cancel.is_set() or interrupt.is_set():
                return
            result = compute_block(scene, images, block, ir_length, amp_tol_q, energy_tol_q2)
            if interrupt.is_set():
                # 分块边界：计算完成但不落盘，避免被误认为已完成。
                return
            if block_observer is not None:
                block_observer(block)
            state_writer(result)
            with results_lock:
                results.append(result)
                finished_names.append(block.name())
        except BaseException as exc:  # noqa: BLE001 - 首错需要统一上抛
            with results_lock:
                if not first_error:
                    first_error.append(exc)
            cancel.set()
        finally:
            gate.release(cost)

    with ThreadPoolExecutor(max_workers=scene.max_workers) as pool:
        futures = [pool.submit(worker, idx, block) for idx, block in enumerate(pending)]
        for future in futures:
            future.result()

    if first_error:
        raise first_error[0]
    if interrupt.is_set():
        exc = E.BusinessError(
            E.E_INTERRUPTED,
            "收到可处理终止信号，已在分块边界停止",
            {"reason": interrupt.reason, "completed_blocks": sorted(finished_names)},
        )
        exc.interrupt_requested = True  # type: ignore[attr-defined]
        raise exc
    return results, sorted(finished_names)


__all__ = ["BudgetGate", "InterruptFlag", "run_scheduler"]


_ = FP_SCALE
