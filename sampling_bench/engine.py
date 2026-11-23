"""诊断工作台主编排引擎。

把校验、策略裁决、三预算准入、进程池分块调度、版本化快照与请求边界检查点
组合为一次可复现的批量解码运行，并保留最近业务错误码与恢复报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .admission import (
    AdmissionPlan,
    arbitrate,
    detect_top_p_regrets,
)
from .budget import (
    BudgetLedger,
    BudgetLimits,
)
from .checkpoint import Checkpoint, CheckpointStore, RecoveryReport
from .dataset import load_logits, load_vocab
import json
from .models import Request, SampleResult
from .scheduler import BatchTask, ForcedTermination, Scheduler
from .snapshot import SnapshotStore
from .validation import validate_requests


@dataclass
class RunConfig:
    max_workers: int = 2
    batch_size: int = 2
    step_budget: int = 10_000
    cache_budget: int = 10_000
    slot_budget: int = 4
    wave_budget: int = 1_000
    verify_batches: bool = True

    def limits(self) -> BudgetLimits:
        return BudgetLimits(
            step_budget=self.step_budget,
            cache_budget=self.cache_budget,
            slot_budget=self.slot_budget,
        )


@dataclass
class RunArtifacts:
    plan: AdmissionPlan
    ledger: BudgetLedger
    results: list[SampleResult]
    snapshot_versions: dict[str, int]
    recovery: RecoveryReport
    validation_errors: list[dict[str, Any]] = field(default_factory=list)


class BenchEngine:
    def __init__(self, data_dir: str | Path, work_dir: str | Path, config: RunConfig):
        self.data_dir = str(data_dir)
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.vocab = load_vocab(self.data_dir)
        self.raw_requests = self._load_raw_requests()
        self.snapshots = SnapshotStore(self.work_dir / "snapshots")
        self.checkpoints = CheckpointStore(self.work_dir / "checkpoints")
        self.recent_errors: list[dict[str, Any]] = []

    def _load_raw_requests(self) -> list[dict[str, Any]]:
        """以原始字典形式加载请求，结构性校验延后到 validate()。"""
        path = Path(self.data_dir) / "requests.jsonl"
        raw: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    raw.append(json.loads(line))
        return raw

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def validate(self):
        report = validate_requests(self.raw_requests)
        self.recent_errors.extend(report.errors)
        return report

    # ------------------------------------------------------------------
    # 准入与策略裁决
    # ------------------------------------------------------------------
    def plan_admission(self, requests: list[Request]) -> AdmissionPlan:
        plan = arbitrate(
            requests, self.config.limits(), max_waves=self.config.wave_budget
        )
        profile = plan.selected_profile
        if profile.top_p_cap < 1.0:
            admitted_set = set(plan.selected.admitted_ids)
            admitted_requests = [
                request
                for request in requests
                if request.request_id in admitted_set
            ]
            regrets = detect_top_p_regrets(
                admitted_requests,
                profile,
                _sample_for_regret,
                self.vocab,
                lambda rid: load_logits(self.data_dir, rid),
            )
            plan.selected.top_p_regrets = regrets
        return plan

    # ------------------------------------------------------------------
    # 任务构造
    # ------------------------------------------------------------------
    def build_tasks(
        self, requests: list[Request], plan: AdmissionPlan
    ) -> list[BatchTask]:
        profile = plan.selected_profile
        by_id = {request.request_id: request for request in requests}
        tasks: list[BatchTask] = []
        for request_id in plan.selected.admitted_ids:
            request = by_id[request_id]
            effective_params = profile.effective_params(request.sampling_params)
            override = (
                effective_params.to_dict()
                if effective_params != request.sampling_params
                else None
            )
            tasks.append(
                BatchTask(
                    request=request,
                    effective_length=profile.effective_length(request),
                    override_params=override,
                )
            )
        tasks.sort(key=lambda task: task.request.request_id)
        return tasks

    # ------------------------------------------------------------------
    # 完整运行（含恢复）
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        crash_after_committed: int | None = None,
        fresh: bool = False,
    ) -> RunArtifacts:
        if fresh:
            self.checkpoints.clear()

        report = self.validate()
        requests = report.valid_requests
        plan = self.plan_admission(requests)
        all_tasks = self.build_tasks(requests, plan)

        limits = BudgetLimits(
            step_budget=min(self.config.step_budget, plan.selected_profile.step_budget),
            cache_budget=min(
                self.config.cache_budget, plan.selected_profile.cache_budget
            ),
            slot_budget=min(
                self.config.slot_budget, plan.selected_profile.slot_budget
            ),
        )

        checkpoint = self.checkpoints.read()
        recovery = self._build_recovery_report(checkpoint, all_tasks)

        if checkpoint is not None:
            ledger = BudgetLedger.from_events(limits, checkpoint.ledger_events)
            results_by_id = {r.request_id: r for r in checkpoint.results}
            start_index = self._resume_start_index(checkpoint, all_tasks)
        else:
            ledger = BudgetLedger(limits=limits)
            results_by_id = {}
            start_index = 0

        remaining_tasks = all_tasks[start_index:]
        scheduler = Scheduler(
            self.data_dir,
            self.vocab,
            max_workers=self.config.max_workers,
            batch_size=self.config.batch_size,
            verify=self.config.verify_batches,
        )

        def on_batch_committed(batch_results, batch_tasks):
            for task, result in zip(batch_tasks, batch_results):
                request = self._effective_request(task)
                ledger.reserve(request)
                ledger.release(request)
                results_by_id[result.request_id] = result
            committed_ids = sorted(results_by_id.keys())
            next_id = self._next_request_id(committed_ids, all_tasks)
            self.checkpoints.write(
                Checkpoint(
                    committed_ids=committed_ids,
                    next_request_id=next_id,
                    results=[results_by_id[rid] for rid in committed_ids],
                    ledger_events=list(ledger.events),
                    consumed_steps=ledger.used()["steps"],
                    consumed_cache=ledger.used()["cache"],
                    consumed_slots=ledger.used()["slots"],
                    checksum="",
                )
            )

        forced: ForcedTermination | None = None
        try:
            new_results = scheduler.run_batches(
                remaining_tasks,
                on_batch_committed=on_batch_committed,
                crash_after_committed=crash_after_committed,
                already_committed=start_index,
            )
        except ForcedTermination as exc:
            forced = exc
            new_results = []

        ordered_ids = [task.request.request_id for task in all_tasks]
        final_results = [results_by_id[rid] for rid in ordered_ids if rid in results_by_id]

        snapshot_versions = self._publish_snapshots(plan, ledger, final_results)

        if forced is not None:
            # 演示路径：保留检查点供下一次 resume，向上层暴露终止状态。
            raise InterruptedRun(
                str(forced),
                RunArtifacts(
                    plan=plan,
                    ledger=ledger,
                    results=final_results,
                    snapshot_versions=snapshot_versions,
                    recovery=recovery,
                    validation_errors=report.errors,
                ),
            )

        # 正常完成后清理检查点，并在恢复报告中补全重执行集合。
        self.checkpoints.clear()
        if recovery.resumed:
            recovery.reexecuted_ids = [
                task.request.request_id
                for task in all_tasks
                if task.request.request_id not in set(recovery.committed_ids)
            ]
            recovery.disjoint = not (
                set(recovery.committed_ids) & set(recovery.reexecuted_ids)
            )
        return RunArtifacts(
            plan=plan,
            ledger=ledger,
            results=final_results,
            snapshot_versions=snapshot_versions,
            recovery=recovery,
            validation_errors=report.errors,
        )

    # ------------------------------------------------------------------
    # 快照发布
    # ------------------------------------------------------------------
    def _publish_snapshots(
        self,
        plan: AdmissionPlan,
        ledger: BudgetLedger,
        results: list[SampleResult],
    ) -> dict[str, int]:
        versions: dict[str, int] = {}
        samples_payload = [result.to_dict() for result in results]
        ledger_payload = ledger.to_dict()
        decisions_payload = plan.to_dict()

        for name, payload in (
            ("samples", samples_payload),
            ("ledger", ledger_payload),
            ("decisions", decisions_payload),
        ):
            next_version = self.snapshots.latest_version(name) + 1
            snapshot = self.snapshots.write(name, payload, expected_next_version=next_version)
            versions[name] = snapshot.version
        return versions

    # ------------------------------------------------------------------
    # 恢复辅助
    # ------------------------------------------------------------------
    def _build_recovery_report(
        self, checkpoint: Checkpoint | None, all_tasks: list[BatchTask]
    ) -> RecoveryReport:
        if checkpoint is None:
            return RecoveryReport(resumed=False)
        return RecoveryReport(
            resumed=True,
            committed_ids=list(checkpoint.committed_ids),
            next_request_id=checkpoint.next_request_id,
        )

    def _resume_start_index(
        self, checkpoint: Checkpoint, all_tasks: list[BatchTask]
    ) -> int:
        committed = set(checkpoint.committed_ids)
        for index, task in enumerate(all_tasks):
            if task.request.request_id not in committed:
                return index
        return len(all_tasks)

    def _next_request_id(
        self, committed_ids: list[str], all_tasks: list[BatchTask]
    ) -> str | None:
        committed = set(committed_ids)
        for task in all_tasks:
            if task.request.request_id not in committed:
                return task.request.request_id
        return None

    def _effective_request(self, task: BatchTask) -> Request:
        request = task.request
        if task.effective_length == request.target_length:
            return request
        return Request(
            request_id=request.request_id,
            prompt_tokens=request.prompt_tokens,
            sampling_params=request.sampling_params,
            target_length=task.effective_length,
            seed=request.seed,
            priority=request.priority,
        )


class InterruptedRun(RuntimeError):
    """强制终止演示路径：携带已完成的部分产物。"""

    def __init__(self, message: str, artifacts: RunArtifacts):
        super().__init__(message)
        self.artifacts = artifacts


def _sample_for_regret(request, vocab, logits, param_override=None):
    from .sampler import sample_request

    return sample_request(request, vocab, logits, param_override=param_override)
