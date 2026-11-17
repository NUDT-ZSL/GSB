from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .canonical import canonical_json, sha256_json
from .declarations import MAX_BATCH_SIZE
from .errors import (
    GatewayError,
    InputError,
    ReplayError,
    RouteError,
    SnapshotError,
    StateError,
    UpstreamError,
)
from .fingerprint import (
    derive_fingerprint,
    fingerprint_difference,
    fingerprint_map,
    materialize_backend_states,
)
from .inference import FingerprintMismatch
from .services import TcpInferenceClient
from .logger import StructuredLogger
from .models import BackendDecl, BackendState, DeclarationBundle, Request, RuleDecl
from .stats import compute_stats, diff_stats, empty_stats, stats_summary
from .storage import SessionStore


@dataclass
class ExecutionReport:
    responses: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    batch_ids: list[str]
    stats_version: int
    stats: dict[str, Any]
    invalidated_entry_count: int
    void_batch_ids: list[str]
    redispatched_batch_ids: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "batch_ids": list(self.batch_ids),
            "invalidated_entries": self.invalidated_entry_count,
            "redispatched_batch_ids": list(self.redispatched_batch_ids),
            "request_count": len(self.responses),
            "stats_version": self.stats_version,
            "stats": stats_summary(self.stats),
            "void_batch_ids": list(self.void_batch_ids),
        }


class GatewayEngine:
    """Deterministic router and fingerprint-bound ledger state machine."""

    def __init__(
        self,
        *,
        store: SessionStore,
        bundle: DeclarationBundle,
        logger: StructuredLogger,
        client: TcpInferenceClient | None = None,
        redispatch_policy: str = "always",
        max_redispatch: int = 1,
    ) -> None:
        self.store = store
        self.bundle = bundle
        self.logger = logger
        self.client = client or TcpInferenceClient()
        self.redispatch_policy = redispatch_policy
        self.max_redispatch = max_redispatch
        self._lock = threading.RLock()
        self._backend_declarations = bundle.backend_by_id
        self._current_states: dict[str, BackendState] = {}
        self._last_report: ExecutionReport | None = None
        self._execution_hook: Callable[[dict[str, Any]], None] | None = None
        self._next_execution_hook: Callable[[dict[str, Any]], None] | None = None
        self.evolution = self._bootstrap()
        self.state: dict[str, Any]

    @property
    def states(self) -> dict[str, BackendState]:
        with self._lock:
            return dict(self._current_states)

    def _bootstrap(self) -> dict[str, Any]:
        with self._lock:
            previous_state = self.store.get_current_state()
            if previous_state is None:
                states = materialize_backend_states(self.bundle.backends, {})
                self._current_states = states
                state = self._empty_state(states)
                self.state = state
                self.store.replace_current_view(state)
                summary = {
                    "affected_backends": sorted(states),
                    "affected_rules": sorted(rule.rule_id for rule in self.bundle.rules),
                    "fingerprints": fingerprint_map(states),
                    "reason": "INITIAL_LOAD",
                    "stats_version": 0,
                    "type": "SESSION_INITIALIZED",
                }
                self.state["last_evolution"] = summary
                self.store.replace_current_view(self.state)
                self.logger.info(
                    "SESSION_INITIALIZED",
                    "fresh gateway session initialized",
                    **summary,
                )
                return summary

            old_states = self._states_from_state(previous_state)
            new_states = materialize_backend_states(self.bundle.backends, old_states)
            self._current_states = new_states
            diff = fingerprint_difference(old_states, new_states)
            rule_changed = previous_state.get("rule_version") != self.bundle.rule_version
            changed = bool(
                diff["affected"] or rule_changed
            )
            recovered_batches = self._mark_restart_inflight_batches(previous_state)
            stats_dirty = bool(previous_state.get("stats_dirty"))
            if not changed and not recovered_batches and not stats_dirty:
                previous_state["backend_states"] = self._serialize_states(new_states)
                previous_state["rules"] = self.bundle.rules_document()
                previous_state["rule_version"] = self.bundle.rule_version
                self.state = previous_state
                self.store.replace_current_view(self.state)
                summary = {
                    "affected_backends": [],
                    "affected_rules": [],
                    "fingerprints": fingerprint_map(new_states),
                    "reason": "DECLARATIONS_UNCHANGED",
                    "stats_version": int(previous_state["stats_version"]),
                    "type": "SESSION_REOPENED",
                }
                self.logger.info(
                    "SESSION_REOPENED",
                    "declarations match the current fingerprint set",
                    **summary,
                )
                return summary

            state = previous_state
            old_rule_map = {
                str(rule["rule_id"]): str(rule["target_backend"])
                for rule in state.get("rules", [])
                if isinstance(rule, dict) and "rule_id" in rule
            }
            new_rule_map = {rule.rule_id: rule.target_backend for rule in self.bundle.rules}
            affected_backends = set(diff["affected"])
            affected_rules = set()
            for rule_id in set(old_rule_map) | set(new_rule_map):
                old_target = old_rule_map.get(rule_id)
                new_target = new_rule_map.get(rule_id)
                if old_target != new_target:
                    affected_rules.add(rule_id)
                elif old_target in affected_backends or new_target in affected_backends:
                    affected_rules.add(rule_id)
            invalidated_count = 0
            for entry in state.get("ledger", []):
                if not entry.get("effective"):
                    continue
                status = self._binding_status_against(entry, new_states)
                if status["valid"]:
                    continue
                entry["effective"] = 0
                entry["invalid_reason"] = status["reason"]
                current = new_states.get(entry["backend_id"])
                entry["invalidated_by_fingerprint"] = (
                    current.fingerprint if current is not None else None
                )
                invalidated_count += 1

            void_batch_ids = sorted(set(recovered_batches))
            for batch in state.get("batches", []):
                if batch.get("status") != "IN_FLIGHT":
                    continue
                if batch["batch_id"] not in void_batch_ids:
                    void_batch_ids.append(batch["batch_id"])
                batch["status"] = "VOID"
                batch["error_code"] = "E_FINGERPRINT_CHANGED"
                batch["error_message"] = "batch voided during declaration evolution"
                for entry in state.get("ledger", []):
                    if entry.get("batch_id") == batch["batch_id"] and not entry.get("committed"):
                        entry["void"] = 1 if isinstance(entry.get("void"), int) else True
                        entry["effective"] = 0
                        entry["invalid_reason"] = "IN_FLIGHT_FINGERPRINT_CHANGED"
                invalidated_count += 1

            state["view_version"] = int(state.get("view_version", 0)) + 1
            state["event_epoch"] = int(state.get("event_epoch", 0)) + 1
            state["backend_states"] = self._serialize_states(new_states)
            state["rules"] = self.bundle.rules_document()
            state["rule_version"] = self.bundle.rule_version
            old_stats = state.get("stats") or empty_stats(self.bundle, new_states)
            new_stats = compute_stats(state.get("ledger", []), self.bundle, new_states)
            publish_reason = (
                "FINGERPRINT_EVOLUTION"
                if changed
                else "STATS_RECOMPUTED_AFTER_RESTART"
            )
            stats_version, stats_meta = self._publish_stats(
                state=state,
                current_stats=new_stats,
                previous_stats=old_stats,
                reason=publish_reason,
                affected_rules=affected_rules,
                affected_backends=affected_backends,
                invalidated_entry_count=invalidated_count,
                void_batch_ids=void_batch_ids,
                redispatched_batch_ids=[],
                extra={"fingerprint_diff": diff, "rule_version_changed": rule_changed},
            )
            state["stats"] = new_stats
            state["stats_version"] = stats_version
            state["stats_dirty"] = False
            summary = {
                "affected_backends": sorted(affected_backends),
                "affected_rules": sorted(affected_rules),
                "changed_backends": diff["changed"],
                "fingerprint_diff": diff,
                "fingerprints": fingerprint_map(new_states),
                "invalidated_entry_count": invalidated_count,
                "reason": publish_reason,
                "recalculated_stat_keys": stats_meta["recalculated_stat_keys"],
                "redispatched_batches": [],
                "stats_version": stats_version,
                "type": publish_reason,
                "unchanged_stat_keys": stats_meta["unchanged_stat_keys"],
                "view_version": state["view_version"],
                "void_batch_ids": void_batch_ids,
            }
            state["last_evolution"] = summary
            self.state = state
            self.store.replace_current_view(state)
            self.logger.info(
                "FINGERPRINT_EVOLUTION",
                "backend fingerprint set changed; current view replaced atomically",
                **summary,
            )
            return summary

    def _states_from_state(self, state: dict[str, Any]) -> dict[str, BackendState]:
        states: dict[str, BackendState] = {}
        for raw in state.get("backend_states", []):
            value = BackendState.from_dict(raw)
            states[value.backend_id] = value
        return states

    def _serialize_states(self, states: dict[str, BackendState]) -> list[dict[str, Any]]:
        return [states[key].to_dict() for key in sorted(states)]

    def _empty_state(self, states: dict[str, BackendState]) -> dict[str, Any]:
        return {
            "backend_states": self._serialize_states(states),
            "batches": [],
            "event_epoch": 0,
            "last_evolution": None,
            "last_run_summary": None,
            "last_run_trace": [],
            "ledger": [],
            "next_batch_seq": 1,
            "next_entry_seq": 1,
            "replay_requests": [],
            "rule_version": self.bundle.rule_version,
            "rules": self.bundle.rules_document(),
            "schema_version": 1,
            "stats": empty_stats(self.bundle, states),
            "stats_dirty": False,
            "stats_version": 0,
            "view_version": 1,
        }

    def _mark_restart_inflight_batches(self, state: dict[str, Any]) -> list[str]:
        recovered: list[str] = []
        for batch in state.get("batches", []):
            if batch.get("status") != "IN_FLIGHT":
                continue
            batch["status"] = "VOID"
            batch["error_code"] = "E_RESTART_IN_FLIGHT"
            batch["error_message"] = "batch was still in flight when the session reopened"
            recovered.append(str(batch["batch_id"]))
            for entry in state.get("ledger", []):
                if entry.get("batch_id") == batch["batch_id"] and not entry.get("committed"):
                    entry["void"] = 1 if isinstance(entry.get("void"), int) else True
                    entry["effective"] = 0
                    entry["invalid_reason"] = "RESTART_IN_FLIGHT"
        return sorted(recovered)

    def _binding_status_against(
        self, entry: dict[str, Any], states: dict[str, BackendState]
    ) -> dict[str, Any]:
        backend_id = str(entry.get("backend_id"))
        current = states.get(backend_id)
        if current is None:
            return {
                "reason": "BACKEND_ABSENT",
                "valid": False,
                "current_fingerprint": None,
                "current_load_sequence": None,
            }
        if entry.get("bound_fingerprint") != current.fingerprint:
            return {
                "reason": "FINGERPRINT_CHANGED",
                "valid": False,
                "current_fingerprint": current.fingerprint,
                "current_load_sequence": current.load_sequence,
            }
        if int(entry.get("bound_load_sequence", -1)) != current.load_sequence:
            return {
                "reason": "LOAD_SEQUENCE_CHANGED",
                "valid": False,
                "current_fingerprint": current.fingerprint,
                "current_load_sequence": current.load_sequence,
            }
        return {
            "reason": "BOUND_TO_CURRENT_FINGERPRINT",
            "valid": True,
            "current_fingerprint": current.fingerprint,
            "current_load_sequence": current.load_sequence,
        }

    def binding_status(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._binding_status_against(entry, self._current_states)

    def review_bindings(self) -> dict[str, Any]:
        with self._lock:
            invalid: list[dict[str, Any]] = []
            checked = 0
            for entry in self.state.get("ledger", []):
                if not entry.get("effective"):
                    continue
                checked += 1
                status = self._binding_status_against(entry, self._current_states)
                if not status["valid"]:
                    invalid.append(
                        {
                            "entry_id": entry["entry_id"],
                            "reason": status["reason"],
                            "request_id": entry["request_id"],
                        }
                    )
                    entry["effective"] = 0
                    entry["invalid_reason"] = status["reason"]
                    entry["invalidated_by_fingerprint"] = status["current_fingerprint"]
            summary = {
                "checked_entries": checked,
                "invalid_entries": sorted(invalid, key=lambda item: item["entry_id"]),
            }
            self.logger.info(
                "BINDING_REVIEW",
                "fingerprint binding review completed",
                **summary,
            )
            if invalid:
                self._persist()
            return summary

    def _persist(self) -> None:
        self.store.replace_current_view(self.state)

    def _next_batch_id(self) -> tuple[str, int]:
        sequence = int(self.state["next_batch_seq"])
        self.state["next_batch_seq"] = sequence + 1
        return f"b-{sequence:06d}", sequence

    def _next_entry_id(self) -> tuple[str, int]:
        sequence = int(self.state["next_entry_seq"])
        self.state["next_entry_seq"] = sequence + 1
        return f"e-{sequence:06d}", sequence

    def _route(self, request: Request) -> RuleDecl:
        features = request.feature_dict
        for rule in self.bundle.ordered_rules():
            match = rule.match_dict()
            if all(features.get(key) == value for key, value in match.items()):
                return rule
        raise RouteError(
            "no routing rule matched request",
            f"request:{request.request_id}",
            request_id=request.request_id,
        )

    def _plans(self, requests: list[Request]) -> list[dict[str, Any]]:
        grouped: dict[str, list[tuple[int, Request]]] = {}
        for ordinal, request in enumerate(requests):
            rule = self._route(request)
            backend = self._backend_declarations[rule.target_backend]
            if len(request.inputs) != backend.parameter_length:
                raise InputError(
                    "request input length does not match backend parameter length",
                    f"$[{ordinal}].input",
                    request_id=request.request_id,
                    expected_length=backend.parameter_length,
                    actual_length=len(request.inputs),
                )
            grouped.setdefault(rule.rule_id, []).append((ordinal, request))
        plans: list[dict[str, Any]] = []
        for rule in self.bundle.ordered_rules():
            entries = grouped.get(rule.rule_id, [])
            for start in range(0, len(entries), rule.batch_size):
                chunk = entries[start : start + rule.batch_size]
                plans.append(
                    {
                        "backend_id": rule.target_backend,
                        "first_ordinal": chunk[0][0],
                        "priority": rule.priority,
                        "requests": chunk,
                        "rule_id": rule.rule_id,
                    }
                )
        plans.sort(
            key=lambda plan: (
                plan["priority"],
                plan["rule_id"],
                plan["first_ordinal"],
            )
        )
        return plans

    def execute_requests(
        self,
        requests: list[Request],
        *,
        execution_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> ExecutionReport:
        with self._lock:
            if not requests:
                raise InputError("request sequence is empty", "$")
            plans = self._plans(requests)
            if execution_hook is not None:
                self._execution_hook = execution_hook
                self._next_execution_hook = None
            else:
                self._execution_hook = self._next_execution_hook
                self._next_execution_hook = None
            self.state["replay_requests"] = [request.to_dict() for request in requests]
            self.state["stats_dirty"] = True
            attempt_map: dict[str, int] = {request.request_id: 0 for request in requests}
            trace: list[dict[str, Any]] = []
            batch_ids: list[str] = []
            old_effective = {
                entry["entry_id"]
                for entry in self.state.get("ledger", [])
                if entry.get("effective")
            }
            for plan in plans:
                batch = self._execute_plan(plan, attempt_map, trace, redispatch_of=None)
                batch_ids.append(batch["batch_id"])
            invalidated_after = sum(
                1
                for entry in self.state.get("ledger", [])
                if entry["entry_id"] in old_effective and not entry.get("effective")
            )
            current_stats = compute_stats(
                self.state.get("ledger", []), self.bundle, self._current_states
            )
            previous_stats = self.state.get("stats") or empty_stats(
                self.bundle, self._current_states
            )
            touched_batch_ids = sorted({str(item["batch_id"]) for item in trace})
            affected_backends = {
                batch["backend_id"]
                for batch in self.state["batches"]
                if batch["batch_id"] in touched_batch_ids
            }
            affected_rules = {
                batch["rule_id"]
                for batch in self.state["batches"]
                if batch["batch_id"] in touched_batch_ids
            }
            invalidated_rules = {
                entry["rule_id"]
                for entry in self.state.get("ledger", [])
                if entry["entry_id"] in old_effective and not entry.get("effective")
            }
            affected_rules |= invalidated_rules
            void_batch_ids = sorted(
                {
                    batch["batch_id"]
                    for batch in self.state.get("batches", [])
                    if batch["batch_id"] in touched_batch_ids and batch["status"] == "VOID"
                }
            )
            redispatched_batch_ids = sorted(
                {
                    batch["batch_id"]
                    for batch in self.state.get("batches", [])
                    if batch["batch_id"] in touched_batch_ids and batch.get("redispatch_of")
                }
            )
            stats_version, stats_meta = self._publish_stats(
                state=self.state,
                current_stats=current_stats,
                previous_stats=previous_stats,
                reason="REQUEST_SEQUENCE_COMPLETED",
                affected_rules=affected_rules,
                affected_backends=affected_backends,
                invalidated_entry_count=invalidated_after,
                void_batch_ids=void_batch_ids,
                redispatched_batch_ids=redispatched_batch_ids,
                extra={},
            )
            self.state["stats"] = current_stats
            self.state["stats_version"] = stats_version
            self.state["stats_dirty"] = False
            self.state["last_run_trace"] = sorted(
                trace,
                key=lambda item: (item["request_ordinal"], item["attempt_ordinal"]),
            )
            responses = self._responses_for_requests(requests, trace)
            self.state["last_run_summary"] = {
                "batch_ids": touched_batch_ids,
                "request_count": len(requests),
                "stats_version": stats_version,
                "trace_digest": sha256_json(self.state["last_run_trace"]),
                "void_batch_ids": void_batch_ids,
                "redispatched_batch_ids": redispatched_batch_ids,
            }
            self._persist()
            report = ExecutionReport(
                responses=responses,
                trace=self.state["last_run_trace"],
                batch_ids=touched_batch_ids,
                stats_version=stats_version,
                stats=current_stats,
                invalidated_entry_count=invalidated_after,
                void_batch_ids=void_batch_ids,
                redispatched_batch_ids=redispatched_batch_ids,
            )
            self._last_report = report
            self.logger.info(
                "REQUEST_SEQUENCE_COMPLETED",
                "deterministic request sequence executed",
                **{
                    **report.summary(),
                    "recalculated_stat_keys": stats_meta["recalculated_stat_keys"],
                    "unchanged_stat_keys": stats_meta["unchanged_stat_keys"],
                },
            )
            self._execution_hook = None
            return report

    def _responses_for_requests(
        self, requests: list[Request], trace: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        trace_by_ordinal: dict[int, list[dict[str, Any]]] = {}
        for item in trace:
            trace_by_ordinal.setdefault(int(item["request_ordinal"]), []).append(item)
        responses: list[dict[str, Any]] = []
        for ordinal, request in enumerate(requests):
            attempts = sorted(
                trace_by_ordinal.get(ordinal, []), key=lambda item: item["attempt_ordinal"]
            )
            if not attempts:
                responses.append(
                    {
                        "backend_id": None,
                        "batch_id": None,
                        "batch_status": "ERROR",
                        "bound_fingerprint": None,
                        "bound_load_sequence": None,
                        "duration_ms": None,
                        "error": {"code": "E_STATE", "message": "request has no execution attempt"},
                        "output_summary": None,
                        "protocol": "mlgw-gateway-v1",
                        "request_id": request.request_id,
                        "request_index": ordinal,
                        "rule_id": None,
                        "status": "E_STATE",
                        "void": True,
                    }
                )
                continue
            latest = attempts[-1]
            status = "OK" if latest.get("output_summary") and not latest.get("void") else latest.get("error_code", "E_UPSTREAM")
            response = {
                "backend_id": latest["backend_id"],
                "batch_id": latest["batch_id"],
                "batch_status": latest["batch_status"],
                "bound_fingerprint": latest["bound_fingerprint"],
                "bound_load_sequence": latest["bound_load_sequence"],
                "duration_ms": latest.get("duration_ms"),
                "output_summary": latest.get("output_summary"),
                "protocol": "mlgw-gateway-v1",
                "request_id": request.request_id,
                "request_index": ordinal,
                "rule_id": latest["rule_id"],
                "status": status,
                "void": bool(latest.get("void")),
            }
            if status != "OK":
                response["error"] = latest.get("error", {"code": status})
            responses.append(response)
        return responses

    def _execute_plan(
        self,
        plan: dict[str, Any],
        attempt_map: dict[str, int],
        trace: list[dict[str, Any]],
        *,
        redispatch_of: str | None,
    ) -> dict[str, Any]:
        backend_id = str(plan["backend_id"])
        rule_id = str(plan["rule_id"])
        bound_state = self._current_states[backend_id]
        batch_id, batch_seq = self._next_batch_id()
        batch_requests = [
            (int(ordinal), request) for ordinal, request in plan["requests"]
        ]
        request_documents = [request.to_dict() for _, request in batch_requests]
        batch = {
            "batch_id": batch_id,
            "batch_seq": batch_seq,
            "bound_fingerprint": bound_state.fingerprint,
            "bound_load_sequence": bound_state.load_sequence,
            "backend_id": backend_id,
            "created_epoch": int(self.state["event_epoch"]),
            "duration_ms": None,
            "error_code": None,
            "error_message": None,
            "output_summary": None,
            "redispatch_of": redispatch_of,
            "request_ids": [request.request_id for _, request in batch_requests],
            "requests": request_documents,
            "rule_id": rule_id,
            "status": "IN_FLIGHT",
            "superseded_by": None,
            "view_version": int(self.state["view_version"]),
        }
        self.state["batches"].append(batch)
        pending_entries: list[dict[str, Any]] = []
        for ordinal, request in batch_requests:
            entry_id, entry_seq = self._next_entry_id()
            entry = {
                "attempt_ordinal": int(attempt_map[request.request_id]),
                "batch_id": batch_id,
                "backend_id": backend_id,
                "bound_fingerprint": bound_state.fingerprint,
                "bound_load_sequence": bound_state.load_sequence,
                "committed": 0,
                "effective": 0,
                "entry_id": entry_id,
                "entry_seq": entry_seq,
                "invalid_reason": None,
                "invalidated_by_fingerprint": None,
                "output_summary": None,
                "redispatch_of_entry_id": None,
                "request_id": request.request_id,
                "request_ordinal": ordinal,
                "rule_id": rule_id,
                "view_version": int(self.state["view_version"]),
                "void": 0,
            }
            self.state["ledger"].append(entry)
            pending_entries.append(entry)
        self._persist()

        result: dict[str, Any] | None = None
        error: GatewayError | None = None
        try:
            result = self.client.call_batch(
                state=bound_state,
                batch_id=batch_id,
                inputs=[list(request.inputs) for _, request in batch_requests],
            )
        except FingerprintMismatch as exc:
            error = exc
        except UpstreamError as exc:
            error = exc
        except Exception as exc:
            error = UpstreamError(
                "unexpected backend client failure",
                backend_id=backend_id,
                exception=type(exc).__name__,
                message=str(exc),
            )

        if self._execution_hook is not None:
            hook = self._execution_hook
            self._execution_hook = None
            hook(
                {
                    "backend_id": backend_id,
                    "batch_id": batch_id,
                    "bound_fingerprint": bound_state.fingerprint,
                    "error": None if error is None else error.to_dict(),
                    "result": result,
                }
            )

        current_state = self._current_states.get(backend_id)
        fingerprint_changed = (
            current_state is None
            or current_state.fingerprint != batch["bound_fingerprint"]
            or batch.get("status") == "VOID"
        )
        if fingerprint_changed:
            batch["status"] = "VOID"
            batch["error_code"] = "E_FINGERPRINT_CHANGED"
            batch["error_message"] = "backend fingerprint changed while the batch was in flight"
            batch["output_summary"] = None
            batch["duration_ms"] = None
            for entry in pending_entries:
                entry["void"] = 1
                entry["effective"] = 0
                entry["invalid_reason"] = "IN_FLIGHT_FINGERPRINT_CHANGED"
                entry["invalidated_by_fingerprint"] = (
                    current_state.fingerprint if current_state is not None else None
                )
            self._append_trace(
                trace,
                batch_requests,
                batch,
                void=True,
                output_summary=None,
                duration_ms=None,
                error_code="E_FINGERPRINT_CHANGED",
            )
            should_redispatch = (
                self.redispatch_policy == "always"
                and redispatch_of is None
                and self.max_redispatch >= 1
                and current_state is not None
            )
            if should_redispatch:
                for _, request in batch_requests:
                    attempt_map[request.request_id] += 1
                replacement = self._execute_plan(
                    plan,
                    attempt_map,
                    trace,
                    redispatch_of=batch_id,
                )
                batch["superseded_by"] = replacement["batch_id"]
                self.state["event_epoch"] = int(self.state["event_epoch"]) + 1
                self._persist()
                return replacement
            self.state["event_epoch"] = int(self.state["event_epoch"]) + 1
            self._persist()
            return batch

        if error is not None:
            batch["status"] = "ERROR"
            batch["error_code"] = error.code
            batch["error_message"] = error.message
            for entry in pending_entries:
                entry["void"] = 1
                entry["effective"] = 0
                entry["invalid_reason"] = "UPSTREAM_UNAVAILABLE"
            self._append_trace(
                trace,
                batch_requests,
                batch,
                void=True,
                output_summary=None,
                duration_ms=None,
                error_code=error.code,
                error=error.to_dict(),
            )
            self.state["event_epoch"] = int(self.state["event_epoch"]) + 1
            self._persist()
            self.review_bindings()
            return batch

        if result is None:
            raise StateError("backend call returned neither a result nor an error")
        batch["status"] = "COMPLETED"
        batch["output_summary"] = str(result["output_summary"])
        batch["duration_ms"] = int(result["duration_ms"])
        batch_request_ids = {request.request_id for _, request in batch_requests}
        for entry in self.state.get("ledger", []):
            if (
                entry.get("effective")
                and entry["request_id"] in batch_request_ids
                and entry["batch_id"] != batch_id
            ):
                entry["effective"] = 0
                entry["invalid_reason"] = "SUPERSEDED_BY_RERUN"
                entry["invalidated_by_fingerprint"] = bound_state.fingerprint
        for entry in pending_entries:
            entry["committed"] = 1
            entry["effective"] = 1
            entry["output_summary"] = batch["output_summary"]
        self._append_trace(
            trace,
            batch_requests,
            batch,
            void=False,
            output_summary=batch["output_summary"],
            duration_ms=batch["duration_ms"],
            error_code=None,
        )
        self.state["event_epoch"] = int(self.state["event_epoch"]) + 1
        self._persist()
        return batch

    def _append_trace(
        self,
        trace: list[dict[str, Any]],
        batch_requests: Iterable[tuple[int, Request]],
        batch: dict[str, Any],
        *,
        void: bool,
        output_summary: str | None,
        duration_ms: int | None,
        error_code: str | None,
        error: dict[str, Any] | None = None,
    ) -> None:
        entries_by_request = {
            entry["request_id"]: entry
            for entry in self.state["ledger"]
            if entry["batch_id"] == batch["batch_id"]
        }
        for ordinal, request in batch_requests:
            entry = entries_by_request[request.request_id]
            record = {
                "attempt_ordinal": int(entry["attempt_ordinal"]),
                "backend_id": batch["backend_id"],
                "batch_id": batch["batch_id"],
                "batch_status": batch["status"],
                "bound_fingerprint": batch["bound_fingerprint"],
                "bound_load_sequence": batch["bound_load_sequence"],
                "duration_ms": duration_ms,
                "error_code": error_code,
                "output_summary": output_summary,
                "redispatch_of": batch.get("redispatch_of"),
                "request_id": request.request_id,
                "request_ordinal": int(ordinal),
                "rule_id": batch["rule_id"],
                "void": bool(void),
            }
            if error is not None:
                record["error"] = error
            trace.append(record)

    def _publish_stats(
        self,
        *,
        state: dict[str, Any],
        current_stats: dict[str, Any],
        previous_stats: dict[str, Any],
        reason: str,
        affected_rules: Iterable[str],
        affected_backends: Iterable[str],
        invalidated_entry_count: int,
        void_batch_ids: Iterable[str],
        redispatched_batch_ids: Iterable[str],
        extra: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        stats_version = self.store.next_stats_version()
        previous_version = int(state.get("stats_version", 0)) or None
        diff = diff_stats(
            previous_stats,
            current_stats,
            sorted(set(affected_rules)),
            sorted(set(affected_backends)),
        )
        metadata = {
            "affected_backends": sorted(set(affected_backends)),
            "affected_rules": sorted(set(affected_rules)),
            "changed_stat_keys": diff["changed_keys"],
            "invalidated_entry_count": int(invalidated_entry_count),
            "previous_version": previous_version,
            "reason": reason,
            "recalculated_stat_keys": diff["recalculated_keys"],
            "redispatched_batch_ids": sorted(set(redispatched_batch_ids)),
            "unchanged_stat_keys": diff["unchanged_keys"],
            "unexpected_changed_stat_keys": diff["unexpected_changed_keys"],
            "void_batch_ids": sorted(set(void_batch_ids)),
            **extra,
        }
        if diff["unexpected_changed_keys"]:
            raise StateError(
                "unaffected aggregate values changed",
                keys=diff["unexpected_changed_keys"],
            )
        self.store.save_stats_version(
            stats_version=stats_version,
            stats=current_stats,
            metadata=metadata,
            view_version=int(state["view_version"]),
            logical_epoch=int(state["event_epoch"]),
            reason=reason,
            previous_version=previous_version,
            readonly=False,
        )
        return stats_version, metadata

    def reload_backend(
        self,
        *,
        backend_id: str,
        declaration: BackendDecl,
    ) -> dict[str, Any]:
        """Explicitly reload one backend; used for controlled in-flight drills."""

        with self._lock:
            old = self._current_states.get(backend_id)
            if old is None:
                raise StateError("cannot reload an unknown backend", backend_id=backend_id)
            if declaration.backend_id != backend_id:
                raise StateError("reload declaration backend_id does not match")
            new_sequence = old.load_sequence + 1
            fingerprint, config_digest, content_signature = derive_fingerprint(
                declaration, new_sequence
            )
            new_state = BackendState(
                backend_id=declaration.backend_id,
                model_id=declaration.model_id,
                host=declaration.host,
                port=declaration.port,
                max_concurrency=declaration.max_concurrency,
                parameter_length=declaration.parameter_length,
                load_sequence=new_sequence,
                content_signature=content_signature,
                config_digest=config_digest,
                fingerprint=fingerprint,
                canonical_vector=declaration.canonical_vector(),
            )
            had_inflight = any(
                batch.get("backend_id") == backend_id
                and batch.get("status") == "IN_FLIGHT"
                for batch in self.state.get("batches", [])
            )
            self._current_states[backend_id] = new_state
            self._backend_declarations[backend_id] = declaration
            voided_batches: list[str] = []
            invalidated = 0
            for entry in self.state.get("ledger", []):
                if (
                    entry.get("effective")
                    and entry.get("backend_id") == backend_id
                    and entry.get("bound_fingerprint") != fingerprint
                ):
                    entry["effective"] = 0
                    entry["invalid_reason"] = "FINGERPRINT_CHANGED"
                    entry["invalidated_by_fingerprint"] = fingerprint
                    invalidated += 1
            for batch in self.state.get("batches", []):
                if (
                    batch.get("backend_id") == backend_id
                    and batch.get("bound_fingerprint") != fingerprint
                    and batch.get("status") == "IN_FLIGHT"
                ):
                    batch["status"] = "VOID"
                    batch["error_code"] = "E_FINGERPRINT_CHANGED"
                    batch["error_message"] = "backend reloaded while batch was in flight"
                    voided_batches.append(batch["batch_id"])
                    for entry in self.state.get("ledger", []):
                        if entry.get("batch_id") == batch["batch_id"]:
                            entry["void"] = 1
                            entry["effective"] = 0
                            entry["invalid_reason"] = "IN_FLIGHT_FINGERPRINT_CHANGED"
                            entry["invalidated_by_fingerprint"] = fingerprint
            self.state["backend_states"] = self._serialize_states(self._current_states)
            self.state["view_version"] = int(self.state["view_version"]) + 1
            self.state["event_epoch"] = int(self.state["event_epoch"]) + 1
            self.state["stats_dirty"] = True
            stats_version = int(self.state.get("stats_version", 0))
            if not had_inflight:
                previous_stats = self.state.get("stats") or empty_stats(
                    self.bundle, self._current_states
                )
                current_stats = compute_stats(
                    self.state.get("ledger", []), self.bundle, self._current_states
                )
                affected_rules = {
                    rule.rule_id
                    for rule in self.bundle.rules
                    if rule.target_backend == backend_id
                }
                stats_version, _ = self._publish_stats(
                    state=self.state,
                    current_stats=current_stats,
                    previous_stats=previous_stats,
                    reason="BACKEND_RELOAD_STATS_RECOMPUTED",
                    affected_rules=affected_rules,
                    affected_backends={backend_id},
                    invalidated_entry_count=invalidated,
                    void_batch_ids=voided_batches,
                    redispatched_batch_ids=[],
                    extra={"backend_id": backend_id, "load_sequence": new_sequence},
                )
                self.state["stats"] = current_stats
                self.state["stats_version"] = stats_version
                self.state["stats_dirty"] = False
            self._persist()
            summary = {
                "backend_id": backend_id,
                "fingerprint": fingerprint,
                "invalidated_entry_count": invalidated,
                "load_sequence": new_sequence,
                "stats_version": stats_version,
                "void_batch_ids": sorted(voided_batches),
            }
            self.logger.info(
                "BACKEND_RELOADED",
                "backend state reloaded and in-flight batches voided",
                **summary,
            )
            return summary

    def save_snapshot(self, label: str) -> dict[str, Any]:
        with self._lock:
            latest_by_request: dict[int, dict[str, Any]] = {}
            for item in self.state.get("last_run_trace", []):
                ordinal = int(item["request_ordinal"])
                current = latest_by_request.get(ordinal)
                if current is None or int(item["attempt_ordinal"]) >= int(
                    current["attempt_ordinal"]
                ):
                    latest_by_request[ordinal] = item
            replay_expected = [
                copy.deepcopy(latest_by_request[ordinal])
                for ordinal in sorted(latest_by_request)
            ]
            snapshot = self.store.save_snapshot(
                state=self.state,
                label=label,
                replay_requests=copy.deepcopy(self.state.get("replay_requests", [])),
                replay_expected=replay_expected,
            )
            self.logger.info(
                "SNAPSHOT_SAVED",
                "session snapshot persisted",
                snapshot_id=snapshot["snapshot_id"],
                stats_version=snapshot["stats_version"],
                state_digest=snapshot["state_digest"],
            )
            return snapshot

    def set_next_execution_hook(
        self, hook: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """Install a one-shot diagnostic hook for the next request sequence."""

        with self._lock:
            self._next_execution_hook = hook

    def restore_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self._lock:
            snapshot = self.store.get_snapshot(snapshot_id)
            current_fingerprints = fingerprint_map(self._current_states)
            snapshot_fingerprints = dict(sorted(snapshot["backend_fingerprints"].items()))
            state_fingerprints = {
                str(raw["backend_id"]): str(raw["fingerprint"])
                for raw in snapshot["state"].get("backend_states", [])
            }
            if (
                state_fingerprints != snapshot_fingerprints
                or snapshot["state"].get("rule_version") != snapshot["rule_version"]
            ):
                raise SnapshotError(
                    "snapshot metadata does not match its embedded state",
                    snapshot_id=snapshot_id,
                    metadata_fingerprints=snapshot_fingerprints,
                    state_fingerprints=state_fingerprints,
                )
            if (
                current_fingerprints != snapshot_fingerprints
                or snapshot["rule_version"] != self.bundle.rule_version
            ):
                reset = self._reset_current_view_after_conflict(
                    snapshot_id=snapshot_id,
                    current_fingerprints=current_fingerprints,
                    snapshot_fingerprints=snapshot_fingerprints,
                    snapshot_rule_version=snapshot["rule_version"],
                )
                raise SnapshotError(
                    "snapshot fingerprint set does not match current declarations; current view was fully replaced",
                    snapshot_id=snapshot_id,
                    **{
                        "current_fingerprints": current_fingerprints,
                        "snapshot_fingerprints": snapshot_fingerprints,
                        "fingerprint_diff": {
                            "added": sorted(set(current_fingerprints) - set(snapshot_fingerprints)),
                            "removed": sorted(set(snapshot_fingerprints) - set(current_fingerprints)),
                            "changed": sorted(
                                key
                                for key in set(current_fingerprints) & set(snapshot_fingerprints)
                                if current_fingerprints[key] != snapshot_fingerprints[key]
                            ),
                        },
                        "reset": reset,
                        "rule_version_match": snapshot["rule_version"] == self.bundle.rule_version,
                    },
                )
            integrity_digest = sha256_json(snapshot["state"])
            if integrity_digest != snapshot["state_digest"]:
                raise SnapshotError(
                    "snapshot state digest verification failed",
                    snapshot_id=snapshot_id,
                    expected=snapshot["state_digest"],
                    actual=integrity_digest,
                )
            self.store.replace_current_view(snapshot["state"])
            if int(snapshot["state"]["stats_version"]) > 0:
                self.store.mark_stats_current(int(snapshot["state"]["stats_version"]))
            self.state = snapshot["state"]
            self._current_states = self._states_from_state(self.state)
            summary = {
                "ledger_count": len(self.state.get("ledger", [])),
                "snapshot_id": snapshot_id,
                "stats_version": int(self.state["stats_version"]),
                "view_version": int(self.state["view_version"]),
            }
            self.logger.info(
                "SNAPSHOT_RESTORED",
                "snapshot replaced the current ledger and statistics view",
                **summary,
            )
            return snapshot

    def _reset_current_view_after_conflict(
        self,
        *,
        snapshot_id: str,
        current_fingerprints: dict[str, str],
        snapshot_fingerprints: dict[str, str],
        snapshot_rule_version: str,
    ) -> dict[str, Any]:
        old_stats = self.state.get("stats") or empty_stats(
            self.bundle, self._current_states
        )
        old_stats_version = int(self.state.get("stats_version", 0))
        state = self._empty_state(self._current_states)
        state["view_version"] = int(self.state.get("view_version", 0)) + 1
        state["event_epoch"] = int(self.state.get("event_epoch", 0)) + 1
        state["last_snapshot_conflict"] = {
            "current_fingerprints": current_fingerprints,
            "snapshot_fingerprints": snapshot_fingerprints,
            "snapshot_id": snapshot_id,
            "snapshot_rule_version": snapshot_rule_version,
        }
        current_stats = empty_stats(self.bundle, self._current_states)
        affected_rules = {rule.rule_id for rule in self.bundle.rules}
        affected_backends = set(self._current_states)
        stats_version, _ = self._publish_stats(
            state=state,
            current_stats=current_stats,
            previous_stats=old_stats,
            reason="SNAPSHOT_FINGERPRINT_CONFLICT_FULL_REPLACE",
            affected_rules=affected_rules,
            affected_backends=affected_backends,
            invalidated_entry_count=sum(
                1 for entry in self.state.get("ledger", []) if entry.get("effective")
            ),
            void_batch_ids=[],
            redispatched_batch_ids=[],
            extra={"snapshot_id": snapshot_id},
        )
        state["stats"] = current_stats
        state["stats_version"] = stats_version
        state["last_evolution"] = {
            "reason": "SNAPSHOT_FINGERPRINT_CONFLICT_FULL_REPLACE",
            "snapshot_id": snapshot_id,
            "stats_version": stats_version,
        }
        self.state = state
        self.store.replace_current_view(state)
        summary = {
            "previous_stats_version": old_stats_version or None,
            "replaced_with_stats_version": stats_version,
            "snapshot_id": snapshot_id,
        }
        self.logger.warning(
            "SNAPSHOT_FINGERPRINT_CONFLICT",
            "snapshot conflict caused a whole current-view replacement",
            **summary,
        )
        return summary

    def replay_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self._lock:
            snapshot = self.store.get_snapshot(snapshot_id)
            if self.store.current_digest() != snapshot["state_digest"]:
                raise ReplayError(
                    "current state is not the restored snapshot state",
                    field="state_digest",
                    expected=snapshot["state_digest"],
                    actual=self.store.current_digest(),
                )
            requests = [
                Request(
                    request_id=str(raw["request_id"]),
                    features=tuple(
                        sorted(
                            (str(key), value)
                            for key, value in dict(raw["features"]).items()
                        )
                    ),
                    inputs=tuple(float(value) for value in raw["input"]),
                )
                for raw in snapshot["replay_requests"]
            ]
            expected = sorted(
                snapshot["replay_expected"],
                key=lambda item: (item["request_ordinal"], item["attempt_ordinal"]),
            )
            actual = self._transient_trace(requests, snapshot["state"])
            comparison = self._compare_replay(expected, actual)
            summary = {
                "actual_trace_digest": sha256_json(actual),
                "expected_trace_digest": sha256_json(expected),
                "matched": comparison is None,
                "request_count": len(requests),
                "snapshot_id": snapshot_id,
            }
            if comparison is not None:
                self.logger.error(
                    "E_REPLAY", "snapshot replay mismatch", **comparison
                )
                raise ReplayError(
                    "snapshot replay differs from recorded execution",
                    **comparison,
                )
            self.logger.info(
                "REPLAY_VERIFIED", "snapshot replay matched recorded ledger", **summary
            )
            return {**summary, "trace": actual}

    def _transient_trace(
        self, requests: list[Request], snapshot_state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        states = self._states_from_state(snapshot_state)
        plans = self._plans(requests)
        trace: list[dict[str, Any]] = []
        batch_number = 1
        for plan in plans:
            backend_id = plan["backend_id"]
            state = states[backend_id]
            batch_id = f"replay-b-{batch_number:06d}"
            batch_number += 1
            error: GatewayError | None = None
            result: dict[str, Any] | None = None
            try:
                result = self.client.call_batch(
                    state=state,
                    batch_id=batch_id,
                    inputs=[list(request.inputs) for _, request in plan["requests"]],
                )
            except GatewayError as exc:
                error = exc
            except Exception as exc:
                error = UpstreamError(
                    "replay backend call failed",
                    backend_id=backend_id,
                    exception=type(exc).__name__,
                    message=str(exc),
                )
            for ordinal, request in plan["requests"]:
                trace.append(
                    {
                        "attempt_ordinal": 0,
                        "backend_id": backend_id,
                        "batch_id": batch_id,
                        "batch_status": "ERROR" if error else "COMPLETED",
                        "bound_fingerprint": state.fingerprint,
                        "bound_load_sequence": state.load_sequence,
                        "duration_ms": None if error else int(result["duration_ms"]),
                        "error_code": None if error is None else error.code,
                        "output_summary": None if error else str(result["output_summary"]),
                        "request_id": request.request_id,
                        "request_ordinal": int(ordinal),
                        "rule_id": plan["rule_id"],
                        "void": bool(error),
                    }
                )
        return sorted(
            trace, key=lambda item: (item["request_ordinal"], item["attempt_ordinal"])
        )

    def _compare_replay(
        self, expected: list[dict[str, Any]], actual: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        expected_groups: dict[int, list[dict[str, Any]]] = {}
        actual_groups: dict[int, list[dict[str, Any]]] = {}
        for item in expected:
            expected_groups.setdefault(int(item["request_ordinal"]), []).append(item)
        for item in actual:
            actual_groups.setdefault(int(item["request_ordinal"]), []).append(item)
        ordinals = sorted(set(expected_groups) | set(actual_groups))
        fields = (
            "bound_fingerprint",
            "rule_id",
            "backend_id",
            "void",
            "output_summary",
            "error_code",
        )
        for ordinal in ordinals:
            expected_attempts = expected_groups.get(ordinal, [])
            actual_attempts = actual_groups.get(ordinal, [])
            request_id = (
                expected_attempts[0]["request_id"]
                if expected_attempts
                else actual_attempts[0]["request_id"]
            )
            if len(expected_attempts) != len(actual_attempts):
                return {
                    "actual": len(actual_attempts),
                    "field": "attempt_count",
                    "expected": len(expected_attempts),
                    "request_id": request_id,
                    "request_index": ordinal,
                    "request_ordinal": ordinal + 1,
                }
            for attempt_index, (want, got) in enumerate(
                zip(expected_attempts, actual_attempts)
            ):
                for field in fields:
                    if want.get(field) != got.get(field):
                        return {
                            "actual": got.get(field),
                            "attempt_ordinal": int(want.get("attempt_ordinal", attempt_index)),
                            "expected": want.get(field),
                            "field": field,
                            "request_id": request_id,
                            "request_index": ordinal,
                            "request_ordinal": ordinal + 1,
                        }
        return None

    def binding_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows: list[dict[str, Any]] = []
            for entry in sorted(
                self.state.get("ledger", []),
                key=lambda item: (
                    item["request_ordinal"],
                    item["attempt_ordinal"],
                    item["entry_id"],
                ),
            ):
                status = self._binding_status_against(entry, self._current_states)
                rows.append(
                    {
                        "backend_id": entry["backend_id"],
                        "batch_id": entry["batch_id"],
                        "binding_reason": status["reason"],
                        "binding_valid": status["valid"],
                        "bound_fingerprint": entry["bound_fingerprint"],
                        "effective": bool(entry.get("effective")),
                        "entry_id": entry["entry_id"],
                        "invalid_reason": entry.get("invalid_reason"),
                        "output_summary": entry.get("output_summary"),
                        "request_id": entry["request_id"],
                        "request_index": int(entry["request_ordinal"]),
                        "rule_id": entry["rule_id"],
                        "void": bool(entry.get("void")),
                    }
                )
            return rows

    @property
    def last_report(self) -> ExecutionReport | None:
        return self._last_report
