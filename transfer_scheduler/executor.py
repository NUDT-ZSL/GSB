"""本地进程池执行与结构化异常回传。

* 进程池统一通过 :func:`multiprocessing.get_context("spawn")` 创建，
  Windows 默认即为 spawn，Linux/macOS 下行为也与之一致；
* 按调度计划的并行分组逐波次分派，组内按 task_id 升序提交，
  ``chunksize`` 固定，分块边界与归并顺序完全确定性可复现；
* worker 内任何异常都收口为 :class:`FailedTask`，携带业务码、阶段名、
  task_id；单个任务失败不影响同批其他任务的已提交结果；
* :class:`FaultInjection` 支持演示用的确定性任务故障注入。
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass, field

from .config import AppConfig
from .data import TaskSpec
from .errors import E_WORKER_FAILURE, BusinessError
from .pipeline import FailedTask, TaskResult, run_task
from .planner import SchedulePlan


@dataclass(frozen=True)
class FaultInjection:
    """故障注入表：task_id -> 触发阶段（演示并行异常回传）。"""

    failures: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _WorkItem:
    """跨进程工作单元，全部字段可 pickle。"""

    task: TaskSpec
    config: AppConfig
    effective_sample_count: int
    fault_stage: str | None


def _worker(item: _WorkItem) -> TaskResult | FailedTask:
    """spawn 子进程入口：纯函数式执行，异常不外泄。"""
    if item.fault_stage is not None:
        return FailedTask(
            task_id=item.task.task_id,
            status="unavailable",
            code=E_WORKER_FAILURE,
            stage=item.fault_stage,
            message=f"故障注入：在 {item.fault_stage} 阶段主动失败",
            context={"injected": True},
        )
    try:
        return run_task(
            item.task,
            item.config.pipeline,
            item.config.transfer_gate,
            item.config.score_gate,
            item.effective_sample_count,
        )
    except BusinessError as exc:
        return FailedTask(
            task_id=item.task.task_id,
            status="unavailable",
            code=exc.code,
            stage=exc.context.get("stage", "unknown"),
            message=exc.message,
            context=dict(exc.context),
        )
    except Exception as exc:  # noqa: BLE001 - 进程边界兜底
        return FailedTask(
            task_id=item.task.task_id,
            status="unavailable",
            code=E_WORKER_FAILURE,
            stage="unknown",
            message=str(exc),
            context={"exception_type": type(exc).__name__},
        )


class ParallelExecutor:
    """按调度计划分组分派任务的本地进程池执行器。"""

    def __init__(self, config: AppConfig, fault_injection: FaultInjection | None = None) -> None:
        self.config = config
        self.fault_injection = fault_injection or FaultInjection()

    def _build_items(self, plan: SchedulePlan, tasks_by_id: dict[str, TaskSpec]) -> list[_WorkItem]:
        items: list[_WorkItem] = []
        # 跨组也按波次顺序、组内 task_id 升序，保证分块边界确定。
        for group in plan.groups:
            for task_id in sorted(group):
                task = tasks_by_id[task_id]
                items.append(
                    _WorkItem(
                        task=task,
                        config=self.config,
                        effective_sample_count=plan.effective_samples[task_id],
                        fault_stage=self.fault_injection.failures.get(task_id),
                    )
                )
        return items

    def execute(
        self,
        plan: SchedulePlan,
        tasks: list[TaskSpec],
    ) -> dict[str, TaskResult | FailedTask]:
        """执行全部准入任务，返回按 task_id 升序归并的结果字典。"""
        tasks_by_id = {task.task_id: task for task in tasks}
        items = self._build_items(plan, tasks_by_id)
        if not items:
            return {}
        ctx = mp.get_context("spawn")
        results: list[TaskResult | FailedTask] = []
        # 按并行组划分波次，波次内池大小受槽位约束，波次间确定性排序归并。
        cursor = 0
        with ctx.Pool(processes=self.config.execution.worker_count) as pool:
            for group in plan.groups:
                size = len(group)
                wave_items = items[cursor : cursor + size]
                cursor += size
                wave_results = pool.map(
                    _worker,
                    wave_items,
                    chunksize=self.config.execution.chunksize,
                )
                # pool.map 本身按提交顺序返回；这里再按 task_id 排序，
                # 即使后续更换分派原语也保持归并顺序确定。
                wave_results.sort(key=lambda result: result.task_id)
                results.extend(wave_results)
        return {result.task_id: result for result in sorted(results, key=lambda item: item.task_id)}

    def execute_in_process(
        self,
        plan: SchedulePlan,
        tasks: list[TaskSpec],
    ) -> dict[str, TaskResult | FailedTask]:
        """同进程串行执行（测试与低资源环境使用），语义与进程池一致。"""
        tasks_by_id = {task.task_id: task for task in tasks}
        results: list[TaskResult | FailedTask] = []
        for item in self._build_items(plan, tasks_by_id):
            results.append(_worker(item))
        return {result.task_id: result for result in sorted(results, key=lambda item: item.task_id)}


def merge_results(
    results: dict[str, TaskResult | FailedTask],
) -> tuple[dict[str, TaskResult], dict[str, FailedTask]]:
    """把混合结果拆分为成功表与失败表，均按键排序。"""
    ok: dict[str, TaskResult] = {}
    failed: dict[str, FailedTask] = {}
    for task_id in sorted(results):
        result = results[task_id]
        if isinstance(result, FailedTask):
            failed[task_id] = result
        else:
            ok[task_id] = result
    return ok, failed
