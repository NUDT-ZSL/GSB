"""预算账本：扣减、释放与降级归还的原子性保证。

所有写操作在同一把可重入锁内完成并返回账本快照副本，查询侧
（:meth:`snapshot`）拿到的永远是某一完整状态，观察不到一半扣减的中间态。
账本内容可 JSON 化，供版本化快照整体持久化。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .config import BudgetConfig
from .planner import CostEstimate, SchedulePlan


@dataclass
class BudgetLedger:
    """三重预算的运行时账本。"""

    compute_budget: int
    memory_budget_bytes: int
    parallel_slots: int
    compute_used: int = 0
    memory_reserved: int = 0
    slots_held: int = 0
    reservations: dict[str, dict[str, int]] = field(default_factory=dict)
    released: dict[str, dict[str, int]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @classmethod
    def from_config(cls, config: BudgetConfig) -> "BudgetLedger":
        return cls(
            compute_budget=config.compute_budget,
            memory_budget_bytes=config.memory_budget_bytes,
            parallel_slots=config.parallel_slots,
        )

    def reserve(self, estimate: CostEstimate, slot_count: int = 1) -> None:
        """原子扣减一个任务的计算/内存/槽位预算。"""
        with self.lock:
            if estimate.task_id in self.reservations:
                raise KeyError(f"任务 {estimate.task_id} 已存在预算预留")
            if self.compute_used + estimate.compute_units > self.compute_budget:
                raise OverflowError("计算预算原子扣减失败")
            if self.memory_reserved + estimate.memory_bytes > self.memory_budget_bytes:
                raise OverflowError("内存预算原子扣减失败")
            if self.slots_held + slot_count > self.parallel_slots:
                raise OverflowError("槽位预算原子扣减失败")
            self.compute_used += estimate.compute_units
            self.memory_reserved += estimate.memory_bytes
            self.slots_held += slot_count
            self.reservations[estimate.task_id] = {
                "compute_units": estimate.compute_units,
                "memory_bytes": estimate.memory_bytes,
                "slots": slot_count,
            }

    def release(self, task_id: str) -> dict[str, int]:
        """原子释放任务全部占用（计算、内存、槽位一并归还）。"""
        with self.lock:
            entry = self.reservations.pop(task_id)
            self.compute_used -= entry["compute_units"]
            self.memory_reserved -= entry["memory_bytes"]
            self.slots_held -= entry["slots"]
            self.released[task_id] = dict(entry)
            return dict(entry)

    def refund_degradation(self, task_id: str, refund_compute: int, refund_memory: int) -> None:
        """降级归还：任务实际占用低于预留时，原子退还差额。"""
        with self.lock:
            entry = self.reservations[task_id]
            refund_compute = min(refund_compute, entry["compute_units"])
            refund_memory = min(refund_memory, entry["memory_bytes"])
            entry["compute_units"] -= refund_compute
            entry["memory_bytes"] -= refund_memory
            self.compute_used -= refund_compute
            self.memory_reserved -= refund_memory

    def reconcile_with_plan(self, plan: SchedulePlan) -> None:
        """按调度计划一次性原子建立全部预留，保证账本与准入集合可核对。"""
        with self.lock:
            self.compute_used = 0
            self.memory_reserved = 0
            self.slots_held = 0
            self.reservations = {}
            self.released = {}
            estimate_by_id = {item.task_id: item for item in plan.estimates}
            for task_id in plan.admitted:
                estimate = estimate_by_id[task_id]
                self.reservations[task_id] = {
                    "compute_units": estimate.compute_units,
                    "memory_bytes": estimate.memory_bytes,
                    "slots": 1,
                }
                self.compute_used += estimate.compute_units
                self.memory_reserved += estimate.memory_bytes
                self.slots_held += 1
            if self.compute_used > self.compute_budget:
                raise OverflowError("计划计算代价超出预算，账本拒绝对账")
            if self.memory_reserved > self.memory_budget_bytes:
                raise OverflowError("计划内存占用超出预算，账本拒绝对账")
            if self.slots_held > self.parallel_slots * max(1, len(plan.groups)):
                raise OverflowError("计划槽位占用异常，账本拒绝对账")

    def snapshot(self) -> dict:
        """返回完整账本的 JSON 化快照（深拷贝，持锁期间组装）。"""
        with self.lock:
            return {
                "compute_budget": self.compute_budget,
                "memory_budget_bytes": self.memory_budget_bytes,
                "parallel_slots": self.parallel_slots,
                "compute_used": self.compute_used,
                "memory_reserved": self.memory_reserved,
                "slots_held": self.slots_held,
                "reservations": {
                    key: dict(value) for key, value in sorted(self.reservations.items())
                },
                "released": {
                    key: dict(value) for key, value in sorted(self.released.items())
                },
            }

    def load_snapshot(self, payload: dict) -> None:
        """从快照整体替换当前账本状态（重启恢复路径）。"""
        with self.lock:
            self.compute_budget = payload["compute_budget"]
            self.memory_budget_bytes = payload["memory_budget_bytes"]
            self.parallel_slots = payload["parallel_slots"]
            self.compute_used = payload["compute_used"]
            self.memory_reserved = payload["memory_reserved"]
            self.slots_held = payload["slots_held"]
            self.reservations = {
                key: dict(value) for key, value in payload["reservations"].items()
            }
            self.released = {
                key: dict(value) for key, value in payload["released"].items()
            }
