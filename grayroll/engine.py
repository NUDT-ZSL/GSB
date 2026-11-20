from __future__ import annotations

import os
from typing import Any

import numpy as np

from .errors import GrayrollError
from .store import Store
from .util import canonical_bytes, natural_key, sha256_bytes


def _ratio_matches(left: float, right: float) -> bool:
    return bool(np.isclose(left, right, rtol=0.0, atol=1e-12))


class Engine:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _prevalidate_clock(self, until: int, events: list[dict[str, Any]]) -> None:
        if isinstance(until, bool) or not isinstance(until, int) or until < 0:
            raise GrayrollError(
                "E_BAD_EVENT", "target logical clock must be a non-negative integer"
            )
        watermark = self.store.get_clock()
        tolerance = self.store.get_tolerance()
        if until + tolerance < watermark:
            raise GrayrollError(
                "E_CLOCK_REGRESSION",
                f"logical clock regression {watermark} -> {until} exceeds tolerance {tolerance}",
                {
                    "current_watermark_ms": watermark,
                    "requested_ms": until,
                    "tolerance_ms": tolerance,
                },
            )
        previous_event: int | None = None
        for event in events:
            if self.store.event_is_applied(event):
                continue
            event_clock = int(event["logical_ms"])
            if event_clock + tolerance < watermark:
                raise GrayrollError(
                    "E_CLOCK_REGRESSION",
                    f"event {event['event_id']} clock is behind the logical watermark",
                    {
                        "current_watermark_ms": watermark,
                        "event_id": event["event_id"],
                        "requested_ms": event_clock,
                        "tolerance_ms": tolerance,
                    },
                )
            if previous_event is not None and event_clock < previous_event:
                raise GrayrollError(
                    "E_BAD_EVENT",
                    "observation event stream is not ordered by logical_ms",
                )
            previous_event = event_clock

    def advance(
        self, until: int, events: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        events = events or []
        self.store.current_package_id()
        self._prevalidate_clock(until, events)
        for event_seq, event in enumerate(events):
            if event["logical_ms"] > until:
                break
            if self.store.event_is_applied(event):
                continue
            effective_ms = max(event["logical_ms"], self.store.get_clock())
            self._run_until(effective_ms)
            self._apply_event(event, event_seq, effective_ms)
            self._run_until(effective_ms)
        self.store.set_clock(until)
        self._run_until(until)
        return self.store.status()

    def resume(
        self, until: int, events: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return self.advance(until, events)

    def _run_until(self, now: int) -> None:
        for _ in range(10000):
            changed = False
            changed = self._recover_intents(now) or changed
            changed = self._process_rollbacks(now) or changed
            changed = self._synchronize_batches(now) or changed
            if not changed:
                return
        raise GrayrollError("E_INVALID_STATE", "advance loop did not converge")

    def _recover_intents(self, now: int) -> bool:
        changed = False
        for intent in self.store.ledger_intents():
            if int(intent["scheduled_ms"]) > now:
                continue
            if intent["direction"] == "RAMP":
                self.store.commit_ramp_intent(
                    intent["operation_key"],
                    max(int(intent["scheduled_ms"]), now),
                )
                changed = True
            else:
                self.store.commit_rollback_intent(
                    intent["operation_key"], recovery_ms=now
                )
                changed = True
        return changed

    def _process_rollbacks(self, now: int) -> bool:
        changed = False
        for rollback in self.store.open_rollbacks():
            assert rollback is not None
            pending = list(rollback["pending_targets"])
            completed = list(rollback["completed_targets"])
            base_ms = max(int(rollback["abort_ms"]), int(rollback["window_start_ms"]))
            end_ms = int(rollback["window_end_ms"])
            step_ms = int(rollback["step_ms"])
            if not pending:
                continue
            if now > end_ms:
                self.store.fail_rollback(
                    rollback["rollback_id"],
                    logical_ms=end_ms,
                    first_failure="ROLLBACK_WINDOW_EXPIRED_WITH_PENDING_STEPS",
                )
                changed = True
                continue
            while pending:
                index = len(completed)
                scheduled_ms = base_ms + index * step_ms
                if scheduled_ms > end_ms:
                    self.store.fail_rollback(
                        rollback["rollback_id"],
                        logical_ms=end_ms,
                        first_failure="ROLLBACK_STEP_SCHEDULED_AFTER_WINDOW",
                    )
                    changed = True
                    break
                if scheduled_ms > now:
                    break
                target = float(pending[0])
                operation_key = (
                    f"rollback:{rollback['rollback_id']}:{index}:{target:.12f}"
                )
                _, created = self.store.ensure_intent(
                    operation_key=operation_key,
                    package_id=rollback["package_id"],
                    service=rollback["service"],
                    batch_id=rollback["batch_id"],
                    direction="ROLLBACK",
                    tier_index=index,
                    target_ratio=target,
                    scheduled_ms=scheduled_ms,
                    stage_after="ROLLING_BACK",
                    rollback_id=rollback["rollback_id"],
                    basis={
                        "completed_before": completed,
                        "reason": rollback["reason"],
                        "rollback_id": rollback["rollback_id"],
                        "schedule_base_ms": base_ms,
                        "schedule_formula": "max(abort_ms, window_start_ms) + step_index * step_ms",
                        "step_index": index,
                        "step_ms": step_ms,
                        "window_end_ms": end_ms,
                    },
                )
                if created and os.environ.get("GRAYROLL_CRASH_AT") == "rollback_intent":
                    os._exit(75)
                outcome = self.store.commit_rollback_intent(
                    operation_key, recovery_ms=now
                )
                changed = True
                if not outcome.get("applied"):
                    break
                updated = self.store.get_rollback(rollback["rollback_id"])
                if not updated or updated["status"] != "OPEN":
                    break
                completed = list(updated["completed_targets"])
                pending = list(updated["pending_targets"])
        return changed

    def _blockers(
        self,
        view: dict[str, Any],
        runtime: dict[str, Any],
        now: int,
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        same_service = [
            batch
            for batch in self.store.package_batches(runtime["package_id"])
            if batch["service"] == view["service"]
        ]
        same_service.sort(key=lambda batch: natural_key(batch["batch_id"]))
        ids = [batch["batch_id"] for batch in same_service]
        position = ids.index(view["batch_id"])
        previous_id = ids[position - 1] if position > 0 else None
        checks: list[tuple[str, str]] = []
        if previous_id is not None:
            checks.append(("PREDECESSOR_NOT_STABLE", previous_id))
        for dependency in view["depends_on"]:
            checks.append(("DEPENDENCY_NOT_STABLE", dependency))
        for code, batch_id in checks:
            key = (code, batch_id)
            if key in seen:
                continue
            seen.add(key)
            dependency_runtime = self.store.runtime_get(view["service"], batch_id)
            if not dependency_runtime or dependency_runtime["stage"] != "STABLE":
                blockers.append(
                    {
                        "batch_id": batch_id,
                        "code": code,
                        "observed_stage": (
                            dependency_runtime["stage"]
                            if dependency_runtime
                            else "MISSING"
                        ),
                    }
                )
        if now < int(view["earliest_rollout_ms"]):
            blockers.append(
                {
                    "code": "EARLIEST_NOT_REACHED",
                    "earliest_rollout_ms": int(view["earliest_rollout_ms"]),
                    "wait_ms": int(view["earliest_rollout_ms"]) - now,
                }
            )
        return blockers

    def _synchronize_batches(self, now: int) -> bool:
        changed = False
        for runtime in self.store.runtime_all():
            view = self.store.get_package_batch(
                runtime["package_id"], runtime["service"], runtime["batch_id"]
            )
            if view is None:
                continue
            if runtime["stage"] in {"PENDING", "WAITING", "BLOCKED"}:
                blockers = self._blockers(view, runtime, now)
                if blockers:
                    if runtime["stage"] != "WAITING" or runtime["blockers"] != blockers:
                        self.store.set_waiting(
                            runtime["service"],
                            runtime["batch_id"],
                            "WAITING",
                            blockers,
                            now,
                        )
                        changed = True
                    continue
                self.store.set_waiting(
                    runtime["service"],
                    runtime["batch_id"],
                    "RAMPING",
                    [],
                    now,
                )
                runtime = self.store.runtime_get(
                    runtime["service"], runtime["batch_id"]
                )
                changed = True
            if runtime and runtime["stage"] == "RAMPING":
                changed = self._promote_one_tier(view, runtime, now) or changed
        return changed

    def _promote_one_tier(
        self, view: dict[str, Any], runtime: dict[str, Any], now: int
    ) -> bool:
        tiers = list(view["tiers"])
        next_index = int(runtime["current_tier_index"]) + 1
        if next_index >= len(tiers):
            if runtime["stage"] != "STABLE":
                self.store.set_batch_stage(
                    runtime["service"], runtime["batch_id"], "STABLE", now
                )
                return True
            return False
        ratio = float(tiers[next_index])
        markers = np.asarray(view["observation_markers"], dtype=np.float64)
        is_marker = bool(
            markers.size and np.any(np.isclose(markers, ratio, atol=1e-12))
        )
        last = next_index == len(tiers) - 1
        stage_after = "OBSERVING" if is_marker else ("STABLE" if last else "RAMPING")
        operation_key = (
            f"ramp:{runtime['package_id']}:{runtime['service']}:"
            f"{runtime['batch_id']}:{next_index}:{ratio:.12f}"
        )
        _, created = self.store.ensure_intent(
            operation_key=operation_key,
            package_id=runtime["package_id"],
            service=runtime["service"],
            batch_id=runtime["batch_id"],
            direction="RAMP",
            tier_index=next_index,
            target_ratio=ratio,
            scheduled_ms=now,
            stage_after=stage_after,
            rollback_id=None,
            basis={
                "earliest_rollout_ms": view["earliest_rollout_ms"],
                "is_observation_point": is_marker,
                "source_version": view["source_version"],
                "tier_index": next_index,
            },
        )
        if created and os.environ.get("GRAYROLL_CRASH_AT") == "ramp_intent":
            os._exit(75)
        self.store.commit_ramp_intent(operation_key, now)
        return True

    def _evaluate_snapshot(
        self, event: dict[str, Any], criteria: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        rules = criteria["rules"]
        metrics = event["metrics"]
        metric_names = [rule["metric"] for rule in rules]
        values = np.asarray(
            [metrics.get(name, np.nan) for name in metric_names], dtype=np.float64
        )
        thresholds = np.asarray(
            [float(rule["value"]) for rule in rules], dtype=np.float64
        )
        operators = [rule["op"] for rule in rules]
        missing = np.isnan(values)
        rule_results: list[dict[str, Any]] = []
        for name, value, threshold, operator in zip(
            metric_names, values, thresholds, operators, strict=True
        ):
            if np.isnan(value):
                passed: bool | None = None
            elif operator == "<":
                passed = bool(value < threshold)
            elif operator == "<=":
                passed = bool(value <= threshold)
            elif operator == ">":
                passed = bool(value > threshold)
            elif operator == ">=":
                passed = bool(value >= threshold)
            else:
                passed = bool(value == threshold)
            rule_results.append(
                {
                    "metric": name,
                    "observed": None if np.isnan(value) else float(value),
                    "op": operator,
                    "passed": passed,
                    "threshold": float(threshold),
                }
            )
        basis = {
            "criteria": criteria,
            "event_id": event["event_id"],
            "rules": rule_results,
            "sample_count": event["sample_count"],
        }
        if event["sample_count"] < int(criteria["min_samples"]):
            basis["reason"] = "INSUFFICIENT_SAMPLES"
            return "CONTINUE", basis
        if bool(np.any(missing)):
            verdict = "FAIL" if criteria["unknown_metric"] == "FAIL" else "CONTINUE"
            basis["reason"] = "MISSING_METRIC"
            return verdict, basis
        if bool(np.all(np.asarray([item["passed"] for item in rule_results]))):
            basis["reason"] = "ALL_RULES_PASSED"
            return "PASS", basis
        basis["reason"] = "RULE_VIOLATION"
        return "FAIL", basis

    def _rollback_id(
        self,
        *,
        package_id: str,
        service: str,
        batch_id: str,
        abort_ms: int,
        event_id: str,
    ) -> str:
        digest = sha256_bytes(
            canonical_bytes(
                {
                    "abort_ms": abort_ms,
                    "batch_id": batch_id,
                    "event_id": event_id,
                    "package_id": package_id,
                    "service": service,
                }
            )
        )
        return f"rb-{digest[:24]}"

    def _rollback_spec(
        self,
        *,
        runtime: dict[str, Any],
        view: dict[str, Any],
        abort_ms: int,
        reason: str,
        event_id: str,
    ) -> dict[str, Any]:
        targets = [
            float(value)
            for value in sorted(runtime["completed_tiers"], reverse=True)
            if float(value) < float(runtime["current_ratio"]) - 1e-12
        ]
        if not targets or not _ratio_matches(targets[-1], 0.0):
            targets.append(0.0)
        return {
            "rollback_id": self._rollback_id(
                package_id=runtime["package_id"],
                service=runtime["service"],
                batch_id=runtime["batch_id"],
                abort_ms=abort_ms,
                event_id=event_id,
            ),
            "package_id": runtime["package_id"],
            "service": runtime["service"],
            "batch_id": runtime["batch_id"],
            "reason": reason,
            "abort_ms": abort_ms,
            "start_ms": int(view["rollback_start_ms"]),
            "end_ms": int(view["rollback_end_ms"]),
            "step_ms": int(view["rollback_step_ms"]),
            "targets": targets,
        }

    def _apply_event(
        self, event: dict[str, Any], event_seq: int, effective_ms: int
    ) -> None:
        runtime = self.store.runtime_get(event["service"], event["batch_id"])
        if not runtime:
            raise GrayrollError(
                "E_EVENT_BATCH_STATE",
                f"event {event['event_id']} references unknown batch runtime",
            )
        view = self.store.get_package_batch(
            runtime["package_id"], event["service"], event["batch_id"]
        )
        if view is None:
            raise GrayrollError(
                "E_INVALID_STATE", "batch runtime has no bound package view"
            )
        if event["type"] == "manual_abort":
            if runtime["stage"] in {
                "PENDING",
                "WAITING",
                "BLOCKED",
                "ROLLING_BACK",
                "ROLLED_BACK",
                "ROLLBACK_FAILED",
            }:
                raise GrayrollError(
                    "E_EVENT_BATCH_STATE",
                    f"manual abort is not valid while batch is {runtime['stage']}",
                )
            spec = self._rollback_spec(
                runtime=runtime,
                view=view,
                abort_ms=effective_ms,
                reason=f"manual_abort:{event['reason']}",
                event_id=event["event_id"],
            )
            self.store.apply_manual_abort(
                event=event,
                event_seq=event_seq,
                effect_ms=effective_ms,
                rollback_spec=spec,
            )
            return

        if runtime["stage"] not in {"RAMPING", "OBSERVING"}:
            raise GrayrollError(
                "E_EVENT_BATCH_STATE",
                f"snapshot is not valid while batch is {runtime['stage']}",
            )
        if not _ratio_matches(runtime["current_ratio"], event["tier_ratio"]):
            raise GrayrollError(
                "E_EVENT_BATCH_STATE",
                "snapshot tier_ratio does not match the active batch ratio",
                {
                    "active_ratio": runtime["current_ratio"],
                    "event_id": event["event_id"],
                    "event_ratio": event["tier_ratio"],
                },
            )
        current_index = int(runtime["current_tier_index"])
        current_ratio = float(view["tiers"][current_index])
        if not _ratio_matches(current_ratio, event["tier_ratio"]):
            raise GrayrollError(
                "E_EVENT_BATCH_STATE",
                "snapshot does not target the current tier",
            )
        verdict, basis = self._evaluate_snapshot(event, view["criteria"])
        if verdict == "CONTINUE":
            stage_after = "OBSERVING"
        elif verdict == "PASS":
            stage_after = (
                "STABLE" if current_index == len(view["tiers"]) - 1 else "RAMPING"
            )
        else:
            stage_after = "ROLLING_BACK"
        rollback_spec = None
        if verdict == "FAIL":
            rollback_spec = self._rollback_spec(
                runtime=runtime,
                view=view,
                abort_ms=effective_ms,
                reason=f"observation_failed:{event['event_id']}",
                event_id=event["event_id"],
            )
        self.store.apply_snapshot(
            event=event,
            event_seq=event_seq,
            effect_ms=effective_ms,
            verdict=verdict,
            basis=basis,
            stage_after=stage_after,
            rollback_spec=rollback_spec,
        )
