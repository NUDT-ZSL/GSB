from __future__ import annotations

import copy
import json
import os
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .dataset import query_bundle
from .errors import BusinessError, conflict, fail
from .locking import lock_path
from .probe_plan import ProbePlan, build_probe_plan
from .schema import Event, Instance, Service, parse_sample
from .storage import RoundRecord, Snapshot, Storage
from .util import (
    canonical_json_text,
    content_digest,
    first_difference,
    is_int,
    normalize_expected_digest,
    sha256_text,
)


PROBE_COMPLETED = "completed"
PROBE_FAILED = "failed_after_start"
PROBE_ABANDONED = "abandoned_budget"
PROBE_NOT_STARTED = "not_started_within_budget"


@dataclass(slots=True)
class _ProbeRun:
    domain: str
    kind: str
    status: str
    cost_ms: int
    start_offset_ms: int | None
    end_offset_ms: int | None
    reason: str | None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_ms": self.cost_ms,
            "domain": self.domain,
            "end_offset_ms": self.end_offset_ms,
            "error": self.error,
            "kind": self.kind,
            "reason": self.reason,
            "result": self.result,
            "start_offset_ms": self.start_offset_ms,
            "status": self.status,
        }


def _request_error(message: str, field_path: str) -> None:
    fail("INVALID_REQUEST", message, field_path=field_path)


def _optional_nonnegative_int(
    payload: dict[str, Any],
    name: str,
) -> int | None:
    if name not in payload or payload[name] is None:
        return None
    value = payload[name]
    if not is_int(value) or value < 0:
        _request_error(f"{name} must be a non-negative integer.", name)
    return value


def _normalize_switch_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        _request_error("Request body must be a JSON object.", "$")
    round_id = payload.get("round_id")
    if not is_int(round_id) or round_id <= 0:
        _request_error("round_id must be a positive integer.", "round_id")
    logical_time_ms = payload.get("logical_time_ms")
    if not is_int(logical_time_ms) or logical_time_ms < 0:
        _request_error(
            "logical_time_ms must be a non-negative integer.",
            "logical_time_ms",
        )
    event_seq = _optional_nonnegative_int(payload, "event_seq")
    max_concurrency = _optional_nonnegative_int(payload, "max_concurrency")
    if max_concurrency is not None and max_concurrency == 0:
        _request_error("max_concurrency must be positive.", "max_concurrency")
    time_budget_ms = _optional_nonnegative_int(payload, "time_budget_ms")
    expected_policy_version = _optional_nonnegative_int(
        payload,
        "expected_policy_version",
    )
    expected_pointer_version = _optional_nonnegative_int(
        payload,
        "expected_pointer_version",
    )
    normalized = {
        "event_seq": event_seq,
        "expected_policy_version": expected_policy_version,
        "expected_pointer_version": expected_pointer_version,
        "logical_time_ms": logical_time_ms,
        "max_concurrency": max_concurrency,
        "round_id": round_id,
        "time_budget_ms": time_budget_ms,
    }
    normalized["input_digest"] = content_digest(normalized)
    return normalized


class Workbench:
    def __init__(self, state_dir: str | Path = "state") -> None:
        self.state_dir = Path(state_dir)
        self.storage = Storage(self.state_dir)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            _request_error("Request body must be a JSON object.", "$")
        source = payload.get("sample")
        expected = payload.get(
            "expected_sample_digest",
            payload.get("expected_digest"),
        )
        if expected is not None and not isinstance(expected, str):
            _request_error(
                "expected_sample_digest must be a string.",
                "expected_sample_digest",
            )
        expected_digest = normalize_expected_digest(expected)

        sample_path_key = (
            "sample_path" if "sample_path" in payload else "path"
        )
        if source is None and sample_path_key in payload:
            sample_path_value = payload[sample_path_key]
            if isinstance(sample_path_value, os.PathLike):
                sample_path_value = os.fspath(sample_path_value)
            if not isinstance(sample_path_value, str) or not sample_path_value:
                _request_error("sample_path must be a non-empty string.", "sample_path")
            path = Path(sample_path_value)
            manifest_expected: str | None = None
            if path.is_dir():
                manifest_path = path / "manifest.json"
                if manifest_path.exists():
                    try:
                        manifest = json.loads(
                            manifest_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError) as exc:
                        fail(
                            "INVALID_SAMPLE",
                            f"Unable to parse sample manifest: {exc}",
                            field_path="sample_path",
                            context={"manifest": str(manifest_path)},
                        )
                    manifest_expected = normalize_expected_digest(
                        manifest.get("sample_digest")
                    )
                path = path / "sample.json"
            if not path.exists():
                fail(
                    "SAMPLE_NOT_FOUND",
                    "sample_path does not exist.",
                    field_path="sample_path",
                    context={"path": str(path)},
                    http_status=404,
                )
            try:
                source = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                fail(
                    "INVALID_SAMPLE",
                    f"Unable to parse sample file: {exc}",
                    field_path="sample_path",
                    context={"path": str(path)},
                )
            if expected_digest is None:
                expected_digest = manifest_expected
            elif (
                manifest_expected is not None
                and expected_digest != manifest_expected
            ):
                fail(
                    "SAMPLE_DIGEST_MISMATCH",
                    "Explicit digest conflicts with the manifest digest.",
                    field_path="expected_sample_digest",
                    context={
                        "explicit": expected_digest,
                        "manifest": manifest_expected,
                    },
                    http_status=409,
                )

        if source is None and "topology" in payload and "policy" in payload:
            source = {
                "events": payload.get("events", []),
                "policy": payload["policy"],
                "sample_id": payload.get("sample_id", "inline-sample"),
                "schema_version": payload.get("schema_version", 1),
                "topology": payload["topology"],
            }
        if source is None:
            _request_error(
                "Provide sample, sample_path, or topology/policy/events.",
                "sample",
            )
        sample = parse_sample(source)
        return self.storage.load_sample(
            sample,
            expected_sample_digest=expected_digest,
        )

    # ------------------------------------------------------------------
    # Read and query isolation
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return self.storage.status()

    def _require_current(self) -> Snapshot:
        snapshot = self.storage.current_snapshot()
        if snapshot is None:
            fail("NOT_LOADED", "No sample has been loaded.", http_status=404)
        return snapshot

    def query(
        self,
        service_name: str,
        *,
        pointer_version: int | None = None,
    ) -> dict[str, Any]:
        if pointer_version is None:
            snapshot = self._require_current()
        else:
            snapshot = self.storage.snapshot_by_pointer(pointer_version)
            if snapshot is None:
                fail(
                    "SNAPSHOT_NOT_FOUND",
                    "Requested pointer version does not exist.",
                    context={"pointer_version": pointer_version},
                    http_status=404,
                )
        service = snapshot.sample.services.get(service_name)
        if service is None:
            fail(
                "SERVICE_NOT_FOUND",
                "Service does not exist in this sample.",
                context={"service": service_name},
                http_status=404,
            )
        service_state = snapshot.state["services"][service_name]
        bundle = query_bundle(
            service,
            service_state["primary"],
            policy_version=snapshot.policy_version,
            pointer_version=snapshot.pointer_version,
        )
        frozen = service.domain in snapshot.state["frozen_domains"]
        return {
            "domain": service.domain,
            "frozen": frozen,
            "policy_version": snapshot.policy_version,
            "pointer_version": snapshot.pointer_version,
            "primary": service_state["primary"],
            "query": bundle,
            "snapshot_digest": snapshot.digest,
        }

    # ------------------------------------------------------------------
    # Round planning and execution
    # ------------------------------------------------------------------
    def switch(self, payload: Any) -> dict[str, Any]:
        request_input = _normalize_switch_request(payload)
        round_id = request_input["round_id"]
        existing_record = self.storage.get_round(round_id)
        if existing_record is not None:
            self._check_round_idempotency(existing_record, request_input)
            return self._execute_round(existing_record)

        last_conflict: BusinessError | None = None
        for _ in range(4):
            snapshot = self._require_current()
            try:
                record = self._plan_and_prepare_round(
                    request_input,
                    snapshot,
                )
            except BusinessError as exc:
                if exc.code == "STATE_CONFLICT":
                    last_conflict = exc
                    continue
                raise
            return self._execute_round(record)
        if last_conflict is not None:
            raise last_conflict
        conflict(
            "STATE_CONFLICT",
            "Unable to establish a stable base snapshot for the round.",
            context={"round_id": round_id},
        )
        return {}

    def _check_round_idempotency(
        self,
        record: RoundRecord,
        request_input: dict[str, Any],
    ) -> None:
        stored_digest = record.request.get("input_digest")
        if stored_digest != request_input["input_digest"]:
            conflict(
                "IDEMPOTENCY_CONFLICT",
                "The same round_id was submitted with different input.",
                context={
                    "existing_input_digest": stored_digest,
                    "incoming_input_digest": request_input["input_digest"],
                    "round_id": record.round_id,
                },
            )

    def _plan_and_prepare_round(
        self,
        request_input: dict[str, Any],
        snapshot: Snapshot,
    ) -> RoundRecord:
        sample = snapshot.sample
        policy = sample.policy
        state = snapshot.state
        clock_ms = int(request_input["logical_time_ms"])
        round_id = int(request_input["round_id"])
        base_clock = int(state["clock_ms"])
        tolerance = policy.clock_regression_tolerance_ms
        if clock_ms < base_clock - tolerance:
            fail(
                "CLOCK_REGRESSION",
                "Injected logical clock moved backwards beyond tolerance.",
                field_path="logical_time_ms",
                context={
                    "base_clock_ms": base_clock,
                    "clock_ms": clock_ms,
                    "round_id": round_id,
                    "tolerance_ms": tolerance,
                },
                http_status=409,
            )

        last_event_seq = int(state["last_event_seq"])
        explicit_event_seq = request_input["event_seq"]
        max_event_seq = sample.events[-1].seq if sample.events else 0
        if explicit_event_seq is not None:
            if explicit_event_seq < last_event_seq:
                conflict(
                    "EVENT_SEQUENCE_REGRESSION",
                    "event_seq cannot move behind the committed event watermark.",
                    context={
                        "committed_event_seq": last_event_seq,
                        "event_seq": explicit_event_seq,
                        "round_id": round_id,
                    },
                )
            if explicit_event_seq > max_event_seq:
                fail(
                    "INVALID_EVENT_SEQUENCE",
                    "event_seq is beyond the end of the sample event stream.",
                    field_path="event_seq",
                    context={
                        "event_seq": explicit_event_seq,
                        "max_event_seq": max_event_seq,
                        "round_id": round_id,
                    },
                )
            effective_event_seq = explicit_event_seq
        else:
            eligible = [
                event.seq
                for event in sample.events
                if event.seq > last_event_seq
                and event.logical_time_ms <= clock_ms
            ]
            effective_event_seq = max(eligible, default=last_event_seq)

        selected_events = [
            event
            for event in sample.events
            if last_event_seq < event.seq <= effective_event_seq
        ]
        for event in selected_events:
            if event.logical_time_ms > clock_ms:
                fail(
                    "CLOCK_REGRESSION",
                    "Injected logical clock is behind a selected event.",
                    field_path="logical_time_ms",
                    context={
                        "clock_ms": clock_ms,
                        "event_logical_time_ms": event.logical_time_ms,
                        "event_seq": event.seq,
                        "round_id": round_id,
                    },
                )

        triggered = self._triggered_services(snapshot, selected_events)
        target_domain_set = {
            sample.services[service_name].domain for service_name in triggered
        }
        target_domain_set.update(
            service.domain
            for service_name, service in sample.services.items()
            if state["services"][service_name]["switched_away"]
        )
        target_domains = tuple(
            domain for domain in policy.freeze_order if domain in target_domain_set
        )

        max_concurrency = (
            request_input["max_concurrency"]
            if request_input["max_concurrency"] is not None
            else policy.max_concurrency
        )
        time_budget_ms = (
            request_input["time_budget_ms"]
            if request_input["time_budget_ms"] is not None
            else policy.time_budget_ms
        )
        plan = build_probe_plan(
            target_domains=target_domains,
            freeze_order=policy.freeze_order,
            freeze_batch_size=policy.freeze_batch_size,
            freeze_cost_ms=policy.freeze_probe_cost_ms,
            switch_cost_ms=policy.switch_probe_cost_ms,
            max_concurrency=max_concurrency,
            budget_ms=time_budget_ms,
        )
        request = {
            **request_input,
            "base_pointer_version": snapshot.pointer_version,
            "base_policy_version": snapshot.policy_version,
            "clock_ms": max(clock_ms, base_clock),
            "effective_event_seq": effective_event_seq,
            "policy_version": snapshot.policy_version,
            "sample_digest": sample.sample_digest,
            "selected_event_seqs": [event.seq for event in selected_events],
            "target_domains": list(target_domains),
        }
        request_digest = content_digest(
            {
                "input_digest": request_input["input_digest"],
                "plan": plan.as_dict(),
                "selected_event_seqs": request["selected_event_seqs"],
                "target_domains": request["target_domains"],
            }
        )
        return self.storage.prepare_round(
            round_id=round_id,
            request=request,
            request_digest=request_digest,
            plan=plan,
            sample=sample,
            base_pointer_version=snapshot.pointer_version,
            target_domains=target_domains,
            expected_policy_version=request_input["expected_policy_version"],
            expected_pointer_version=request_input["expected_pointer_version"],
        )

    def _triggered_services(
        self,
        snapshot: Snapshot,
        events: list[Event],
    ) -> dict[str, dict[str, Any]]:
        triggered: dict[str, dict[str, Any]] = {}
        for service_name, service in snapshot.sample.services.items():
            policy = snapshot.sample.policy.services[service_name]
            primary = snapshot.state["services"][service_name]["primary"]
            for event in events:
                if event.instance_id != primary:
                    continue
                if event.metric != policy.trigger_metric:
                    continue
                if event.value >= policy.trigger_threshold:
                    triggered[service_name] = {
                        "event_seq": event.seq,
                        "logical_time_ms": event.logical_time_ms,
                        "metric": event.metric,
                        "value": event.value,
                    }
                    break
        return triggered

    def _execute_round(self, record: RoundRecord) -> dict[str, Any]:
        if record.status == "committed":
            return record.result or {}
        base = self.storage.snapshot_by_pointer(record.base_pointer_version)
        if base is None:
            fail(
                "SNAPSHOT_NOT_FOUND",
                "Round base snapshot is missing.",
                context={
                    "base_pointer_version": record.base_pointer_version,
                    "round_id": record.round_id,
                },
                http_status=409,
            )
        if base.sample.sample_digest != record.sample_digest:
            conflict(
                "SAMPLE_DIGEST_MISMATCH",
                "Round base sample does not match the prepared sample.",
                context={
                    "prepared_sample_digest": record.sample_digest,
                    "stored_sample_digest": base.sample.sample_digest,
                    "round_id": record.round_id,
                },
            )

        crash_after = [
            int(os.environ.get("WORKBENCH_CRASH_AFTER_DOMAIN_COMMITS", "0"))
        ]
        for batch_index, planned_batch in enumerate(record.plan.batches):
            batch_domains = tuple(
                dict.fromkeys(probe.domain for probe in planned_batch)
            )
            existing = self.storage.get_round_domains(record.round_id)
            pending = tuple(
                domain for domain in batch_domains if domain not in existing
            )
            if not pending:
                continue
            # Different worker processes start at different points in a batch,
            # allowing them to process different domains concurrently.
            rotation = os.getpid() % len(pending)
            pending = pending[rotation:] + pending[:rotation]
            self._process_batch(
                record=record,
                base=base,
                batch_index=batch_index,
                domains=pending,
                crash_after=crash_after,
            )

        return self._finalize(record, base)

    def _process_batch(
        self,
        *,
        record: RoundRecord,
        base: Snapshot,
        batch_index: int,
        domains: tuple[str, ...],
        crash_after: list[int],
    ) -> list[str]:
        sample = base.sample
        selected_events = [
            event
            for event in sample.events
            if event.seq in set(record.request["selected_event_seqs"])
        ]
        triggered = self._triggered_services(base, selected_events)
        planned_by_domain = {
            domain: record.plan.probes_for_domain(domain) for domain in domains
        }

        executable: dict[tuple[str, str], Callable[[], dict[str, Any]]] = {}
        domain_has_budget_abandon = {
            domain: any(
                probe.status == PROBE_ABANDONED
                for probe in planned_by_domain[domain]
            )
            for domain in domains
        }
        for domain in domains:
            if domain_has_budget_abandon[domain]:
                continue
            executable[(domain, "freeze")] = (
                lambda d=domain: self._freeze_probe(
                    base,
                    record,
                    d,
                    triggered,
                )
            )
            executable[(domain, "switch")] = (
                lambda d=domain: self._switch_probe(
                    base,
                    record,
                    d,
                    triggered,
                    selected_events,
                )
            )

        results: dict[tuple[str, str], Future[dict[str, Any]]] = {}
        if executable:
            worker_count = min(record.plan.max_concurrency, len(executable))
            with ThreadPoolExecutor(
                max_workers=max(worker_count, 1),
                thread_name_prefix=f"round-{record.round_id}",
            ) as executor:
                for key, function in executable.items():
                    results[key] = executor.submit(function)
                # Exiting the context waits for every started probe to finish.
                for future in as_completed(tuple(results.values())):
                    try:
                        future.result()
                    except BaseException:
                        pass

        saved_domains: list[str] = []
        for domain in domains:
            existing = self.storage.get_round_domains(record.round_id).get(domain)
            if existing is not None:
                continue
            probes = self._collect_probe_outcomes(
                domain=domain,
                planned=planned_by_domain[domain],
                futures=results,
                domain_skipped=domain_has_budget_abandon[domain],
            )
            outcome = self._combine_domain_outcome(
                base=base,
                record=record,
                domain=domain,
                probes=probes,
            )
            status = outcome["status"]
            with lock_path(
                self.storage.lock_dir
                / f"round-{record.round_id}-domain-{sha256_text(domain)[:16]}.lock"
            ):
                self.storage.save_domain_result(
                    record.round_id,
                    domain,
                    status,
                    outcome,
                )
            saved_domains.append(domain)
            if crash_after[0] > 0:
                crash_after[0] -= 1
                if crash_after[0] == 0:
                    os._exit(77)
        return saved_domains

    def _collect_probe_outcomes(
        self,
        *,
        domain: str,
        planned: tuple[Any, ...],
        futures: dict[tuple[str, str], Future[dict[str, Any]]],
        domain_skipped: bool,
    ) -> list[dict[str, Any]]:
        outcomes: list[_ProbeRun] = []
        for item in planned:
            run = _ProbeRun(
                domain=domain,
                kind=item.kind,
                status=item.status,
                cost_ms=item.cost_ms,
                start_offset_ms=item.start_offset_ms,
                end_offset_ms=item.end_offset_ms,
                reason=item.reason,
            )
            if item.status == PROBE_ABANDONED:
                outcomes.append(run)
                continue
            if domain_skipped:
                run.status = PROBE_NOT_STARTED
                run.reason = "peer_probe_abandoned_by_budget"
                run.end_offset_ms = None
                outcomes.append(run)
                continue
            future = futures.get((domain, item.kind))
            if future is None:
                run.status = PROBE_NOT_STARTED
                run.reason = "probe_not_scheduled"
                run.end_offset_ms = None
                outcomes.append(run)
                continue
            try:
                run.result = future.result()
                run.status = PROBE_COMPLETED
            except BusinessError as exc:
                run.status = PROBE_FAILED
                run.error = exc.to_dict()["error"]
            except BaseException as exc:
                run.status = PROBE_FAILED
                run.error = {
                    "code": "PROBE_INTERNAL_ERROR",
                    "context": {"domain": domain},
                    "field_path": None,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            outcomes.append(run)
        return [outcome.as_dict() for outcome in outcomes]

    def _freeze_probe(
        self,
        base: Snapshot,
        record: RoundRecord,
        domain: str,
        triggered: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        sample = base.sample
        triggered_services = sorted(
            service_name
            for service_name, service in sample.services.items()
            if service.domain == domain and service_name in triggered
        )
        frozen = domain in base.state["frozen_domains"]
        return {
            "already_frozen": frozen,
            "domain": domain,
            "should_freeze": bool(triggered_services) and not frozen,
            "triggered_services": triggered_services,
        }

    def _switch_probe(
        self,
        base: Snapshot,
        record: RoundRecord,
        domain: str,
        triggered: dict[str, dict[str, Any]],
        selected_events: list[Event],
    ) -> dict[str, Any]:
        sample = base.sample
        policy = sample.policy
        clock_ms = int(record.request["clock_ms"])
        round_id = record.round_id
        service_states: dict[str, Any] = {}
        switches: list[dict[str, Any]] = []
        failback_steps: list[dict[str, Any]] = []
        cooldown_waits: list[dict[str, Any]] = []

        for service_name, service in sample.services.items():
            if service.domain != domain:
                continue
            old_state = copy.deepcopy(base.state["services"][service_name])
            new_state = copy.deepcopy(old_state)
            service_policy = policy.services[service_name]
            is_triggered = service_name in triggered
            last_switch = new_state["last_switch_clock_ms"]
            cooldown_elapsed = (
                last_switch is None
                or clock_ms - int(last_switch) >= service_policy.cooldown_ms
            )

            if is_triggered:
                if not cooldown_elapsed:
                    cooldown_waits.append(
                        {
                            "reason": "cooldown_wait",
                            "remaining_ms": (
                                service_policy.cooldown_ms
                                - (clock_ms - int(last_switch))
                            ),
                            "service": service_name,
                        }
                    )
                else:
                    target = self._select_target(service, new_state["primary"])
                    if target is None:
                        raise BusinessError(
                            code="SWITCH_TARGET_MISSING",
                            message=(
                                "No eligible standby instance exists for the "
                                "triggered service."
                            ),
                            field_path=(
                                f"topology.services.{service_name}.instances"
                            ),
                            context={
                                "domain": domain,
                                "round_id": round_id,
                                "service": service_name,
                                "current_primary": new_state["primary"],
                            },
                            http_status=409,
                        )
                    old_primary = new_state["primary"]
                    new_state["primary"] = target.instance_id
                    new_state["switched_away"] = True
                    new_state["failback_step"] = -1
                    new_state["last_switch_clock_ms"] = clock_ms
                    new_state["last_transition_round"] = round_id
                    new_state["primary_revision"] += 1
                    switches.append(
                        {
                            "domain": domain,
                            "from_instance": old_primary,
                            "logical_time_ms": clock_ms,
                            "reason": "trigger_threshold",
                            "service": service_name,
                            "to_instance": target.instance_id,
                        }
                    )
            elif new_state["switched_away"]:
                if not cooldown_elapsed:
                    cooldown_waits.append(
                        {
                            "reason": "cooldown_wait",
                            "remaining_ms": (
                                service_policy.cooldown_ms
                                - (clock_ms - int(last_switch))
                            ),
                            "service": service_name,
                        }
                    )
                else:
                    next_step = int(new_state["failback_step"]) + 1
                    ladder = service_policy.failback_ladder
                    if next_step >= len(ladder):
                        next_step = len(ladder) - 1
                    ratio = float(ladder[next_step])
                    new_state["failback_step"] = next_step
                    new_state["last_switch_clock_ms"] = clock_ms
                    new_state["last_transition_round"] = round_id
                    failback_steps.append(
                        {
                            "domain": domain,
                            "logical_time_ms": clock_ms,
                            "ratio": ratio,
                            "service": service_name,
                            "step": next_step,
                        }
                    )
                    if ratio == 0:
                        original = new_state["initial_primary"]
                        old_primary = new_state["primary"]
                        if old_primary != original:
                            switches.append(
                                {
                                    "domain": domain,
                                    "from_instance": old_primary,
                                    "logical_time_ms": clock_ms,
                                    "reason": "failback_complete",
                                    "service": service_name,
                                    "to_instance": original,
                                }
                            )
                            new_state["primary"] = original
                            new_state["primary_revision"] += 1
                        new_state["switched_away"] = False
                        new_state["failback_step"] = -1
            service_states[service_name] = new_state

        consistency = self._query_consistency(
            base=base,
            domain=domain,
            service_states=service_states,
        )
        divergent = [item for item in consistency if not item["equal"]]
        if divergent:
            first = divergent[0]["first_divergence"]
            raise BusinessError(
                code="QUERY_CONSISTENCY_VIOLATION",
                message=(
                    "Primary and standby aggregate query results diverged "
                    "during switch planning."
                ),
                field_path=first["field_path"],
                context={
                    "consistency": consistency,
                    "domain": domain,
                    "first_divergence": first,
                    "round_id": round_id,
                    "service": divergent[0]["service"],
                },
                http_status=409,
            )

        triggered_in_domain = any(
            service.domain == domain and name in triggered
            for name, service in sample.services.items()
        )
        all_returned = all(
            not service_states[name]["switched_away"]
            for name, service in sample.services.items()
            if service.domain == domain
        )
        unfreeze = (
            domain in base.state["frozen_domains"]
            and not triggered_in_domain
            and all_returned
        )
        return {
            "cooldown_waits": cooldown_waits,
            "consistency": consistency,
            "failback_steps": failback_steps,
            "service_states": service_states,
            "switches": switches,
            "unfreeze": unfreeze,
        }

    def _select_target(self, service: Service, current: str) -> Instance | None:
        for instance in service.ordered_instances():
            if instance.instance_id != current:
                return instance
        return None

    def _query_consistency(
        self,
        *,
        base: Snapshot,
        domain: str,
        service_states: dict[str, Any],
    ) -> list[dict[str, Any]]:
        consistency: list[dict[str, Any]] = []
        for service_name, service in base.sample.services.items():
            if service.domain != domain:
                continue
            before_primary = base.state["services"][service_name]["primary"]
            after_primary = service_states[service_name]["primary"]
            before = query_bundle(
                service,
                before_primary,
                policy_version=base.policy_version,
                pointer_version=base.pointer_version,
            )
            after = query_bundle(
                service,
                after_primary,
                policy_version=base.policy_version,
                pointer_version=base.pointer_version + 1,
            )
            raw_difference = first_difference(
                before["queries"],
                after["queries"],
                "queries",
            )
            first_divergence = None
            if raw_difference is not None:
                first_divergence = {
                    "field_path": raw_difference["field_path"],
                    "primary_instance": before_primary,
                    "primary_pointer_version": before["pointer_version"],
                    "primary_value": raw_difference["left_value"],
                    "right_present": raw_difference["right_present"],
                    "left_present": raw_difference["left_present"],
                    "standby_instance": after_primary,
                    "standby_pointer_version": after["pointer_version"],
                    "standby_value": raw_difference["right_value"],
                }
            consistency.append(
                {
                    "after_digest": after["query_digest"],
                    "after_instance": after_primary,
                    "after_pointer_version": after["pointer_version"],
                    "before_digest": before["query_digest"],
                    "before_instance": before_primary,
                    "before_pointer_version": before["pointer_version"],
                    "domain": domain,
                    "equal": raw_difference is None,
                    "first_divergence": first_divergence,
                    "service": service_name,
                }
            )
        return consistency

    def _combine_domain_outcome(
        self,
        *,
        base: Snapshot,
        record: RoundRecord,
        domain: str,
        probes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        failed = [probe for probe in probes if probe["status"] == PROBE_FAILED]
        abandoned = [
            probe for probe in probes if probe["status"] == PROBE_ABANDONED
        ]
        not_started = [
            probe for probe in probes if probe["status"] == PROBE_NOT_STARTED
        ]
        base_outcome = {
            "batch_index": record.plan.batch_index(domain),
            "domain": domain,
            "probes": probes,
        }
        if failed:
            return {
                **base_outcome,
                "status": "failed",
                "error": failed[0]["error"],
            }
        if abandoned or not_started:
            reasons = sorted(
                {
                    probe["reason"]
                    for probe in abandoned + not_started
                    if probe["reason"]
                }
            )
            return {
                **base_outcome,
                "status": "abandoned",
                "error": {
                    "code": "PROBE_BUDGET_ABANDONED",
                    "context": {
                        "domain": domain,
                        "reasons": reasons,
                        "round_id": record.round_id,
                    },
                    "field_path": None,
                    "message": (
                        "At least one required probe was abandoned or not "
                        "started because of the logical time budget."
                    ),
                },
            }

        freeze_result = next(
            (
                probe["result"]
                for probe in probes
                if probe["kind"] == "freeze" and probe["result"] is not None
            ),
            None,
        )
        switch_result = next(
            (
                probe["result"]
                for probe in probes
                if probe["kind"] == "switch" and probe["result"] is not None
            ),
            None,
        )
        if freeze_result is None or switch_result is None:
            return {
                **base_outcome,
                "status": "failed",
                "error": {
                    "code": "PROBE_RESULT_MISSING",
                    "context": {"domain": domain, "round_id": record.round_id},
                    "field_path": None,
                    "message": "A required probe did not produce a result.",
                },
            }
        return {
            **base_outcome,
            "consistency": switch_result["consistency"],
            "cooldown_waits": switch_result["cooldown_waits"],
            "failback_steps": switch_result["failback_steps"],
            "freeze": {
                "already_frozen": freeze_result["already_frozen"],
                "frozen": bool(freeze_result["should_freeze"]),
                "triggered_services": freeze_result["triggered_services"],
                "unfrozen": bool(switch_result["unfreeze"]),
            },
            "service_states": switch_result["service_states"],
            "status": "completed",
            "switches": switch_result["switches"],
        }

    # ------------------------------------------------------------------
    # Commit and recovery
    # ------------------------------------------------------------------
    def _finalize(self, record: RoundRecord, base: Snapshot) -> dict[str, Any]:
        domains = self.storage.get_round_domains(record.round_id)
        missing = [
            domain for domain in record.target_domains if domain not in domains
        ]
        if missing:
            conflict(
                "ROUND_INCOMPLETE",
                "Round still has domains without a terminal outcome.",
                context={
                    "missing_domains": sorted(missing),
                    "round_id": record.round_id,
                },
            )

        final_state = copy.deepcopy(base.state)
        pointer_version = base.pointer_version + 1
        final_state["pointer_version"] = pointer_version
        final_state["clock_ms"] = max(
            int(base.state["clock_ms"]),
            int(record.request["clock_ms"]),
        )
        final_state["last_event_seq"] = int(record.request["effective_event_seq"])
        final_state["round_watermark"] = max(
            int(base.state["round_watermark"]),
            record.round_id,
        )
        final_state["created_round"] = record.round_id
        frozen_domains = set(base.state["frozen_domains"])
        switches_by_service: list[dict[str, Any]] = []
        failback_steps: list[dict[str, Any]] = []
        cooldown_waits: list[dict[str, Any]] = []
        consistency: list[dict[str, Any]] = []
        domain_summaries: list[dict[str, Any]] = []

        for domain in base.sample.policy.freeze_order:
            item = domains.get(domain)
            if item is None:
                continue
            outcome = item["outcome"]
            summary = {
                "domain": domain,
                "error": outcome.get("error"),
                "probes": outcome.get("probes", []),
                "status": item["status"],
            }
            if item["status"] == "completed":
                for service_name, state in outcome["service_states"].items():
                    final_state["services"][service_name] = state
                freeze = outcome["freeze"]
                if freeze["frozen"]:
                    frozen_domains.add(domain)
                if freeze["unfrozen"]:
                    frozen_domains.discard(domain)
                switches_by_service.extend(outcome.get("switches", []))
                failback_steps.extend(outcome.get("failback_steps", []))
                cooldown_waits.extend(outcome.get("cooldown_waits", []))
                consistency.extend(outcome.get("consistency", []))
                summary.update(
                    {
                        "consistency": outcome.get("consistency", []),
                        "freeze": freeze,
                        "switches": outcome.get("switches", []),
                    }
                )
            domain_summaries.append(summary)

        final_state["frozen_domains"] = sorted(frozen_domains)
        query_summaries: dict[str, dict[str, Any]] = {}
        for service_name, service in base.sample.services.items():
            bundle = query_bundle(
                service,
                final_state["services"][service_name]["primary"],
                policy_version=base.policy_version,
                pointer_version=pointer_version,
            )
            query_summaries[service_name] = bundle
        final_state["query_summaries"] = query_summaries
        switches_by_service.sort(
            key=lambda item: (item["service"], item["logical_time_ms"])
        )
        consistency.sort(key=lambda item: item["service"])
        snapshot_digest = content_digest(final_state)
        result = {
            "base_pointer_version": base.pointer_version,
            "clock_ms": final_state["clock_ms"],
            "consistency": consistency,
            "domains": domain_summaries,
            "failback_steps": sorted(
                failback_steps,
                key=lambda item: (item["service"], item["step"]),
            ),
            "cooldown_waits": sorted(
                cooldown_waits,
                key=lambda item: item["service"],
            ),
            "frozen_domains": final_state["frozen_domains"],
            "last_event_seq": final_state["last_event_seq"],
            "pointer_version": pointer_version,
            "policy_version": base.policy_version,
            "probe_plan": record.plan.as_dict(),
            "round_id": record.round_id,
            "round_watermark": final_state["round_watermark"],
            "sample_digest": base.sample.sample_digest,
            "snapshot_digest": snapshot_digest,
            "status": "committed",
            "switches": switches_by_service,
            "target_domains": list(record.target_domains),
        }
        return self.storage.finalize_round(
            round_record=record,
            final_state=final_state,
            query_summaries=query_summaries,
            switches=switches_by_service,
            result=result,
        )

    def resume(self, payload: Any | None = None) -> dict[str, Any]:
        payload = payload or {}
        if not isinstance(payload, dict):
            _request_error("Request body must be a JSON object.", "$")
        requested_round = payload.get("round_id")
        if requested_round is not None:
            if not is_int(requested_round) or requested_round <= 0:
                _request_error("round_id must be a positive integer.", "round_id")
            record = self.storage.get_round(requested_round)
            if record is None:
                fail(
                    "ROUND_NOT_FOUND",
                    "Round does not exist.",
                    context={"round_id": requested_round},
                    http_status=404,
                )
            records = [record]
        else:
            records = self.storage.list_prepared_rounds()

        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for record in records:
            try:
                results.append(self._execute_round(record))
            except BusinessError as exc:
                errors.append(exc.to_dict()["error"])
        return {
            "errors": errors,
            "resumed_rounds": [record.round_id for record in records],
            "results": results,
            "status": "ok" if not errors else "partial",
        }

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    def verify(self, payload: Any | None = None) -> dict[str, Any]:
        payload = payload or {}
        if not isinstance(payload, dict):
            _request_error("Request body must be a JSON object.", "$")
        round_id = payload.get("round_id")
        service_filter = payload.get("service")
        if round_id is not None and (not is_int(round_id) or round_id <= 0):
            _request_error("round_id must be a positive integer.", "round_id")
        if service_filter is not None and not isinstance(service_filter, str):
            _request_error("service must be a string.", "service")

        if round_id is None:
            after = self._require_current()
            before = after
            record = None
            result: dict[str, Any] | None = None
        else:
            record = self.storage.get_round(round_id)
            if record is None:
                fail(
                    "ROUND_NOT_FOUND",
                    "Round does not exist.",
                    context={"round_id": round_id},
                    http_status=404,
                )
            if record.status != "committed" or record.result is None:
                conflict(
                    "ROUND_NOT_COMMITTED",
                    "Only a committed round can be verified.",
                    context={"round_id": round_id},
                )
            result = record.result
            before = self.storage.snapshot_by_pointer(
                record.base_pointer_version
            )
            after = self.storage.snapshot_by_pointer(result["pointer_version"])
            if before is None or after is None:
                conflict(
                    "SNAPSHOT_NOT_FOUND",
                    "A snapshot required by verification is missing.",
                    context={"round_id": round_id},
                )

        assert before is not None and after is not None
        service_names = sorted(after.sample.services)
        if service_filter is not None:
            if service_filter not in after.sample.services:
                fail(
                    "SERVICE_NOT_FOUND",
                    "Service does not exist in this sample.",
                    context={"service": service_filter},
                    http_status=404,
                )
            service_names = [service_filter]

        service_reports: list[dict[str, Any]] = []
        overall = True
        for service_name in service_names:
            service = after.sample.services[service_name]
            before_primary = before.state["services"][service_name]["primary"]
            after_primary = after.state["services"][service_name]["primary"]
            before_bundle = query_bundle(
                service,
                before_primary,
                policy_version=before.policy_version,
                pointer_version=before.pointer_version,
            )
            after_bundle = query_bundle(
                service,
                after_primary,
                policy_version=after.policy_version,
                pointer_version=after.pointer_version,
            )
            raw_difference = first_difference(
                before_bundle["queries"],
                after_bundle["queries"],
                "queries",
            )
            first_divergence = None
            if raw_difference is not None:
                overall = False
                first_divergence = {
                    "field_path": raw_difference["field_path"],
                    "primary_instance": before_primary,
                    "primary_pointer_version": before.pointer_version,
                    "primary_value": raw_difference["left_value"],
                    "right_present": raw_difference["right_present"],
                    "left_present": raw_difference["left_present"],
                    "standby_instance": after_primary,
                    "standby_pointer_version": after.pointer_version,
                    "standby_value": raw_difference["right_value"],
                }

            replica_checks: list[dict[str, Any]] = []
            for instance in service.instances:
                bundle = query_bundle(
                    service,
                    instance.instance_id,
                    policy_version=after.policy_version,
                    pointer_version=after.pointer_version,
                )
                difference = first_difference(
                    after_bundle["queries"],
                    bundle["queries"],
                    "queries",
                )
                if difference is not None:
                    overall = False
                replica_checks.append(
                    {
                        "equal_to_primary": difference is None,
                        "first_divergence": difference,
                        "instance": instance.instance_id,
                        "query_digest": bundle["query_digest"],
                    }
                )
            replica_checks.sort(key=lambda item: item["instance"])

            committed_summary = after.summary_rows.get(service_name)
            summary_match = bool(
                committed_summary is not None
                and committed_summary.get("query_digest")
                == after_bundle["query_digest"]
            )
            if not summary_match:
                overall = False
            service_reports.append(
                {
                    "after": {
                        "instance": after_primary,
                        "pointer_version": after.pointer_version,
                        "query_digest": after_bundle["query_digest"],
                    },
                    "before": {
                        "instance": before_primary,
                        "pointer_version": before.pointer_version,
                        "query_digest": before_bundle["query_digest"],
                    },
                    "committed_summary_match": summary_match,
                    "equal": raw_difference is None,
                    "first_divergence": first_divergence,
                    "replica_checks": replica_checks,
                    "service": service_name,
                }
            )

        return {
            "pointer_versions": {
                "after": after.pointer_version,
                "before": before.pointer_version,
            },
            "policy_versions": {
                "after": after.policy_version,
                "before": before.policy_version,
            },
            "round_id": round_id,
            "service_filter": service_filter,
            "services": service_reports,
            "status": "consistent" if overall else "inconsistent",
            "switches": [] if result is None else result.get("switches", []),
            "verified": overall,
        }

    def ledger(self, round_id: int | None = None) -> dict[str, Any]:
        rows = self.storage.switch_ledger(round_id)
        return {
            "count": len(rows),
            "entries": rows,
            "round_id": round_id,
        }

