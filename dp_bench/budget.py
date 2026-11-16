"""全局步数预算账本。

并行线程共享一个账本。为保证并行结果与“单线程按句 ID 顺序执行”逐字段
一致，预算扣减按 (句 ID 升序, 步序号) 的全序进行：每个句 ID 有一个轮次，
只有轮到该句时它的 request 才会被受理，其余线程在条件变量上等待。

这样无论线程实际完成顺序如何，预算扣减序列、剩余额度与终止句位置都与
串行执行完全相同，同时各句的特征构造/打分仍在线程中真正并行。

恢复时通过 pre_consumed 注入已完成句的步数，账本即可重建出与不中断运行
完全相同的扣减序列、消耗曲线与剩余额度，随后从下一句继续。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .errors import BudgetExhaustedError, NotScheduledError


@dataclass
class Deduction:
    sentence_id: str
    step_no: int
    sort_key: str
    remaining_after: int

    def as_dict(self) -> dict:
        return {
            "sentence_id": self.sentence_id,
            "step_no": self.step_no,
            "sort_key": self.sort_key,
            "remaining_after": self.remaining_after,
        }


class BudgetLedger:
    """按句 ID 稳定顺序扣减的全局预算账本。"""

    def __init__(self, sentence_ids: list[str], global_budget: int,
                 quota: int, pre_consumed: dict | None = None):
        self._order = list(sentence_ids)
        self._global_budget = int(global_budget)
        self._quota = int(quota)
        self._cond = threading.Condition()
        self._cursor = 0
        self._used_by: dict[str, int] = {sid: 0 for sid in sentence_ids}
        self._exhausted = False
        self._deductions: list[Deduction] = []
        self._curve = [0]
        self._denied: set[str] = set()

        pre_consumed = pre_consumed or {}
        remaining = self._global_budget
        for sid in self._order:
            if sid not in pre_consumed:
                # 预消耗字典的键即“已完成句”集合；遇到首个未完成句即停止，
                # 保证恢复后从正确的句 ID 继续（含零步数已完成句）。
                break
            used = int(pre_consumed.get(sid, 0))
            self._used_by[sid] = used
            self._cursor += 1
            for step_no in range(1, used + 1):
                remaining -= 1
                self._deductions.append(Deduction(
                    sentence_id=sid,
                    step_no=step_no,
                    sort_key=f"{sid}@{step_no:08d}",
                    remaining_after=remaining,
                ))
                self._curve.append(self._global_budget - remaining)
        self._global_remaining = remaining

    # ---- 句任务生命周期 -------------------------------------------------
    def release(self, sentence_id: str) -> None:
        """某句结束（成功/失败/异常）后调用，推进全局轮次。"""
        with self._cond:
            if self._cursor < len(self._order) and \
                    self._order[self._cursor] == sentence_id:
                self._cursor += 1
                self._cond.notify_all()

    def _wait_turn(self, sentence_id: str) -> None:
        while self._cursor < len(self._order) and \
                self._order[self._cursor] != sentence_id:
            if self._exhausted or sentence_id in self._denied:
                raise NotScheduledError(
                    "全局预算已耗尽，按句 ID 升序标记为未调度",
                    phase="schedule", sentence_id=sentence_id)
            self._cond.wait()
        if self._exhausted or sentence_id in self._denied:
            raise NotScheduledError(
                "全局预算已耗尽，按句 ID 升序标记为未调度",
                phase="schedule", sentence_id=sentence_id)

    # ---- 单步预算申请 ---------------------------------------------------
    def request(self, sentence_id: str, step_no: int) -> None:
        with self._cond:
            self._wait_turn(sentence_id)

            if self._used_by[sentence_id] >= self._quota:
                self._exhausted = True
                self._deny_rest_locked(sentence_id)
                raise BudgetExhaustedError(
                    "单句消耗超出预算配额，终止且不产出部分弧",
                    phase="parse", sentence_id=sentence_id,
                    step_no=step_no)

            if self._global_remaining <= 0:
                self._exhausted = True
                self._deny_rest_locked(sentence_id)
                raise BudgetExhaustedError(
                    "消耗超出剩余全局预算，终止且不产出部分弧",
                    phase="parse", sentence_id=sentence_id,
                    step_no=step_no)

            self._global_remaining -= 1
            self._used_by[sentence_id] += 1
            self._deductions.append(Deduction(
                sentence_id=sentence_id,
                step_no=step_no,
                sort_key=f"{sentence_id}@{step_no:08d}",
                remaining_after=self._global_remaining,
            ))
            self._curve.append(self._global_budget - self._global_remaining)

    def _deny_rest_locked(self, denied_sid: str) -> None:
        """预算在 denied_sid 处耗尽，其后句 ID 全部标记未调度。"""
        try:
            idx = self._order.index(denied_sid)
        except ValueError:
            idx = self._cursor
        for sid in self._order[idx + 1:]:
            self._denied.add(sid)
        self._cond.notify_all()

    # ---- 收尾：标记未调度句 ---------------------------------------------
    def mark_unscheduled(self) -> list[str]:
        """所有任务结束后，返回应标记为未调度的句 ID（升序）。"""
        with self._cond:
            return sorted(self._denied)

    # ---- 报告视图 -------------------------------------------------------
    @property
    def global_consumed(self) -> int:
        return self._global_budget - self._global_remaining

    @property
    def global_remaining(self) -> int:
        return self._global_remaining

    @property
    def global_budget(self) -> int:
        return self._global_budget

    @property
    def quota(self) -> int:
        return self._quota

    def used_of(self, sentence_id: str) -> int:
        return self._used_by.get(sentence_id, 0)

    def deductions(self) -> list[dict]:
        return [d.as_dict() for d in self._deductions]

    def curve(self) -> list[int]:
        return list(self._curve)
