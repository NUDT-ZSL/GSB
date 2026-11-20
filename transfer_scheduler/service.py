"""应用编排服务：把计划器、账本、快照、提交门、执行器与自检串成工作流。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .concurrency import CommitGate
from .config import AppConfig
from .consistency import run_all_checks
from .data import TaskSpec
from .errors import BusinessError
from .executor import FaultInjection, ParallelExecutor, merge_results
from .ledger import BudgetLedger
from .pipeline import FailedTask, TaskResult
from .planner import SchedulePlan, build_schedule_plan
from .snapshots import LEDGER_KIND, PLAN_KIND, SUMMARY_KIND, SnapshotStore


@dataclass
class ExecutionSummary:
    """一次完整运行的执行摘要（摘要快照的载荷来源）。"""

    run_id: str
    plan_id: str
    versions: dict[str, int]
    admitted: tuple[str, ...]
    rejected: tuple[dict[str, Any], ...]
    groups: tuple[tuple[str, ...], ...]
    scores: dict[str, float]
    gate_decisions: dict[str, dict[str, Any]]
    unavailable: dict[str, dict[str, Any]]
    ledger: dict[str, Any]
    evidence: dict[str, Any]
    last_errors: tuple[dict[str, Any], ...]


class WorkflowService:
    """对外的门面服务，维护账本、快照存储、提交门与最近业务错误。"""

    def __init__(
        self,
        config: AppConfig,
        snapshot_dir: str | Path,
        error_ring_size: int = 10,
    ) -> None:
        self.config = config
        self.snapshots = SnapshotStore(snapshot_dir)
        self.gate = CommitGate()
        self.ledger = BudgetLedger.from_config(config.budget)
        self._errors: deque[dict[str, Any]] = deque(maxlen=error_ring_size)

    def record_error(self, exc: BusinessError) -> dict[str, Any]:
        entry = {
            "code": exc.code,
            "message": exc.message,
            "context": exc.context,
        }
        self._errors.append(entry)
        return entry

    @property
    def last_errors(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._errors)

    def restore_from_snapshots(self) -> dict[str, int]:
        """重启恢复：逐类加载快照，版本/校验和变化时整体替换内存态。"""
        restored: dict[str, int] = {}
        plan_env = self.snapshots.load(PLAN_KIND)
        if plan_env is not None:
            restored[PLAN_KIND] = plan_env.version
        ledger_env = self.snapshots.load(LEDGER_KIND)
        if ledger_env is not None:
            self.ledger.load_snapshot(ledger_env.payload)
            restored[LEDGER_KIND] = ledger_env.version
        summary_env = self.snapshots.load(SUMMARY_KIND)
        if summary_env is not None:
            restored[SUMMARY_KIND] = summary_env.version
        return restored

    def run(
        self,
        tasks: list[TaskSpec],
        run_id: str = "run-0001",
        plan_id: str = "plan-0001",
        fault_injection: FaultInjection | None = None,
        in_process: bool = False,
        skip_serial_check: bool = False,
    ) -> ExecutionSummary:
        """执行一次完整调度-执行-自检工作流。

        提交阶段经 :class:`CommitGate` 非阻塞保护；冲突时后到者立即得到
        :class:`EStageBusy`，不会阻塞等待。
        """
        owner = run_id
        with self.gate.transaction(owner):
            plan = build_schedule_plan(tasks, self.config.budget, plan_id=plan_id)
            plan_env = self.snapshots.save(PLAN_KIND, plan)

            # 账本与计划原子对账，随后整体快照。
            self.ledger = BudgetLedger.from_config(self.config.budget)
            self.ledger.reconcile_with_plan(plan)
            ledger_env = self.snapshots.save(LEDGER_KIND, self.ledger.snapshot())

            executor = ParallelExecutor(self.config, fault_injection)
            if in_process:
                results = executor.execute_in_process(plan, tasks)
            else:
                results = executor.execute(plan, tasks)
            ok, failed = merge_results(results)

            # 失败任务释放其预留（计算/内存/槽位原子归还）。
            released_failed: set[str] = set()
            for task_id in failed:
                if task_id in self.ledger.snapshot()["reservations"]:
                    self.ledger.release(task_id)
                    released_failed.add(task_id)

            evidence = run_all_checks(
                plan,
                self.ledger,
                tasks,
                results,
                self.config,
                skip_serial=skip_serial_check,
                released_failed=released_failed,
            )

            summary = self._build_summary(
                run_id,
                plan,
                ok,
                failed,
                evidence,
                plan_env.version,
                ledger_env.version,
            )
            summary_env = self.snapshots.save(SUMMARY_KIND, summary)
            summary.versions[SUMMARY_KIND] = summary_env.version
            return summary

    def _build_summary(
        self,
        run_id: str,
        plan: SchedulePlan,
        ok: dict[str, TaskResult],
        failed: dict[str, FailedTask],
        evidence: dict[str, Any],
        plan_version: int,
        ledger_version: int,
    ) -> ExecutionSummary:
        scores = {task_id: result.score for task_id, result in sorted(ok.items())}
        gate_decisions = {
            task_id: {
                "accepted": result.gate.accepted,
                "reason": result.gate.reason,
                "in_tie_band": result.gate.in_tie_band,
                "distance": result.gate.distance,
            }
            for task_id, result in sorted(ok.items())
        }
        unavailable = {
            task_id: {
                "status": result.status,
                "code": result.code,
                "stage": result.stage,
                "message": result.message,
            }
            for task_id, result in sorted(failed.items())
        }
        rejected = tuple(
            {
                "task_id": item.task_id,
                "code": item.code,
                "conflict_type": item.conflict_type,
                "reason": item.reason,
                "arbitration_rank": item.arbitration_rank,
            }
            for item in plan.rejected
        )
        return ExecutionSummary(
            run_id=run_id,
            plan_id=plan.plan_id,
            versions={PLAN_KIND: plan_version, LEDGER_KIND: ledger_version},
            admitted=plan.admitted,
            rejected=rejected,
            groups=plan.groups,
            scores=scores,
            gate_decisions=gate_decisions,
            unavailable=unavailable,
            ledger=self.ledger.snapshot(),
            evidence=evidence,
            last_errors=self.last_errors,
        )
