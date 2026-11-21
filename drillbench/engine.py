from __future__ import annotations

import os
import queue
import threading
from typing import Any

from .clock import LogicalClock
from .errors import BusinessError
from .http_client import AbortController, HTTPResult, body_is_json, perform_http_get
from .http_service import LocalHTTPService
from .models import (
    ERROR_BAD_STATUS,
    ERROR_CLOCK_ROLLBACK,
    ERROR_CONN_REFUSED,
    ERROR_CONNECTION_DROPPED,
    ERROR_DATA_CORRUPTION,
    ERROR_EXPECTATION_MISMATCH,
    ERROR_INTERNAL,
    ERROR_MALFORMED_BODY,
    ERROR_SERVICE_REJECTED,
    ERROR_STEP_TIMEOUT,
    ERROR_TRANSPORT,
    OUTCOME_ABORTED,
    OUTCOME_OBSERVED,
    OUTCOME_TIMEOUT_AFTER_START,
    STAGE_ABORTED,
    STAGE_OBSERVED,
    STAGE_PENDING,
    STAGE_RUNNING,
    STAGE_TIMED_OUT,
    Injection,
    PlanStep,
    Route,
    Snapshot,
    StepObservation,
    status_event,
)
from .store import StateStore
from .util import sha256_text


class DrillEngine:
    def __init__(self, store: StateStore, *, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    def _load_run_context(
        self,
        *,
        plan_name: str | None,
    ) -> tuple[dict[str, Any], Snapshot]:
        ensured = self.store.ensure_run(run_id=self.run_id, plan_name=plan_name)
        run = ensured["run"]
        _, snapshot = self.store.get_snapshot(int(run["version"]))
        return run, snapshot

    def _ready_steps(
        self,
        snapshot: Snapshot,
        step_rows: list[dict[str, Any]],
    ) -> list[PlanStep]:
        by_seq = {int(row["seq"]): row for row in step_rows}
        ready: list[PlanStep] = []
        for step in snapshot.plan.steps:
            row = by_seq[step.seq]
            if row["stage"] != STAGE_PENDING:
                continue
            if all(
                by_seq[dep]["stage"] in {"OBSERVED", "SKIPPED"}
                for dep in step.depends_on
            ):
                ready.append(step)
        return ready

    def _classify_half_committed_steps(
        self,
        *,
        version: int,
        step_rows: list[dict[str, Any]],
    ) -> tuple[list[int], list[int]]:
        stale: list[int] = []
        active: list[int] = []
        for row in step_rows:
            if row["stage"] != STAGE_RUNNING:
                continue
            seq = int(row["seq"])
            lock = self.store.step_lock(version, seq)
            try:
                lock.acquire()
            except BusinessError as exc:
                if exc.code == "E_LOCKED":
                    active.append(seq)
                    continue
                raise
            else:
                stale.append(seq)
            finally:
                lock.release()
        return stale, active

    def _real_delay_ms(self, clock: LogicalClock, route: Route) -> int:
        if route.delay_logical_ms <= 0:
            return 0
        return max(
            1,
            int(
                round(
                    route.delay_logical_ms * clock.real_per_logical
                )
            ),
        )

    def _injection_trigger(
        self,
        snapshot: Snapshot,
        step: PlanStep,
        start_ms: int,
        clock: LogicalClock,
    ) -> tuple[tuple[Injection, ...], int | None]:
        injections = snapshot.injection_map_by_step.get(step.seq, ())
        if not injections:
            return (), None
        trigger_ms = max(start_ms, min(injection.at_ms for injection in injections))
        deadline = start_ms + step.timeout_logical_ms
        if trigger_ms >= deadline:
            return (), trigger_ms
        if clock.current_ms < trigger_ms:
            clock.advance_to(trigger_ms)
        return injections, trigger_ms

    def _abort_and_wait(
        self,
        *,
        controller: AbortController,
        result_queue: queue.Queue[HTTPResult],
        thread: threading.Thread,
    ) -> None:
        controller.abort()
        thread.join(timeout=1.0)

    def _execute_http_step(
        self,
        *,
        snapshot: Snapshot,
        step: PlanStep,
        route: Route,
        clock: LogicalClock,
        start_ms: int,
    ) -> StepObservation:
        injections, trigger_ms = self._injection_trigger(
            snapshot, step, start_ms, clock
        )
        injection_ids = tuple(
            injection.injection_id
            for injection in sorted(
                injections,
                key=lambda item: (item.at_ms, item.injection_id),
            )
        )
        injection_types = tuple(
            injection.injection_type
            for injection in sorted(
                injections,
                key=lambda item: (item.at_ms, item.injection_id),
            )
        )
        rollback = next(
            (
                injection
                for injection in injections
                if injection.injection_type == "clock_rollback"
            ),
            None,
        )
        if rollback is not None:
            before, after = clock.rollback(rollback.rollback_ms)
            guard_ms = before
            observation = StepObservation(
                step=step.seq,
                attempt_no=1,
                code=ERROR_CLOCK_ROLLBACK,
                outcome_class=OUTCOME_ABORTED,
                stage=STAGE_ABORTED,
                event="CLOCK_ROLLBACK_DETECTED",
                response_status=None,
                body_sha256=None,
                injected_at_ms=trigger_ms,
                injections=injection_ids,
                detail={
                    "error": ERROR_CLOCK_ROLLBACK,
                    "clock_before_ms": before,
                    "clock_after_rollback_ms": after,
                    "monotonic_guard_ms": guard_ms,
                    "rollback_ms": rollback.rollback_ms,
                    "injection": rollback.injection_id,
                },
                started_at_ms=start_ms,
                ended_at_ms=guard_ms,
            )
            clock.current_ms = guard_ms
            return observation

        deadline_ms = start_ms + step.timeout_logical_ms
        planned_end_ms = start_ms + route.delay_logical_ms
        service = LocalHTTPService(snapshot.routes).start()
        controller = AbortController()
        result_queue: queue.Queue[HTTPResult] = queue.Queue(maxsize=1)
        real_delay_ms = self._real_delay_ms(clock, route)
        io_timeout_seconds = max(
            0.05,
            clock.real_seconds_for_logical(
                min(step.timeout_logical_ms, 250)
            ),
        )

        def worker() -> None:
            result = perform_http_get(
                host=service.host,
                port=service.port,
                path=route.path,
                injection_ids=injection_ids,
                injection_types=injection_types,
                real_delay_ms=real_delay_ms,
                io_timeout_seconds=io_timeout_seconds,
                controller=controller,
            )
            try:
                result_queue.put_nowait(result)
            except queue.Full:
                pass

        thread = threading.Thread(
            target=worker,
            name=f"drill-step-{step.seq}",
            daemon=True,
        )
        thread.start()
        result: HTTPResult | None = None
        timed_out = False
        try:
            while True:
                try:
                    result = result_queue.get_nowait()
                    break
                except queue.Empty:
                    pass
                if clock.current_ms >= deadline_ms:
                    self._abort_and_wait(
                        controller=controller,
                        result_queue=result_queue,
                        thread=thread,
                    )
                    timed_out = True
                    break
                if clock.current_ms < min(planned_end_ms, deadline_ms):
                    next_ms = min(
                        clock.current_ms + snapshot.clock.tick_logical_ms,
                        planned_end_ms,
                        deadline_ms,
                    )
                    clock.advance_to(next_ms)
                else:
                    remaining_ms = max(0, deadline_ms - clock.current_ms)
                    thread.join(
                        timeout=max(
                            0.02,
                            clock.real_seconds_for_logical(remaining_ms) + 0.02,
                        )
                    )
                    try:
                        result = result_queue.get_nowait()
                        break
                    except queue.Empty:
                        if clock.current_ms < deadline_ms:
                            clock.advance_to(deadline_ms)
                        self._abort_and_wait(
                            controller=controller,
                            result_queue=result_queue,
                            thread=thread,
                        )
                        timed_out = True
                        break
        finally:
            if not timed_out:
                thread.join(timeout=max(0.05, io_timeout_seconds * 2))
                try:
                    result = result_queue.get_nowait()
                except queue.Empty:
                    pass
            service.close()

        if timed_out:
            return StepObservation(
                step=step.seq,
                attempt_no=1,
                code=ERROR_STEP_TIMEOUT,
                outcome_class=OUTCOME_TIMEOUT_AFTER_START,
                stage=STAGE_TIMED_OUT,
                event="LOGICAL_DEADLINE_REACHED",
                response_status=None,
                body_sha256=None,
                injected_at_ms=trigger_ms,
                injections=injection_ids,
                detail={
                    "error": ERROR_STEP_TIMEOUT,
                    "started_at_ms": start_ms,
                    "deadline_ms": deadline_ms,
                    "logical_elapsed_ms": deadline_ms - start_ms,
                    "timeout_budget_ms": step.timeout_logical_ms,
                    "route_delay_logical_ms": route.delay_logical_ms,
                    "active_abort": True,
                    "wall_clock_used_for_decision": False,
                },
                started_at_ms=start_ms,
                ended_at_ms=deadline_ms,
            )

        if result is None:
            result = HTTPResult(
                ok=False,
                error_code=ERROR_INTERNAL,
                error_detail="HTTP worker produced no result",
                status=None,
                headers={},
                body=b"",
            )

        if clock.current_ms < planned_end_ms:
            clock.advance_to(min(planned_end_ms, deadline_ms))
        ended_at_ms = clock.current_ms
        actual_body_sha = sha256_text(
            result.body.decode("utf-8", errors="replace")
        )
        body_valid = True
        body_parse_detail: str | None = None
        content_type = result.headers.get("content-type", "")
        if "json" in content_type.lower():
            body_valid, body_parse_detail = body_is_json(result.body)
        elif not result.body:
            body_valid = False
            body_parse_detail = "empty response body"

        code: str | None = None
        stage = STAGE_OBSERVED
        outcome_class = OUTCOME_OBSERVED
        event = step.expected_event
        error_detail = result.error_detail

        effect = result.headers.get("x-drill-effect", "none")
        if result.error_code is not None:
            stage = STAGE_ABORTED
            outcome_class = OUTCOME_ABORTED
            if "network_outage" in injection_types:
                code = ERROR_CONN_REFUSED
            elif result.error_code == ERROR_CONNECTION_DROPPED and route.mode == "disconnect":
                code = ERROR_CONNECTION_DROPPED
            elif result.error_code in {
                ERROR_CONN_REFUSED,
                ERROR_CONNECTION_DROPPED,
                ERROR_TRANSPORT,
            }:
                code = result.error_code
            else:
                code = result.error_code
        elif "service_reject" in injection_types or effect == "service_reject":
            code = ERROR_SERVICE_REJECTED
            stage = STAGE_ABORTED
            outcome_class = OUTCOME_ABORTED
        elif "data_corruption" in injection_types:
            code = ERROR_DATA_CORRUPTION
            stage = STAGE_ABORTED
            outcome_class = OUTCOME_ABORTED
        elif (
            route.mode != "malformed"
            and body_valid
            and actual_body_sha != route.body_sha256
        ):
            code = ERROR_DATA_CORRUPTION
            stage = STAGE_ABORTED
            outcome_class = OUTCOME_ABORTED
        elif route.mode == "malformed" or not body_valid:
            code = ERROR_MALFORMED_BODY
            stage = STAGE_ABORTED
            outcome_class = OUTCOME_ABORTED
        elif result.status is None:
            code = ERROR_TRANSPORT
            stage = STAGE_ABORTED
            outcome_class = OUTCOME_ABORTED
        else:
            actual_event = status_event(result.status)
            actual_events = {"CONNECT_OK", actual_event}
            if body_valid:
                actual_events.add("BODY_VALID")
            if (
                step.expected_event.startswith("HTTP_")
                and step.expected_event != actual_event
            ):
                code = ERROR_BAD_STATUS
                stage = STAGE_ABORTED
                outcome_class = OUTCOME_ABORTED
            elif step.expected_event == "BODY_VALID" and not body_valid:
                code = ERROR_MALFORMED_BODY
                stage = STAGE_ABORTED
                outcome_class = OUTCOME_ABORTED
            elif step.expected_event not in actual_events:
                code = ERROR_EXPECTATION_MISMATCH
                stage = STAGE_ABORTED
                outcome_class = OUTCOME_ABORTED

        if code is None:
            code = OUTCOME_OBSERVED
        elif code not in {
            ERROR_CONN_REFUSED,
            ERROR_CONNECTION_DROPPED,
            ERROR_STEP_TIMEOUT,
            ERROR_BAD_STATUS,
            ERROR_MALFORMED_BODY,
            ERROR_DATA_CORRUPTION,
            ERROR_SERVICE_REJECTED,
            ERROR_CLOCK_ROLLBACK,
            ERROR_EXPECTATION_MISMATCH,
            ERROR_TRANSPORT,
            ERROR_INTERNAL,
        }:
            code = ERROR_INTERNAL

        detail = {
            "error": code if stage == STAGE_ABORTED else None,
            "expected_event": step.expected_event,
            "actual_status": result.status,
            "actual_events": sorted(
                {
                    "CONNECT_OK",
                    status_event(result.status)
                    if result.status is not None
                    else "NO_STATUS",
                    *(["BODY_VALID"] if body_valid else []),
                }
            ),
            "body_valid": body_valid,
            "body_parse_detail": body_parse_detail,
            "body_sha256": actual_body_sha,
            "expected_body_sha256": route.body_sha256,
            "route_path": route.path,
            "route_mode": route.mode,
            "route_delay_logical_ms": route.delay_logical_ms,
            "transport_error": error_detail,
            "injection_effect": effect,
            "started_at_ms": start_ms,
            "ended_at_ms": ended_at_ms,
            "logical_elapsed_ms": ended_at_ms - start_ms,
            "wall_clock_used_for_decision": False,
        }
        return StepObservation(
            step=step.seq,
            attempt_no=1,
            code=code,
            outcome_class=outcome_class,
            stage=stage,
            event=event,
            response_status=result.status,
            body_sha256=actual_body_sha,
            injected_at_ms=trigger_ms,
            injections=injection_ids,
            detail=detail,
            started_at_ms=start_ms,
            ended_at_ms=ended_at_ms,
        )

    def _execute_step(
        self,
        *,
        snapshot: Snapshot,
        step: PlanStep,
        route: Route,
        clock: LogicalClock,
        start_ms: int,
    ) -> StepObservation:
        try:
            return self._execute_http_step(
                snapshot=snapshot,
                step=step,
                route=route,
                clock=clock,
                start_ms=start_ms,
            )
        except BusinessError:
            raise
        except Exception as exc:
            return StepObservation(
                step=step.seq,
                attempt_no=1,
                code=ERROR_INTERNAL,
                outcome_class=OUTCOME_ABORTED,
                stage=STAGE_ABORTED,
                event="INTERNAL_ERROR",
                response_status=None,
                body_sha256=None,
                injected_at_ms=None,
                injections=(),
                detail={
                    "error": ERROR_INTERNAL,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
                started_at_ms=start_ms,
                ended_at_ms=clock.current_ms,
            )

    def run(
        self,
        *,
        plan_name: str | None = None,
        resume: bool = False,
        skip_steps: tuple[int, ...] = (),
        once: bool = False,
        crash_at_step: int | None = None,
        crash_phase: str | None = None,
    ) -> dict[str, Any]:
        if crash_phase not in {None, "after-claim", "after-response"}:
            raise BusinessError(
                "E_BAD_ARGUMENT",
                "crash_phase must be after-claim or after-response",
                {"crash_phase": crash_phase},
            )
        run, snapshot = self._load_run_context(plan_name=plan_name)
        version = int(run["version"])
        step_rows = self.store.list_steps(self.run_id)
        stale_half_steps, active_half_steps = self._classify_half_committed_steps(
            version=version,
            step_rows=step_rows,
        )
        recovery_result: dict[str, Any] | None = None
        if resume:
            if active_half_steps:
                raise BusinessError(
                    "E_RUN_ACTIVE",
                    "another process is still executing this run",
                    {
                        "run_id": self.run_id,
                        "active_steps": active_half_steps,
                    },
                )
            recovery_result = self.store.recover_run(self.run_id)
            step_rows = self.store.list_steps(self.run_id)
        elif stale_half_steps:
            raise BusinessError(
                "E_RESUME_REQUIRED",
                "run has half-committed steps; use resume to replay them",
                {
                    "run_id": self.run_id,
                    "half_committed_steps": stale_half_steps,
                },
            )

        run = self.store.get_run(self.run_id)
        clock = LogicalClock.from_spec(snapshot.clock, int(run["logical_clock_ms"]))
        skipped_now: list[int] = []
        step_by_seq = snapshot.plan.step_map
        for step_seq in sorted(set(skip_steps)):
            if step_seq not in step_by_seq:
                raise BusinessError(
                    "E_BAD_STEP",
                    f"skip step does not exist: {step_seq}",
                    {"step": step_seq},
                )
            result = self.store.skip_step(
                run_id=self.run_id,
                version=version,
                step_seq=step_seq,
                clock_ms=clock.current_ms,
                step_is_skippable=step_by_seq[step_seq].skippable,
            )
            if result["changed"]:
                skipped_now.append(step_seq)
            else:
                skipped_now.append(step_seq)

        executed_steps: list[int] = []
        lock_wait_happened = False
        while True:
            step_rows = self.store.list_steps(self.run_id)
            run = self.store.get_run(self.run_id)
            clock.current_ms = max(clock.current_ms, int(run["logical_clock_ms"]))
            ready = self._ready_steps(snapshot, step_rows)
            if not ready:
                break
            progressed = False
            for step in sorted(
                ready,
                key=lambda item: (
                    snapshot.clock.window_map[item.seq].start_ms,
                    item.seq,
                ),
            ):
                lock = self.store.step_lock(version, step.seq)
                try:
                    lock.acquire()
                except BusinessError as exc:
                    if exc.code == "E_LOCKED":
                        lock_wait_happened = True
                        continue
                    raise
                try:
                    row_by_seq = {
                        int(row["seq"]): row for row in step_rows
                    }
                    if row_by_seq[step.seq]["stage"] != STAGE_PENDING:
                        continue
                    window = snapshot.clock.window_map[step.seq]
                    if clock.current_ms < window.start_ms:
                        clock.advance_to(window.start_ms)
                    start_ms = clock.current_ms
                    try:
                        claim = self.store.reserve_step(
                            run_id=self.run_id,
                            version=version,
                            step_seq=step.seq,
                            start_ms=start_ms,
                            deps=step.depends_on,
                        )
                    except BusinessError as exc:
                        if exc.code in {"E_STEP_CLAIMED", "E_LOCKED"}:
                            lock_wait_happened = True
                            continue
                        raise
                    if not claim.get("claimed"):
                        continue
                    if crash_at_step == step.seq and crash_phase == "after-claim":
                        os._exit(137)
                    route = snapshot.route_map[step.seq]
                    observation = self._execute_step(
                        snapshot=snapshot,
                        step=step,
                        route=route,
                        clock=clock,
                        start_ms=start_ms,
                    )
                    if crash_at_step == step.seq and crash_phase == "after-response":
                        os._exit(137)
                    self.store.commit_step(
                        run_id=self.run_id,
                        version=version,
                        observation=observation,
                    )
                    executed_steps.append(step.seq)
                    clock.current_ms = max(
                        clock.current_ms,
                        observation.ended_at_ms or clock.current_ms,
                    )
                    progressed = True
                    break
                finally:
                    lock.release()
            if not progressed:
                if lock_wait_happened:
                    raise BusinessError(
                        "E_LOCKED",
                        "all currently ready steps are locked by other drill processes",
                        {"run_id": self.run_id, "ready_steps": [s.seq for s in ready]},
                    )
                break
            if once:
                break

        state = self.store.finalize_run_state(self.run_id)
        final_run = self.store.get_run(self.run_id)
        return {
            "ok": True,
            "run_id": self.run_id,
            "version": version,
            "state": state,
            "resumed": resume,
            "recovery": recovery_result,
            "skipped_steps": skipped_now,
            "executed_steps": executed_steps,
            "logical_clock_ms": final_run["logical_clock_ms"],
            "observation_digest": final_run["observation_digest"],
            "commit_head": final_run["commit_head"],
        }

