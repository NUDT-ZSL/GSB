from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from typing import Any

from .compare import compute_diff
from .errors import EngineError, Issue
from .jsonutil import canonical_json, sha256_hex
from .model import KIND_ORDER, EngineConfig, Resource
from .planning import generate_plan, public_plan
from .store import StateStore
from .validation import load_bundle


class CrashInjector:
    def __init__(self, specification: str | None) -> None:
        self.points: dict[str, int] = {}
        if specification:
            for item in specification.split(","):
                item = item.strip()
                if not item:
                    continue
                if ":" not in item:
                    raise EngineError(
                        "E_BAD_CLI",
                        "crash point must look like operation-start:2",
                    )
                point, raw_ordinal = item.rsplit(":", 1)
                try:
                    ordinal = int(raw_ordinal)
                except ValueError as exc:
                    raise EngineError(
                        "E_BAD_CLI", f"invalid crash ordinal: {item}"
                    ) from exc
                if ordinal < 1:
                    raise EngineError(
                        "E_BAD_CLI", "crash ordinal must be >= 1"
                    )
                self.points[point] = ordinal

    def maybe_crash(self, point: str, ordinal: int = 1) -> None:
        if self.points.get(point) != ordinal:
            return
        sys.stderr.write(
            canonical_json(
                {
                    "simulated_crash": point,
                    "ordinal": ordinal,
                }
            )
            + "\n"
        )
        sys.stderr.flush()
        os._exit(137)


class DriftEngine:
    def __init__(
        self, store: StateStore, crash_specification: str | None = None
    ) -> None:
        self.store = store
        self.crash = CrashInjector(crash_specification)

    def diff(self) -> dict[str, Any]:
        self.store.require_loaded()
        desired = self.store.desired()
        actual = self.store.actual()
        config = self.store.config()
        issues = self._issues()
        result = compute_diff(desired, actual, issues, config)
        result["validation_issues"] = self.store.issues()
        return result

    def plan(self) -> dict[str, Any]:
        diff = self.diff()
        desired = self.store.desired()
        actual = self.store.actual()
        config = self.store.config()
        plan = generate_plan(
            desired,
            actual,
            diff["drifts"],
            config,
            self._generations(),
        )
        result = public_plan(plan)
        result["comparison_mode"] = diff["algorithm"]["mode"]
        result["drift_count"] = diff["summary"]["drift_count"]
        return result

    def _issues(self) -> list[Issue]:
        issues: list[Issue] = []
        for item in self.store.issues():
            resource = item.get("resource")
            kind = namespace = name = None
            if resource:
                kind, namespace, name = resource.split("/", 2)
            issues.append(
                Issue(
                    code=item["code"],
                    source=item["source"],
                    message=item["message"],
                    path=item.get("path", "$"),
                    index=item.get("index"),
                    kind=kind,
                    namespace=namespace,
                    name=name,
                    quarantined=item.get("quarantined", True),
                )
            )
        return sorted(issues, key=Issue.sort_key)

    def _generations(self) -> dict[str, int]:
        return {
            key: int(state.get("generation", 0))
            for key, state in self.store.object_states().items()
        }

    def apply(
        self,
        *,
        max_rounds: int = 200,
        resume: bool = False,
    ) -> dict[str, Any]:
        self.store.require_loaded()
        if max_rounds < 1:
            raise EngineError("E_BAD_CLI", "max_rounds must be >= 1")
        config = self.store.config()
        recovery = self.store.recover()
        initial_diff = self.diff()
        initial_states = self.store.object_states()
        if self.store.get_meta("state") == "converged" and (
            initial_diff["summary"]["drift_count"] == 0
            or self._only_abandoned(
                initial_diff["drifts"], initial_states
            )
        ):
            return {
                "ok": True,
                "resumed": resume,
                "recovery": recovery,
                "rounds": [],
                "round_count": 0,
                "converged": True,
                "max_rounds_exhausted": False,
                "logical_clock_ms": self.store.clock(),
                "round_watermark": self.store.round_watermark(),
                "comparison_mode": initial_diff["algorithm"]["mode"],
                "remaining_drifts": initial_diff["drifts"],
                "abandoned_resources": [
                    self._state_identity(key, state)
                    for key, state in sorted(initial_states.items())
                    if state["phase"] == "abandoned"
                ],
            }
        round_results: list[dict[str, Any]] = []
        converged = False
        exhausted = False
        for _ in range(max_rounds):
            diff = self.diff()
            if round_results and self._only_abandoned(
                diff["drifts"], self.store.object_states()
            ):
                converged = True
                break
            plan = generate_plan(
                self.store.desired(),
                self.store.actual(),
                diff["drifts"],
                config,
                self._generations(),
            )
            if not plan["valid"]:
                raise EngineError(
                    "E_PLAN_INVALID",
                    "convergence plan violates dependency or safety constraints",
                    details=public_plan(plan),
                    exit_code=1,
                )
            round_no = self.store.allocate_round(self.store.clock())
            self.store.store_round_operations(
                round_no, plan["operations"]
            )
            round_result = self._execute_round(
                round_no=round_no,
                plan=plan,
                diff=diff,
            )
            self.crash.maybe_crash("round-commit", len(round_results) + 1)
            post_diff = self.diff()
            states = self._post_round_states(
                post_diff,
                round_result["_failed_identities"],
                round_result["_abandoned_identities"],
            )
            public_round_result = {
                key: value
                for key, value in round_result.items()
                if not key.startswith("_")
            }
            public_round_result["status"] = "committed"
            self.store.commit_round(
                round_no=round_no,
                started_ms=round_result["logical_start_ms"],
                ended_ms=round_result["logical_end_ms"],
                summary=public_round_result,
                operation_outcomes=round_result["operations"],
                object_states=states,
                comparison_mode=post_diff["algorithm"]["mode"],
            )
            round_results.append(public_round_result)
            if post_diff["summary"]["drift_count"] == 0:
                converged = True
                break
            if self._only_abandoned(
                post_diff["drifts"], self.store.object_states()
            ):
                converged = True
                break
        else:
            exhausted = True

        final_diff = self.diff()
        final_states = self.store.object_states()
        if final_diff["summary"]["drift_count"] == 0:
            converged = True
        elif self._only_abandoned(final_diff["drifts"], final_states):
            converged = True
        if exhausted:
            converged = False
            self.store.set_meta("state", "max_rounds_exhausted")

        if converged:
            self.store.set_meta("state", "converged")
        return {
            "ok": converged,
            "resumed": resume,
            "recovery": recovery,
            "rounds": round_results,
            "round_count": len(round_results),
            "converged": converged,
            "max_rounds_exhausted": exhausted,
            "logical_clock_ms": self.store.clock(),
            "round_watermark": self.store.round_watermark(),
            "comparison_mode": final_diff["algorithm"]["mode"],
            "remaining_drifts": final_diff["drifts"],
            "abandoned_resources": [
                self._state_identity(key, state)
                for key, state in sorted(final_states.items())
                if state["phase"] == "abandoned"
            ],
        }

    def _execute_round(
        self,
        *,
        round_no: int,
        plan: dict[str, Any],
        diff: dict[str, Any],
    ) -> dict[str, Any]:
        config = self.store.config()
        states = self.store.object_states()
        operations = plan["operations"]
        by_id = {operation["operation_id"]: operation for operation in operations}
        logical_start = self.store.clock()
        outcomes: dict[str, dict[str, Any]] = {}
        selected_ids: list[str] = []
        started_ordinal = 0
        ready_count = 0
        unavailable_dependencies: set[str] = set()

        def base_outcome(
            operation: dict[str, Any],
            outcome_class: str,
            outcome: str,
            reason: str,
        ) -> dict[str, Any]:
            return {
                "operation_id": operation["operation_id"],
                "action": operation["action"],
                "identity": operation["identity"],
                "outcome": outcome,
                "outcome_class": outcome_class,
                "reason": reason,
                "started": False,
            }

        # Noop/maintain operations do not consume the mutation budget.
        for operation in operations:
            if operation["action"] != "noop":
                continue
            identity = operation["identity"]
            state = states[operation["resource_key"]]
            if state["abandoned"]:
                outcome = base_outcome(
                    operation,
                    "abandoned",
                    "abandoned",
                    "object_was_explicitly_abandoned",
                )
                outcomes[operation["operation_id"]] = outcome
                unavailable_dependencies.add(operation["operation_id"])
                continue
            existing = self.store.committed_result(
                operation["operation_id"]
            )
            if existing is not None:
                outcome = {
                    **base_outcome(
                        operation,
                        "committed",
                        "noop",
                        "idempotent_noop_replay",
                    ),
                    "idempotent_replay": True,
                }
                outcomes[operation["operation_id"]] = outcome
                continue
            result = {
                "operation_id": operation["operation_id"],
                "action": "noop",
                "identity": identity,
                "outcome": "noop",
                "outcome_class": "committed",
                "started_ms": logical_start,
                "finished_ms": logical_start,
                "target_digest": sha256_hex(
                    operation["payload"].get("object", {})
                ),
            }
            committed = self.store.commit_operation(
                round_no=round_no,
                operation=operation,
                finished_ms=logical_start,
                result=result,
            )
            outcomes[operation["operation_id"]] = {
                **result,
                "idempotent_replay": committed.get(
                    "idempotent_replay", False
                ),
            }

        ready: list[dict[str, Any]] = []
        for operation_id in plan["topological_order"]:
            operation = by_id[operation_id]
            if operation["action"] == "noop":
                continue
            state = states[operation["resource_key"]]
            if state["abandoned"]:
                outcomes[operation_id] = base_outcome(
                    operation,
                    "abandoned",
                    "abandoned",
                    "object_was_explicitly_abandoned",
                )
                unavailable_dependencies.add(operation_id)
                continue
            if any(
                dependency in unavailable_dependencies
                for dependency in operation["depends_on"]
            ):
                outcomes[operation_id] = base_outcome(
                    operation,
                    "abandoned",
                    "abandoned",
                    "prerequisite_operation_abandoned",
                )
                unavailable_dependencies.add(operation_id)
                continue
            if self.store.committed_result(operation_id) is not None:
                outcomes[operation_id] = {
                    **base_outcome(
                        operation,
                        "committed",
                        "committed",
                        "idempotent_committed_replay",
                    ),
                    "idempotent_replay": True,
                }
                continue
            if not all(
                self.store.committed_result(dependency) is not None
                for dependency in operation["depends_on"]
            ):
                outcomes[operation_id] = base_outcome(
                    operation,
                    "not_started_dependency",
                    "deferred",
                    "prerequisite_operation_not_committed",
                )
                continue
            next_retry = state.get("next_retry_at_ms")
            if next_retry is not None and logical_start < next_retry:
                outcomes[operation_id] = {
                    **base_outcome(
                        operation,
                        "not_started_retry_backoff",
                        "deferred",
                        "logical_retry_backoff_pending",
                    ),
                    "next_retry_at_ms": next_retry,
                }
                continue
            ready.append(operation)

        ready_count = len(ready)
        selected = ready[: config.max_concurrency]
        deferred = ready[config.max_concurrency :]
        for operation in deferred:
            outcomes[operation["operation_id"]] = base_outcome(
                operation,
                "not_started_budget",
                "deferred",
                "round_mutation_budget_exhausted",
            )

        failed_identities: set[str] = set()
        abandoned_identities: set[str] = {
            key
            for key, state in states.items()
            if state["abandoned"]
        }
        max_finish = logical_start

        started_batch: list[dict[str, Any]] = []
        for operation in selected:
            operation_id = operation["operation_id"]
            selected_ids.append(operation_id)
            started_ordinal += 1
            state = states[operation["resource_key"]]
            failures = int(state.get("failures", 0)) + 1
            attempt = failures
            duration = config.duration_for(operation["resource_key"])
            timed_out = duration > config.operation_timeout_ms
            elapsed = (
                config.operation_timeout_ms if timed_out else duration
            )
            finished = logical_start + elapsed
            self.store.reserve_slot(
                round_no=round_no,
                operation=operation,
                started_ms=logical_start,
                max_concurrency=config.max_concurrency,
                attempt=attempt,
            )
            self.crash.maybe_crash(
                "operation-start", started_ordinal
            )
            started_batch.append(
                {
                    "ordinal": started_ordinal,
                    "operation": operation,
                    "failures": failures,
                    "attempt": attempt,
                    "duration": duration,
                    "timed_out": timed_out,
                    "finished": finished,
                }
            )

        for started in started_batch:
            operation = started["operation"]
            operation_id = operation["operation_id"]
            failures = started["failures"]
            attempt = started["attempt"]
            duration = started["duration"]
            timed_out = started["timed_out"]
            finished = started["finished"]
            injected_failure = config.should_fail(
                operation["resource_key"]
            )
            if timed_out or injected_failure:
                abandoned = failures > config.retry_limit
                next_retry = (
                    None
                    if abandoned
                    else finished + config.retry_backoff_ms
                )
                reason = (
                    "operation_timeout_budget_exceeded"
                    if timed_out
                    else "injected_operation_failure"
                )
                result = {
                    "operation_id": operation_id,
                    "action": operation["action"],
                    "identity": operation["identity"],
                    "outcome": "started_failed",
                    "outcome_class": "started_failed",
                    "reason": reason,
                    "started": True,
                    "started_ms": logical_start,
                    "finished_ms": finished,
                    "simulated_duration_ms": duration,
                    "operation_timeout_ms": config.operation_timeout_ms,
                    "attempt": attempt,
                    "abandoned": abandoned,
                    "next_retry_at_ms": next_retry,
                }
                self.store.record_failure(
                    round_no=round_no,
                    operation=operation,
                    finished_ms=finished,
                    result=result,
                    failures=failures,
                    abandoned=abandoned,
                    next_retry_at_ms=next_retry,
                )
                outcomes[operation_id] = result
                failed_identities.add(operation["resource_key"])
                if abandoned:
                    abandoned_identities.add(operation["resource_key"])
                max_finish = max(max_finish, finished)
                self.crash.maybe_crash(
                    "operation-commit", started["ordinal"]
                )
                continue

            target_digest = (
                sha256_hex(operation["payload"]["object"])
                if "object" in operation["payload"]
                else sha256_hex(
                    {
                        "identity": operation["identity"],
                        "action": "delete",
                    }
                )
            )
            result = {
                "operation_id": operation_id,
                "action": operation["action"],
                "identity": operation["identity"],
                "outcome": "committed",
                "outcome_class": "committed",
                "started": True,
                "started_ms": logical_start,
                "finished_ms": finished,
                "simulated_duration_ms": duration,
                "operation_timeout_ms": config.operation_timeout_ms,
                "target_digest": target_digest,
                "attempt": attempt,
            }
            committed = self.store.commit_operation(
                round_no=round_no,
                operation=operation,
                finished_ms=finished,
                result=result,
            )
            outcomes[operation_id] = {
                **result,
                "idempotent_replay": committed.get(
                    "idempotent_replay", False
                ),
            }
            max_finish = max(max_finish, finished)
            self.crash.maybe_crash(
                "operation-commit", started["ordinal"]
            )

        backoff_targets = [
            outcome["next_retry_at_ms"]
            for outcome in outcomes.values()
            if outcome["outcome_class"] == "not_started_retry_backoff"
            and outcome.get("next_retry_at_ms") is not None
        ]
        if not selected and backoff_targets:
            max_finish = self.store.advance_clock(min(backoff_targets))

        ordered_outcomes = [
            outcomes[operation["operation_id"]]
            for operation in operations
        ]
        for outcome in ordered_outcomes:
            if outcome.get("outcome_class") == "abandoned":
                identity = outcome["identity"]
                abandoned_identities.add(
                    f"{identity['kind']}/{identity['namespace']}/"
                    f"{identity['name']}"
                )
        class_counts = Counter(
            outcome["outcome_class"] for outcome in ordered_outcomes
        )
        action_counts = Counter(
            outcome["outcome"] for outcome in ordered_outcomes
        )
        namespace_groups: dict[str, list[str]] = defaultdict(list)
        for operation_id in selected_ids:
            operation = by_id[operation_id]
            namespace_groups[operation["identity"]["namespace"]].append(
                operation_id
            )
        deferred_outcomes = [
            outcome
            for outcome in ordered_outcomes
            if outcome["outcome_class"]
            in (
                "not_started_budget",
                "not_started_dependency",
                "not_started_retry_backoff",
            )
        ]
        summary = {
            "round_no": round_no,
            "status": "executing",
            "comparison_mode": diff["algorithm"]["mode"],
            "logical_start_ms": logical_start,
            "logical_end_ms": max_finish,
            "counts": {
                "planned": len(operations),
                "started": len(selected_ids),
                "committed": int(
                    class_counts.get("committed", 0)
                ),
                "noop": int(action_counts.get("noop", 0)),
                "deferred": len(deferred_outcomes),
                "failed": int(
                    class_counts.get("started_failed", 0)
                ),
                "abandoned": int(
                    class_counts.get("abandoned", 0)
                ),
            },
            "outcome_classes": dict(sorted(class_counts.items())),
            "budget": {
                "max_concurrency": config.max_concurrency,
                "ready_mutations": ready_count,
                "started_mutations": len(selected_ids),
                "deferred_ready_mutations": len(deferred),
            },
            "namespace_groups": {
                key: sorted(value)
                for key, value in sorted(namespace_groups.items())
            },
            "operations": ordered_outcomes,
        }
        return {
            **summary,
            "_failed_identities": failed_identities,
            "_abandoned_identities": abandoned_identities,
        }

    def _post_round_states(
        self,
        diff: dict[str, Any],
        failed_identities: set[str],
        abandoned_identities: set[str],
    ) -> dict[str, dict[str, Any]]:
        desired = self.store.desired()
        actual = self.store.actual()
        states = self.store.object_states()
        drift_groups: dict[
            tuple[str, str, str], list[dict[str, Any]]
        ] = defaultdict(list)
        for drift in diff["drifts"]:
            identity = drift["identity"]
            drift_groups[
                (identity["kind"], identity["namespace"], identity["name"])
            ].append(drift)
        result: dict[str, dict[str, Any]] = {}
        for key, state in states.items():
            identity_key = (
                state["kind"],
                state["namespace"],
                state["name"],
            )
            identity_drifts = drift_groups.get(identity_key, [])
            reason = list(state.get("reason", []))
            if identity_drifts:
                reason.append(
                    {
                        "kind": "recomputed_drift_evidence",
                        "drifts": [
                            {
                                "type": drift["type"],
                                "field_path": drift["field_path"],
                                "expected": drift["expected"],
                                "actual": drift["actual"],
                                "basis": drift["basis"],
                            }
                            for drift in sorted(
                                identity_drifts,
                                key=lambda item: (
                                    item["field_path"],
                                    item["type"],
                                ),
                            )
                        ],
                    }
                )
            abandoned = key in abandoned_identities or state["abandoned"]
            if not identity_drifts:
                phase = "converged"
                abandoned = False
                failures = 0
                next_retry = None
            elif abandoned:
                phase = "abandoned"
                failures = int(state.get("failures", 0))
                next_retry = None
            elif key in failed_identities or state.get("phase") == "failed":
                phase = "failed"
                failures = int(state.get("failures", 0))
                next_retry = state.get("next_retry_at_ms")
            else:
                phase = "pending"
                failures = int(state.get("failures", 0))
                next_retry = state.get("next_retry_at_ms")
            result[key] = {
                "kind": state["kind"],
                "namespace": state["namespace"],
                "name": state["name"],
                "desired_exists": key in desired,
                "actual_exists": key in actual,
                "phase": phase,
                "failures": failures,
                "abandoned": abandoned,
                "next_retry_at_ms": next_retry,
                "reason": reason,
            }
        return result

    def _only_abandoned(
        self,
        drifts: list[dict[str, Any]],
        states: dict[str, dict[str, Any]],
    ) -> bool:
        if not drifts:
            return True
        for drift in drifts:
            identity = drift["identity"]
            key = (
                f"{identity['kind']}/{identity['namespace']}/"
                f"{identity['name']}"
            )
            state = states.get(key)
            if state is None or not state.get("abandoned") or (
                state.get("phase") != "abandoned"
            ):
                return False
        return True

    def status(self) -> dict[str, Any]:
        self.store.require_loaded()
        diff = self.diff()
        states = self.store.object_states()
        drift_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for drift in diff["drifts"]:
            identity = drift["identity"]
            key = (
                f"{identity['kind']}/{identity['namespace']}/"
                f"{identity['name']}"
            )
            drift_groups[key].append(drift)
        objects = []
        for key, state in sorted(
            states.items(),
            key=lambda item: (
                item[1]["namespace"],
                KIND_ORDER.get(item[1]["kind"], 99),
                item[1]["name"],
                item[0],
            ),
        ):
            residual = drift_groups.get(key, [])
            basis_chain = [
                {
                    "step": "persisted_state",
                    "phase": state["phase"],
                    "abandoned": state["abandoned"],
                    "failures": state["failures"],
                    "generation": state["generation"],
                }
            ]
            if residual:
                basis_chain.append(
                    {
                        "step": "current_diff",
                        "drifts": [
                            {
                                "type": drift["type"],
                                "field_path": drift["field_path"],
                                "basis": drift["basis"],
                            }
                            for drift in residual
                        ],
                    }
                )
            if state.get("reason"):
                basis_chain.append(
                    {
                        "step": "recorded_reason",
                        "reason": state["reason"],
                    }
                )
            objects.append(
                {
                    "identity": {
                        "kind": state["kind"],
                        "namespace": state["namespace"],
                        "name": state["name"],
                    },
                    "desired_exists": state["desired_exists"],
                    "actual_exists": state["actual_exists"],
                    "phase": state["phase"],
                    "comparison_mode": diff["algorithm"]["mode"],
                    "failures": state["failures"],
                    "abandoned": state["abandoned"],
                    "generation": state["generation"],
                    "next_retry_at_ms": state["next_retry_at_ms"],
                    "residual_drift_count": len(residual),
                    "basis_chain": basis_chain,
                }
            )
        return {
            "ok": True,
            "state": self.store.get_meta("state", "unknown"),
            "comparison_mode": diff["algorithm"]["mode"],
            "algorithm": diff["algorithm"],
            "logical_clock_ms": self.store.clock(),
            "round_watermark": self.store.round_watermark(),
            "active_concurrency": self.store.active_slots(),
            "committed_operation_count": self.store.committed_count(),
            "objects": objects,
        }

    def verify(self) -> dict[str, Any]:
        self.store.require_loaded()
        diff = self.diff()
        states = self.store.object_states()
        integrity = self.store.integrity_snapshot()
        ledger = self.store.ledger_audit()
        checks: list[dict[str, Any]] = []

        def add_check(name: str, ok: bool, details: Any) -> None:
            checks.append({"name": name, "ok": ok, "details": details})

        add_check(
            "no_active_concurrency_slots",
            not integrity["active_slots"],
            integrity["active_slots"],
        )
        add_check(
            "no_started_or_planned_operations",
            integrity["started_or_planned_operations"] == 0,
            integrity["started_or_planned_operations"],
        )
        add_check(
            "no_executing_rounds",
            integrity["executing_rounds"] == 0,
            integrity["executing_rounds"],
        )
        add_check(
            "committed_operation_ledger",
            integrity["committed_without_ledger"] == 0
            and integrity["ledger_without_committed_operation"] == 0
            and integrity["duplicate_ledger_entries"] == 0,
            {
                "committed_without_ledger": integrity[
                    "committed_without_ledger"
                ],
                "ledger_without_committed_operation": integrity[
                    "ledger_without_committed_operation"
                ],
                "duplicate_ledger_entries": integrity[
                    "duplicate_ledger_entries"
                ],
            },
        )
        invalid_ledger = [
            entry for entry in ledger if not entry["digest_valid"]
        ]
        add_check(
            "ledger_result_digests",
            not invalid_ledger,
            invalid_ledger,
        )

        rounds = self.store.rounds()
        committed_rounds = [
            item for item in rounds if item["status"] == "committed"
        ]
        max_committed = (
            max(item["round_no"] for item in committed_rounds)
            if committed_rounds
            else 0
        )
        add_check(
            "round_watermark",
            max_committed == self.store.round_watermark(),
            {
                "round_watermark": self.store.round_watermark(),
                "max_committed_round": max_committed,
            },
        )

        desired = self.store.desired()
        actual = self.store.actual()
        config = self.store.config()
        desired_doc = {
            "objects": [resource.body for resource in desired.values()]
        }
        actual_doc = {
            "objects": [resource.body for resource in actual.values()]
        }
        revalidated_desired, revalidated_actual, _, revalidation_issues = (
            load_bundle(desired_doc, actual_doc, config.to_dict())
        )
        closure_ok = (
            set(revalidated_desired) == set(desired)
            and set(revalidated_actual) == set(actual)
        )
        add_check(
            "resource_reference_closure",
            closure_ok,
            {
                "valid": closure_ok,
                "issues": [
                    issue.to_dict() for issue in revalidation_issues
                ],
            },
        )

        drift_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for drift in diff["drifts"]:
            identity = drift["identity"]
            key = (
                f"{identity['kind']}/{identity['namespace']}/"
                f"{identity['name']}"
            )
            drift_groups[key].append(drift)

        phase_issues: list[dict[str, Any]] = []
        for key, state in sorted(states.items()):
            residual = drift_groups.get(key, [])
            if not residual:
                expected_phase = "converged"
            elif state.get("abandoned"):
                expected_phase = "abandoned"
            else:
                expected_phase = state["phase"]
            if state["phase"] != expected_phase:
                phase_issues.append(
                    {
                        "resource": key,
                        "stored_phase": state["phase"],
                        "expected_phase": expected_phase,
                        "residual_drift_count": len(residual),
                    }
                )
        add_check(
            "object_phase_consistency",
            not phase_issues,
            phase_issues,
        )

        unexpected = []
        for key, drifts in sorted(drift_groups.items()):
            state = states.get(key)
            if state is None or not state.get("abandoned"):
                unexpected.append(key)
        add_check(
            "residual_drifts_only_explicitly_abandoned",
            not unexpected,
            {
                "unexpected_residual_resources": unexpected,
                "residual_drift_count": diff["summary"]["drift_count"],
            },
        )

        risk_objects = []
        for key in sorted(set(drift_groups) | {
            key for key, state in states.items() if state["abandoned"]
        }):
            state = states.get(key, {})
            residual = drift_groups.get(key, [])
            classification = (
                "abandoned_drift"
                if state.get("abandoned")
                else "unexpected_residual_drift"
            )
            chain = [
                {
                    "step": 1,
                    "source": "recomputed_diff",
                    "result": "drift"
                    if residual
                    else "no_residual_drift",
                    "evidence": [
                        {
                            "type": drift["type"],
                            "field_path": drift["field_path"],
                            "expected": drift["expected"],
                            "actual": drift["actual"],
                            "basis": drift["basis"],
                        }
                        for drift in residual
                    ],
                },
                {
                    "step": 2,
                    "source": "sqlite.object_state",
                    "phase": state.get("phase"),
                    "abandoned": state.get("abandoned"),
                    "failures": state.get("failures"),
                    "generation": state.get("generation"),
                    "reason": state.get("reason", []),
                },
                {
                    "step": 3,
                    "source": "ledger",
                    "committed_operation_count": integrity[
                        "committed_operations"
                    ],
                    "logical_clock_ms": integrity["logical_clock_ms"],
                    "round_watermark": integrity["round_watermark"],
                },
            ]
            risk_objects.append(
                {
                    "identity": {
                        "kind": state.get("kind"),
                        "namespace": state.get("namespace"),
                        "name": state.get("name"),
                    },
                    "classification": classification,
                    "phase": state.get("phase"),
                    "basis_chain": chain,
                }
            )

        consistent = all(check["ok"] for check in checks)
        return {
            "ok": consistent,
            "consistent": consistent,
            "checks": checks,
            "comparison_mode": diff["algorithm"]["mode"],
            "summary": {
                "residual_drift_count": diff["summary"]["drift_count"],
                "risk_object_count": len(risk_objects),
                "allowed_abandoned_count": sum(
                    1 for state in states.values() if state["abandoned"]
                ),
                "committed_operation_count": integrity[
                    "committed_operations"
                ],
                "logical_clock_ms": self.store.clock(),
                "round_watermark": self.store.round_watermark(),
            },
            "risk_objects": risk_objects,
            "remaining_drifts": diff["drifts"],
        }

    @staticmethod
    def _state_identity(
        key: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "resource": key,
            "identity": {
                "kind": state["kind"],
                "namespace": state["namespace"],
                "name": state["name"],
            },
            "phase": state["phase"],
            "failures": state["failures"],
            "reason": state["reason"],
        }

