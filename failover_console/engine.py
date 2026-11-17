"""Failover engine: budgeted probes, state transitions, and switch decisions."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .codes import BusinessCode
from .errors import BusinessError, ConsistencyDeviation, ReplayDifference
from .jsonl_logger import JsonlLogger
from .models import (
    VALID_FAULT_KINDS,
    BusinessEvent,
    CycleResult,
    InstanceRuntime,
    InstanceSpec,
    ProbeAttempt,
    ProbeBatch,
    ProbeResult,
    RecoveryReport,
    Scene,
    ServiceRuntime,
    StateTransition,
    SwitchDecision,
)
from .probe import ProbeRunner, ProbeTransport, SUCCESS
from .repository import SessionRepository
from .state_machine import (
    apply_probe_result,
    make_transition,
    mark_missing_probe_unknown,
)


@dataclass
class ReplayResult:
    ok: bool
    code: str
    snapshot_id: int | None
    scene_version: str | None
    differences: list[ReplayDifference] = field(default_factory=list)
    actual_trace: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "snapshot_id": self.snapshot_id,
            "scene_version": self.scene_version,
            "differences": [item.as_dict() for item in self.differences],
            "actual_trace": self.actual_trace,
        }


class FailoverEngine:
    def __init__(
        self,
        scene: Scene,
        db_path: str | Path,
        *,
        logger: JsonlLogger | None = None,
        transport: ProbeTransport | None = None,
        now_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
        switch_hook: Callable[[str], None] | None = None,
        fail_snapshot_writes: bool = False,
    ) -> None:
        self.scene = scene
        self.logger = logger
        self.transport = transport
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self.switch_hook = switch_hook
        self.repo = SessionRepository(
            db_path,
            logger=logger,
            fail_snapshot_writes=fail_snapshot_writes,
        )
        self.recovery: RecoveryReport = self.repo.open_scene(scene)
        self.runtime: dict[str, InstanceRuntime] = self.repo.load_instances()
        self.services: dict[str, ServiceRuntime] = self.repo.load_services()

    def close(self) -> None:
        self.repo.close()

    def __enter__(self) -> "FailoverEngine":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _validate_runtime_against_scene(self) -> None:
        expected_services = {service.service_id for service in self.scene.services}
        expected_instances = {instance.instance_id for instance in self.scene.instances()}
        runtime_services = set(self.services)
        runtime_instances = set(self.runtime)
        if expected_services != runtime_services or expected_instances != runtime_instances:
            raise BusinessError(
                BusinessCode.E_REPLAY,
                "当前场景与恢复后的会话实例集合不一致，拒绝继续运行",
                details={
                    "missing_services": sorted(expected_services - runtime_services),
                    "extra_services": sorted(runtime_services - expected_services),
                    "missing_instances": sorted(expected_instances - runtime_instances),
                    "extra_instances": sorted(runtime_instances - expected_instances),
                },
            )

    def reload_runtime(self) -> None:
        self.runtime = self.repo.load_instances()
        self.services = self.repo.load_services()

    def run_cycle(
        self,
        *,
        runtime_faults: dict[str, str] | None = None,
        persist_snapshot: bool = True,
        now_epoch_ms_override: int | None = None,
    ) -> CycleResult:
        if not self.recovery.scene_version_match:
            raise BusinessError(
                BusinessCode.E_REPLAY,
                "快照场景版本与当前场景不一致；状态已整体恢复，但必须先选择匹配场景才能运行",
                details={
                    "snapshot_id": self.recovery.snapshot_id,
                    "recovery": self.recovery.as_dict(),
                },
            )
        self._validate_runtime_against_scene()
        started_epoch_ms = (
            self.now_ms() if now_epoch_ms_override is None else int(now_epoch_ms_override)
        )
        effective_faults = self._effective_faults(runtime_faults)
        before = self.repo.export_state()
        run_id = f"run-{uuid.uuid4().hex}"
        round_no = self.repo.allocate_round()
        transitions: list[StateTransition] = []
        business_events: list[BusinessEvent] = []
        switch_decisions: list[SwitchDecision] = []
        try:
            probe_batch, probe_events = self._run_probes(
                self._runtime_instances(),
                effective_faults,
                run_id=run_id,
                round_no=round_no,
                now_epoch_ms=started_epoch_ms,
            )
            business_events.extend(probe_events)
            transitions.extend(
                self._apply_probe_results(
                    probe_batch,
                    instance_ids=set(self.runtime),
                    run_id=run_id,
                    round_no=round_no,
                    now_epoch_ms=started_epoch_ms,
                )
            )
            for service_id in sorted(self.services):
                decision = self._evaluate_service(
                    service_id,
                    run_id=run_id,
                    round_no=round_no,
                    runtime_faults=effective_faults,
                    now_epoch_ms=started_epoch_ms,
                    transitions=transitions,
                    business_events=business_events,
                    manual_approved=False,
                )
                if decision is not None:
                    switch_decisions.append(decision)
            after = self.repo.export_state()
            result = CycleResult(
                run_id=run_id,
                round_no=round_no,
                started_epoch_ms=started_epoch_ms,
                probe_batch=probe_batch,
                transitions=tuple(transitions),
                switch_decisions=tuple(switch_decisions),
                business_events=tuple(business_events),
                snapshot_id=None,
                runtime_faults=effective_faults,
            )
            trace = self._build_trace(result)
            snapshot_id = None
            if persist_snapshot:
                snapshot_id = self.repo.save_snapshot(
                    self.scene, before, after, trace, complete=True
                )
            result = CycleResult(
                run_id=result.run_id,
                round_no=result.round_no,
                started_epoch_ms=result.started_epoch_ms,
                probe_batch=result.probe_batch,
                transitions=result.transitions,
                switch_decisions=result.switch_decisions,
                business_events=result.business_events,
                snapshot_id=snapshot_id,
                runtime_faults=result.runtime_faults,
            )
            if self.logger:
                self.logger.info(
                    "CYCLE_COMPLETED",
                    "探测与切换周期完成",
                    run_id=run_id,
                    round_no=round_no,
                    codes=result.codes,
                    peak_global=probe_batch.peak_global,
                    peak_by_service=probe_batch.peak_by_service,
                    snapshot_id=snapshot_id,
                )
            self.reload_runtime()
            return result
        except BusinessError as exc:
            if self.logger:
                self.logger.business_error(exc, run_id=run_id, round_no=round_no)
            self.reload_runtime()
            raise
        except Exception as exc:
            if self.logger:
                self.logger.error(
                    "E_SNAPSHOT",
                    "周期执行异常",
                    run_id=run_id,
                    round_no=round_no,
                    error=f"{type(exc).__name__}: {exc}",
                )
            self.reload_runtime()
            raise

    def _effective_faults(self, runtime_faults: dict[str, str] | None) -> dict[str, str]:
        effective = self.scene.faults_by_instance()
        if runtime_faults:
            for instance_id, kind in runtime_faults.items():
                if instance_id not in self.runtime:
                    raise BusinessError(
                        BusinessCode.E_PROBE,
                        f"故障注入指向不存在的实例: {instance_id}",
                        details={"instance_id": instance_id},
                    )
                if kind not in VALID_FAULT_KINDS:
                    raise BusinessError(
                        BusinessCode.E_PROBE,
                        f"未知故障类型: {kind}",
                        details={"instance_id": instance_id, "kind": kind},
                    )
                effective[instance_id] = kind
        return dict(sorted(effective.items()))

    def _runtime_instances(self) -> list[InstanceSpec]:
        return [
            InstanceSpec(
                service_id=runtime.service_id,
                instance_id=runtime.instance_id,
                role=runtime.role,
                port=runtime.port,
                probe=runtime.probe,
            )
            for runtime in sorted(
                self.runtime.values(), key=lambda item: (item.service_id, item.instance_id)
            )
        ]

    def _instances_for_service(self, service_id: str) -> list[InstanceSpec]:
        return [
            InstanceSpec(
                service_id=runtime.service_id,
                instance_id=runtime.instance_id,
                role=runtime.role,
                port=runtime.port,
                probe=runtime.probe,
            )
            for runtime in sorted(
                (item for item in self.runtime.values() if item.service_id == service_id),
                key=lambda item: item.instance_id,
            )
        ]

    def _run_probes(
        self,
        instances: Iterable[InstanceSpec],
        faults: dict[str, str],
        *,
        run_id: str,
        round_no: int,
        now_epoch_ms: int,
    ) -> tuple[ProbeBatch, list[BusinessEvent]]:
        events: list[BusinessEvent] = []
        events_lock = threading.Lock()

        def on_exhausted(result: ProbeResult) -> None:
            details = {
                "attempts": [
                    {
                        "attempt_no": attempt.attempt_no,
                        "conclusion": attempt.conclusion,
                        "detail": attempt.detail,
                    }
                    for attempt in result.attempts
                ],
                "final_conclusion": result.final_conclusion,
                "fault_kind": faults.get(result.instance_id),
            }
            event = BusinessEvent(
                run_id=run_id,
                round_no=round_no,
                code=BusinessCode.E_PROBE.value,
                service_id=result.service_id,
                instance_id=result.instance_id,
                message=f"实例 {result.instance_id} 重试耗尽，结论 {result.final_conclusion}",
                details=details,
                at_epoch_ms=now_epoch_ms,
            )
            self.repo.record_business_event(event)
            with events_lock:
                events.append(event)
            if self.logger:
                self.logger.error(
                    BusinessCode.E_PROBE.value,
                    event.message,
                    service_id=result.service_id,
                    instance_id=result.instance_id,
                    details=details,
                )

        runner = ProbeRunner(
            self.transport,
            monotonic_ms=self.monotonic_ms,
            epoch_ms=self.now_ms,
            on_attempt=self.repo.add_probe_attempt,
            on_exhausted=on_exhausted,
        )
        batch = runner.run(
            instances,
            self.scene.probe_limits,
            faults,
            run_id=run_id,
            round_no=round_no,
        )
        return batch, events

    def _apply_probe_results(
        self,
        batch: ProbeBatch,
        *,
        instance_ids: set[str],
        run_id: str,
        round_no: int,
        now_epoch_ms: int,
    ) -> list[StateTransition]:
        result_map = {result.instance_id: result for result in batch.results}
        transitions: list[StateTransition] = []
        for instance_id in sorted(instance_ids):
            if instance_id not in self.runtime:
                continue
            runtime = self.runtime[instance_id]
            result = result_map.get(instance_id)
            change = None
            if result is None:
                change = mark_missing_probe_unknown(runtime)
                runtime.requires_reprobe = True
            else:
                change = apply_probe_result(runtime, result)
            runtime.updated_at_epoch_ms = now_epoch_ms
            if change is not None:
                transition = make_transition(
                    change=change,
                    run_id=run_id,
                    round_no=round_no,
                    service_id=runtime.service_id,
                    instance_id=runtime.instance_id,
                )
                self.repo.apply_instance_transition(runtime, transition)
                transitions.append(transition)
            else:
                self.repo.update_instance(runtime)
        return transitions

    def _trigger_for_service(self, service_id: str) -> tuple[str | None, list[str]]:
        instances = [
            item for item in self.runtime.values() if item.service_id == service_id
        ]
        primary = sorted(
            (item for item in instances if item.role == "primary"),
            key=lambda item: item.instance_id,
        )
        healthy = [
            item
            for item in instances
            if item.health_state == "healthy" and not item.isolated
        ]
        service = self.services[service_id]
        reasons: list[str] = []
        if len(primary) != 1:
            reasons.append("primary_topology_invalid")
        elif primary[0].health_state != "healthy" or primary[0].isolated:
            reasons.append("primary_unhealthy")
        if len(healthy) < service.policy.min_healthy_replicas:
            reasons.append("insufficient_healthy")
        return ("+".join(reasons) if reasons else None), [item.instance_id for item in healthy]

    def _select_target(self, service_id: str) -> InstanceRuntime | None:
        candidates = [
            item
            for item in self.runtime.values()
            if item.service_id == service_id
            and item.role == "standby"
            and item.health_state == "healthy"
            and not item.isolated
        ]
        candidates.sort(key=lambda item: (item.instance_id, item.port, item.version))
        return candidates[0] if candidates else None

    def _evaluate_service(
        self,
        service_id: str,
        *,
        run_id: str,
        round_no: int,
        runtime_faults: dict[str, str],
        now_epoch_ms: int,
        transitions: list[StateTransition],
        business_events: list[BusinessEvent],
        manual_approved: bool,
        preset_target: str | None = None,
        preset_trigger: str | None = None,
    ) -> SwitchDecision | None:
        service = self.services[service_id]
        trigger, healthy_ids = self._trigger_for_service(service_id)
        if preset_trigger is not None:
            trigger = preset_trigger
        if trigger is None:
            return None
        primary = sorted(
            (
                item
                for item in self.runtime.values()
                if item.service_id == service_id and item.role == "primary"
            ),
            key=lambda item: item.instance_id,
        )
        source = primary[0].instance_id if primary else None
        target = (
            self.runtime.get(preset_target) if preset_target is not None else self._select_target(service_id)
        )

        if service.policy.mode == "manual" and not manual_approved:
            request_id = f"manual-{uuid.uuid4().hex}"
            self.repo.register_pending(
                request_id=request_id,
                service_id=service_id,
                requested_epoch_ms=now_epoch_ms,
                trigger_reason=trigger,
                source_instance=source or "",
                target_instance=target.instance_id if target else None,
                policy_version=service.policy.version,
            )
            decision = SwitchDecision(
                request_id=request_id,
                run_id=run_id,
                service_id=service_id,
                code=BusinessCode.E_MANUAL_PENDING.value,
                source_instance=source,
                target_instance=target.instance_id if target else None,
                trigger_reason=trigger,
                policy_version=service.policy.version,
                switched=False,
                started_epoch_ms=now_epoch_ms,
                ended_epoch_ms=now_epoch_ms,
                details={"pending_request_id": request_id, "healthy_instances": healthy_ids},
            )
            self.repo.record_standalone_decision(decision)
            if self.logger:
                self.logger.info(
                    BusinessCode.E_MANUAL_PENDING.value,
                    "人工策略拒绝自动切换并登记待确认请求",
                    service_id=service_id,
                    request_id=request_id,
                    target=decision.target_instance,
                    trigger_reason=trigger,
                )
            return decision

        if target is None:
            decision = SwitchDecision(
                request_id=f"notarget-{uuid.uuid4().hex}",
                run_id=run_id,
                service_id=service_id,
                code=BusinessCode.E_NO_TARGET.value,
                source_instance=source,
                target_instance=None,
                trigger_reason=trigger,
                policy_version=service.policy.version,
                switched=False,
                started_epoch_ms=now_epoch_ms,
                ended_epoch_ms=now_epoch_ms,
                details={"healthy_instances": healthy_ids},
            )
            self.repo.record_standalone_decision(decision)
            if self.logger:
                self.logger.info(
                    BusinessCode.E_NO_TARGET.value,
                    "没有满足条件的确定性备用目标",
                    service_id=service_id,
                    trigger_reason=trigger,
                )
            return decision

        cooldown_start = service.cooldown_started_epoch_ms
        if cooldown_start is not None:
            elapsed = now_epoch_ms - cooldown_start
            if elapsed < service.policy.cooldown_ms:
                decision = SwitchDecision(
                    request_id=f"cooldown-{uuid.uuid4().hex}",
                    run_id=run_id,
                    service_id=service_id,
                    code=BusinessCode.E_COOLDOWN.value,
                    source_instance=source,
                    target_instance=target.instance_id,
                    trigger_reason=trigger,
                    policy_version=service.policy.version,
                    switched=False,
                    started_epoch_ms=now_epoch_ms,
                    ended_epoch_ms=now_epoch_ms,
                    details={
                        "cooldown_started_epoch_ms": cooldown_start,
                        "cooldown_ms": service.policy.cooldown_ms,
                        "remaining_ms": max(0, service.policy.cooldown_ms - elapsed),
                    },
                )
                self.repo.record_standalone_decision(decision)
                if self.logger:
                    self.logger.info(
                        BusinessCode.E_COOLDOWN.value,
                        "冷却窗口内抑制重复切换",
                        service_id=service_id,
                        target=decision.target_instance,
                        trigger_reason=trigger,
                        remaining_ms=decision.details["remaining_ms"],
                    )
                return decision

        if source is None:
            decision = SwitchDecision(
                request_id=f"isolate-{uuid.uuid4().hex}",
                run_id=run_id,
                service_id=service_id,
                code=BusinessCode.E_ISOLATE.value,
                source_instance=None,
                target_instance=target.instance_id,
                trigger_reason=trigger,
                policy_version=service.policy.version,
                switched=False,
                started_epoch_ms=now_epoch_ms,
                ended_epoch_ms=now_epoch_ms,
                details={
                    "deviations": [
                        {
                            "service_id": service_id,
                            "instance_id": "",
                            "field": "primary_role",
                            "expected": 1,
                            "actual": 0,
                        }
                    ]
                },
            )
            self.repo.record_standalone_decision(decision)
            return decision

        return self._perform_switch(
            service_id=service_id,
            source_instance=source,
            target=target,
            trigger_reason=trigger,
            run_id=run_id,
            round_no=round_no,
            runtime_faults=runtime_faults,
            now_epoch_ms=now_epoch_ms,
            transitions=transitions,
            business_events=business_events,
            manual_approved=manual_approved,
        )

    def _perform_switch(
        self,
        *,
        service_id: str,
        source_instance: str,
        target: InstanceRuntime,
        trigger_reason: str,
        run_id: str,
        round_no: int,
        runtime_faults: dict[str, str],
        now_epoch_ms: int,
        transitions: list[StateTransition],
        business_events: list[BusinessEvent],
        manual_approved: bool,
    ) -> SwitchDecision:
        service = self.services[service_id]
        request_id = f"switch-{uuid.uuid4().hex}"
        decision = SwitchDecision(
            request_id=request_id,
            run_id=run_id,
            service_id=service_id,
            code="SWITCHING",
            source_instance=source_instance,
            target_instance=target.instance_id,
            trigger_reason=trigger_reason,
            policy_version=service.policy.version,
            switched=False,
            started_epoch_ms=now_epoch_ms,
            ended_epoch_ms=None,
            details={"manual_approved": manual_approved},
        )
        try:
            self.repo.begin_switch(
                request_id=request_id,
                run_id=run_id,
                service_id=service_id,
                source_instance=source_instance,
                target_instance=target.instance_id,
                trigger_reason=trigger_reason,
                policy_version=service.policy.version,
                code="SWITCHING",
                details=decision.details,
                now_epoch_ms=now_epoch_ms,
            )
        except BusinessError as exc:
            if exc.code != BusinessCode.E_BUSY.value:
                raise
            busy = SwitchDecision(
                request_id=f"busy-{uuid.uuid4().hex}",
                run_id=run_id,
                service_id=service_id,
                code=BusinessCode.E_BUSY.value,
                source_instance=source_instance,
                target_instance=target.instance_id,
                trigger_reason=trigger_reason,
                policy_version=service.policy.version,
                switched=False,
                started_epoch_ms=now_epoch_ms,
                ended_epoch_ms=now_epoch_ms,
                details={"reason": "concurrent_switch"},
            )
            self.repo.record_standalone_decision(busy)
            if self.logger:
                self.logger.info(
                    BusinessCode.E_BUSY.value,
                    "服务已有进行中的切换事务，本请求被拒绝",
                    service_id=service_id,
                )
            return busy

        if self.switch_hook is not None:
            self.switch_hook(service_id)

        try:
            source_version, target_version = self.repo.atomic_apply_roles(
                request_id=request_id,
                service_id=service_id,
                source_instance=source_instance,
                target_instance=target.instance_id,
                now_epoch_ms=now_epoch_ms,
            )
        except BusinessError as exc:
            if exc.code != BusinessCode.E_BUSY.value:
                raise
            self.repo.finalize_switch(
                request_id=request_id,
                service_id=service_id,
                code=BusinessCode.E_BUSY.value,
                details={"reason": "role_changed_before_commit"},
                ended_epoch_ms=now_epoch_ms,
            )
            busy = SwitchDecision(
                request_id=request_id,
                run_id=run_id,
                service_id=service_id,
                code=BusinessCode.E_BUSY.value,
                source_instance=source_instance,
                target_instance=target.instance_id,
                trigger_reason=trigger_reason,
                policy_version=service.policy.version,
                switched=False,
                started_epoch_ms=now_epoch_ms,
                ended_epoch_ms=now_epoch_ms,
                details={"reason": "role_changed_before_commit"},
            )
            if self.logger:
                self.logger.info(
                    BusinessCode.E_BUSY.value,
                    "角色在提交前发生变化，切换请求被拒绝",
                    service_id=service_id,
                )
            return busy
        self.reload_runtime()

        post_batch, post_events = self._run_probes(
            self._instances_for_service(service_id),
            runtime_faults,
            run_id=run_id,
            round_no=round_no,
            now_epoch_ms=now_epoch_ms,
        )
        business_events.extend(post_events)
        transitions.extend(
            self._apply_probe_results(
                post_batch,
                instance_ids={
                    item.instance_id
                    for item in self.runtime.values()
                    if item.service_id == service_id
                },
                run_id=run_id,
                round_no=round_no,
                now_epoch_ms=now_epoch_ms,
            )
        )
        deviations = self._check_post_switch_consistency(
            service_id=service_id,
            source_instance=source_instance,
            target_instance=target.instance_id,
        )
        code = BusinessCode.SWITCHED.value if not deviations else BusinessCode.E_ISOLATE.value
        details = {
            "trigger_reason": trigger_reason,
            "source_version_after": source_version,
            "target_version_after": target_version,
            "deviations": [deviation.as_dict() for deviation in deviations],
            "policy_version": service.policy.version,
            "manual_approved": manual_approved,
        }
        self.repo.finalize_switch(
            request_id=request_id,
            service_id=service_id,
            code=code,
            details=details,
            ended_epoch_ms=now_epoch_ms,
        )
        if deviations:
            event = BusinessEvent(
                run_id=run_id,
                round_no=round_no,
                code=BusinessCode.E_ISOLATE.value,
                service_id=service_id,
                instance_id=None,
                message="切换后一致性校验失败",
                details={"deviations": details["deviations"]},
                at_epoch_ms=now_epoch_ms,
            )
            self.repo.record_business_event(event)
            business_events.append(event)
            if self.logger:
                self.logger.error(
                    BusinessCode.E_ISOLATE.value,
                    event.message,
                    service_id=service_id,
                    deviations=details["deviations"],
                )
        elif self.logger:
            self.logger.info(
                BusinessCode.SWITCHED.value,
                "服务切换完成",
                service_id=service_id,
                source=source_instance,
                target=target.instance_id,
                trigger_reason=trigger_reason,
                policy_version=service.policy.version,
            )
        self.reload_runtime()
        return SwitchDecision(
            request_id=request_id,
            run_id=run_id,
            service_id=service_id,
            code=code,
            source_instance=source_instance,
            target_instance=target.instance_id,
            trigger_reason=trigger_reason,
            policy_version=service.policy.version,
            switched=True,
            started_epoch_ms=now_epoch_ms,
            ended_epoch_ms=now_epoch_ms,
            details=details,
        )

    def _check_post_switch_consistency(
        self, *, service_id: str, source_instance: str, target_instance: str
    ) -> list[ConsistencyDeviation]:
        instances = sorted(
            (item for item in self.runtime.values() if item.service_id == service_id),
            key=lambda item: item.instance_id,
        )
        service = self.services[service_id]
        primary = [item for item in instances if item.role == "primary"]
        healthy = [item for item in instances if item.health_state == "healthy" and not item.isolated]
        deviations: list[ConsistencyDeviation] = []
        if len(primary) != 1:
            deviations.append(
                ConsistencyDeviation(
                    service_id,
                    "",
                    "primary_role_count",
                    1,
                    len(primary),
                )
            )
        if len(healthy) < service.policy.min_healthy_replicas:
            deviations.append(
                ConsistencyDeviation(
                    service_id,
                    "",
                    "healthy_replicas",
                    service.policy.min_healthy_replicas,
                    len(healthy),
                )
            )
        source = self.runtime.get(source_instance)
        if source is None or not source.isolated:
            deviations.append(
                ConsistencyDeviation(
                    service_id,
                    source_instance,
                    "isolated",
                    True,
                    source.isolated if source else None,
                )
            )
        target = self.runtime.get(target_instance)
        if target is None or target.role != "primary":
            deviations.append(
                ConsistencyDeviation(
                    service_id,
                    target_instance,
                    "role",
                    "primary",
                    target.role if target else None,
                )
            )
        return deviations

    def approve_manual(
        self,
        request_id: str,
        *,
        runtime_faults: dict[str, str] | None = None,
        now_epoch_ms_override: int | None = None,
    ) -> CycleResult:
        pending = self.repo.get_pending(request_id)
        if pending is None:
            raise BusinessError(
                BusinessCode.E_MANUAL_PENDING,
                "待确认请求不存在",
                details={"request_id": request_id},
            )
        if pending["status"] != "pending":
            raise BusinessError(
                BusinessCode.E_MANUAL_PENDING,
                "待确认请求已处理",
                details={"request_id": request_id, "status": pending["status"]},
            )
        service_id = str(pending["service_id"])
        source = str(pending["source_instance"])
        target_id = pending.get("target_instance")
        target = self.runtime.get(str(target_id)) if target_id else None
        if (
            target is None
            or target.role != "standby"
            or target.health_state != "healthy"
            or target.isolated
        ):
            raise BusinessError(
                BusinessCode.E_NO_TARGET,
                "待确认请求的目标实例当前不可用",
                details={"request_id": request_id, "target": target_id},
            )
        now = self.now_ms() if now_epoch_ms_override is None else int(now_epoch_ms_override)
        faults = self._effective_faults(runtime_faults)
        run_id = f"manual-{uuid.uuid4().hex}"
        round_no = self.repo.allocate_round()
        before = self.repo.export_state()
        transitions: list[StateTransition] = []
        events: list[BusinessEvent] = []
        decision = self._perform_switch(
            service_id=service_id,
            source_instance=source,
            target=target,
            trigger_reason=str(pending["trigger_reason"]),
            run_id=run_id,
            round_no=round_no,
            runtime_faults=faults,
            now_epoch_ms=now,
            transitions=transitions,
            business_events=events,
            manual_approved=True,
        )
        if decision.switched:
            self.repo.set_pending_status(request_id, "approved")
        after = self.repo.export_state()
        batch = ProbeBatch((), 0, {})
        result = CycleResult(
            run_id=run_id,
            round_no=round_no,
            started_epoch_ms=now,
            probe_batch=batch,
            transitions=tuple(transitions),
            switch_decisions=(decision,),
            business_events=tuple(events),
            snapshot_id=self.repo.save_snapshot(
                self.scene, before, after, self._build_trace(
                    CycleResult(
                        run_id=run_id,
                        round_no=round_no,
                        started_epoch_ms=now,
                        probe_batch=batch,
                        transitions=tuple(transitions),
                        switch_decisions=(decision,),
                        business_events=tuple(events),
                        snapshot_id=None,
                        runtime_faults=faults,
                    )
                ), complete=True
            ),
            runtime_faults=faults,
        )
        return result

    def _build_trace(self, result: CycleResult) -> dict[str, Any]:
        probe_results = []
        for item in sorted(
            result.probe_batch.results, key=lambda entry: (entry.service_id, entry.instance_id)
        ):
            probe_results.append(
                {
                    "service_id": item.service_id,
                    "instance_id": item.instance_id,
                    "attempts": [
                        {
                            "attempt_no": attempt.attempt_no,
                            "conclusion": attempt.conclusion,
                        }
                        for attempt in item.attempts
                    ],
                    "final_conclusion": item.final_conclusion,
                }
            )
        events = sorted(
            (event.trace_key() for event in result.business_events),
            key=lambda item: (
                item["code"],
                item["service_id"] or "",
                item["instance_id"] or "",
                json.dumps(item["details"], sort_keys=True, ensure_ascii=False),
            ),
        )
        return {
            "run_id": result.run_id,
            "round_no": result.round_no,
            "started_epoch_ms": result.started_epoch_ms,
            "runtime_faults": dict(sorted(result.runtime_faults.items())),
            "probe_results": probe_results,
            "transitions": [item.trace_key() for item in result.transitions],
            "switch_decisions": [item.trace_key() for item in result.switch_decisions],
            "business_events": events,
            "codes": list(dict.fromkeys(result.codes)),
            "peak_global": result.probe_batch.peak_global,
            "peak_by_service": dict(sorted(result.probe_batch.peak_by_service.items())),
        }

    def replay_latest(self) -> ReplayResult:
        snapshot = self.repo.latest_snapshot()
        if snapshot is None:
            return self._replay_failure("没有可重放的快照")
        expected_trace = snapshot.payload.get("trace")
        before = snapshot.payload.get("before")
        if not isinstance(expected_trace, dict) or not isinstance(before, dict):
            return self._replay_failure(
                "快照缺少 trace/before 段，无法重放", snapshot_id=snapshot.snapshot_id
            )
        config_differences = self._scene_config_differences(snapshot.payload)
        if config_differences:
            if self.logger:
                self.logger.error(
                    BusinessCode.E_REPLAY.value,
                    "场景版本或策略参数不一致，无法重放",
                    snapshot_id=snapshot.snapshot_id,
                    differences=[item.as_dict() for item in config_differences],
                )
            return ReplayResult(
                ok=False,
                code=BusinessCode.E_REPLAY.value,
                snapshot_id=snapshot.snapshot_id,
                scene_version=snapshot.scene_version,
                differences=config_differences,
            )

        faults = {
            str(key): str(value)
            for key, value in expected_trace.get("runtime_faults", {}).items()
        }
        with tempfile.TemporaryDirectory(prefix="failover-replay-") as temp_dir:
            replay_engine = FailoverEngine(
                self.scene,
                Path(temp_dir) / "replay.sqlite",
                logger=None,
                transport=self.transport,
                now_ms=self.now_ms,
                monotonic_ms=self.monotonic_ms,
            )
            try:
                replay_engine.repo.replace_runtime_from_state(
                    before,
                    scene_version=snapshot.scene_version,
                    scene_fingerprint=snapshot.scene_fingerprint,
                )
                replay_engine.reload_runtime()
                actual = replay_engine.run_cycle(
                    runtime_faults=faults,
                    persist_snapshot=False,
                    now_epoch_ms_override=int(expected_trace["started_epoch_ms"]),
                )
                actual_trace = replay_engine._build_trace(actual)
            finally:
                replay_engine.close()

        differences = self._compare_traces(expected_trace, actual_trace)
        if differences:
            if self.logger:
                self.logger.error(
                    BusinessCode.E_REPLAY.value,
                    "快照重放结果不一致",
                    snapshot_id=snapshot.snapshot_id,
                    differences=[item.as_dict() for item in differences],
                )
            return ReplayResult(
                ok=False,
                code=BusinessCode.E_REPLAY.value,
                snapshot_id=snapshot.snapshot_id,
                scene_version=snapshot.scene_version,
                differences=differences,
                actual_trace=actual_trace,
            )
        if self.logger:
            self.logger.info(
                BusinessCode.OK_REPLAY.value,
                "快照重放校验一致",
                snapshot_id=snapshot.snapshot_id,
                scene_version=snapshot.scene_version,
            )
        return ReplayResult(
            ok=True,
            code=BusinessCode.OK_REPLAY.value,
            snapshot_id=snapshot.snapshot_id,
            scene_version=snapshot.scene_version,
            actual_trace=actual_trace,
        )

    def _replay_failure(self, message: str, *, snapshot_id: int | None = None) -> ReplayResult:
        difference = ReplayDifference("snapshot", "snapshot", "available", "missing")
        if self.logger:
            self.logger.error(BusinessCode.E_REPLAY.value, message, snapshot_id=snapshot_id)
        return ReplayResult(
            ok=False,
            code=BusinessCode.E_REPLAY.value,
            snapshot_id=snapshot_id,
            scene_version=None,
            differences=[difference],
        )

    def _scene_config_differences(self, payload: dict[str, Any]) -> list[ReplayDifference]:
        expected = payload.get("scene_config")
        current = self.scene.canonical_payload()
        differences: list[ReplayDifference] = []
        if not isinstance(expected, dict):
            if payload.get("scene_version") != self.scene.scene_version:
                differences.append(
                    ReplayDifference(
                        "scene_version",
                        "scene_version",
                        payload.get("scene_version"),
                        self.scene.scene_version,
                    )
                )
            if payload.get("scene_fingerprint") != self.scene.fingerprint:
                differences.append(
                    ReplayDifference(
                        "scene_fingerprint",
                        "scene_fingerprint",
                        payload.get("scene_fingerprint"),
                        self.scene.fingerprint,
                    )
                )
            return differences
        self._walk_diff(expected, current, "", differences)
        return differences

    def _walk_diff(
        self,
        expected: Any,
        actual: Any,
        path: str,
        differences: list[ReplayDifference],
    ) -> None:
        if type(expected) is not type(actual):
            differences.append(ReplayDifference("config", path or "$", expected, actual))
            return
        if isinstance(expected, dict):
            for key in sorted(set(expected) | set(actual)):
                child = f"{path}.{key}" if path else key
                if key not in expected:
                    differences.append(ReplayDifference("config", child, "<missing>", actual[key]))
                elif key not in actual:
                    differences.append(ReplayDifference("config", child, expected[key], "<missing>"))
                else:
                    self._walk_diff(expected[key], actual[key], child, differences)
            return
        if isinstance(expected, list):
            if len(expected) != len(actual):
                differences.append(
                    ReplayDifference(
                        "config",
                        f"{path}.length" if path else "length",
                        len(expected),
                        len(actual),
                    )
                )
                return
            for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
                self._walk_diff(
                    expected_item,
                    actual_item,
                    f"{path}.{index}" if path else str(index),
                    differences,
                )
            return
        if expected != actual:
            differences.append(ReplayDifference("config", path or "$", expected, actual))

    @staticmethod
    def _compare_traces(
        expected: dict[str, Any], actual: dict[str, Any]
    ) -> list[ReplayDifference]:
        differences: list[ReplayDifference] = []
        expected_probes = {
            (item.get("service_id"), item.get("instance_id")): item
            for item in expected.get("probe_results", [])
        }
        actual_probes = {
            (item.get("service_id"), item.get("instance_id")): item
            for item in actual.get("probe_results", [])
        }
        for key in sorted(set(expected_probes) | set(actual_probes), key=str):
            expected_item = expected_probes.get(key)
            actual_item = actual_probes.get(key)
            normalized_expected = (
                {
                    "attempts": [
                        item.get("conclusion")
                        for item in expected_item.get("attempts", [])
                    ],
                    "final_conclusion": expected_item.get("final_conclusion"),
                }
                if expected_item
                else None
            )
            normalized_actual = (
                {
                    "attempts": [
                        item.get("conclusion") for item in actual_item.get("attempts", [])
                    ],
                    "final_conclusion": actual_item.get("final_conclusion"),
                }
                if actual_item
                else None
            )
            if normalized_expected != normalized_actual:
                differences.append(
                    ReplayDifference(
                        "probe",
                        f"probe_results.{key[0]}.{key[1]}",
                        normalized_expected,
                        normalized_actual,
                    )
                )
        for field_name in ("transitions", "switch_decisions", "business_events"):
            expected_items = expected.get(field_name, [])
            actual_items = actual.get(field_name, [])
            if expected_items != actual_items:
                differences.append(
                    ReplayDifference(
                        "trace",
                        field_name,
                        expected_items,
                        actual_items,
                    )
                )
        expected_codes = expected.get("codes", [])
        actual_codes = actual.get("codes", [])
        if expected_codes != actual_codes:
            differences.append(
                ReplayDifference("trace", "codes", expected_codes, actual_codes)
            )
        return differences

    def status(self) -> dict[str, Any]:
        return {
            "scene_version": self.scene.scene_version,
            "snapshot_fingerprint": self.scene.fingerprint,
            "recovery": self.recovery.as_dict(),
            "instances": [
                item.as_dict()
                for item in sorted(
                    self.runtime.values(), key=lambda entry: (entry.service_id, entry.instance_id)
                )
            ],
            "services": [
                item.as_dict() for item in sorted(self.services.values(), key=lambda entry: entry.policy.service_id)
            ],
            "pending_requests": self.repo.list_pending(),
        }
