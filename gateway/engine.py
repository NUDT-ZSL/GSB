"""Pure deterministic ledger and dispatch arbitration engine.

The engine never reads the system clock and never opens a database.  Given an
initial state and the same ordered commands, it produces byte-identical state.
"""

from __future__ import annotations

import copy
from decimal import Decimal, ROUND_CEILING
from typing import Any

from .canonical import canonical_json, sha256_obj, sha256_text
from .errors import GatewayError
from .fixedpoint import (
    charge_token_ledger,
    format_fixed,
    new_token_ledger,
    refill_token_ledger,
    token_view,
)

OUTCOME_VALUES = {"success", "failure", "rejected", "expired", "timeout", "abort"}
TERMINAL_OUTCOME = {
    "success": "succeeded",
    "failure": "failed",
    "rejected": "rejected",
    "expired": "expired",
    "timeout": "expired",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(value: Any, field: str, minimum: int | None = None) -> int:
    if not _is_int(value):
        raise GatewayError("E_INVALID_COMMAND", f"{field} must be an integer", field=field)
    if minimum is not None and value < minimum:
        raise GatewayError(
            "E_INVALID_COMMAND",
            f"{field} must be >= {minimum}",
            field=field,
            value=value,
        )
    return value


def _backoff_delay(
    config: dict[str, Any],
    request_id: int,
    generation: int,
    replica_id: str,
    clock_ms: int,
) -> tuple[int, int, int]:
    base = int(config["backoff"]["base_ms"])
    cap = int(config["backoff"]["cap_ms"])
    shift = min(max(generation - 1, 0), 62)
    exponential = min(cap, base << shift)
    seed = f"{request_id}:{generation}:{replica_id}:{clock_ms}"
    jitter = int(sha256_text(seed)[:16], 16) % base
    delay = min(cap, exponential + jitter)
    return delay, exponential, jitter


def _dispatch_cost_ms(service: dict[str, Any], replica: dict[str, Any]) -> int:
    batch = Decimal(int(service["batch_window_ms"]))
    compute = Decimal(str(replica["compute_units"]))
    value = (batch / compute).to_integral_value(rounding=ROUND_CEILING)
    return max(1, int(value))


def build_genesis(inputs: dict[str, Any]) -> dict[str, Any]:
    """Build the initial immutable ledger from validated input."""

    config = copy.deepcopy(inputs["config"])
    services_doc = copy.deepcopy(inputs["services"])
    requests_doc = copy.deepcopy(inputs["requests"])
    fingerprint = inputs.get("fingerprint")
    if fingerprint is None:
        fingerprint = sha256_obj(
            {
                "config": config,
                "services": services_doc,
                "requests": requests_doc,
            }
        )
    scale = int(config["fixed_point"]["scale"])
    service_ledgers: dict[str, Any] = {}
    for service in services_doc["services"]:
        override = config["service_overrides"].get(service["name"])
        settings = override if override is not None else config["service"]
        service_ledgers[service["name"]] = {
            "model_version": service["model_version"],
            "batch_window_ms": service["batch_window_ms"],
            "replicas": service["replicas"],
            "max_inflight": int(settings["max_inflight"]),
            "capacity": int(settings["capacity"]),
            "refill_rate_per_second": int(settings["refill_rate_per_second"]),
            "token": new_token_ledger(
                int(settings["capacity"]),
                int(settings["refill_rate_per_second"]),
                scale,
                0,
            ),
        }

    requests: dict[str, Any] = {}
    for request in requests_doc["requests"]:
        request_id = int(request["request_id"])
        service = service_ledgers[request["service"]]
        requests[str(request_id)] = {
            "request_id": request_id,
            "service": request["service"],
            "arrival_ms": int(request["arrival_ms"]),
            "eligible_at_ms": int(request["arrival_ms"])
            + int(service["batch_window_ms"]),
            "input_tokens": int(request["input_tokens"]),
            "output_tokens": int(request["output_tokens"]),
            "priority": int(request["priority"]),
            "generation": 0,
            "status": "queued",
            "next_eligible_ms": int(request["arrival_ms"])
            + int(service["batch_window_ms"]),
            "confirmed": False,
            "confirmed_at_ms": None,
            "dead_letter": None,
            "attempts": [],
            "last_transition_ms": None,
        }

    return {
        "schema_version": 1,
        "db_id": fingerprint,
        "config_version": int(config["config_version"]),
        "config": config,
        "services": service_ledgers,
        "requests": requests,
        "global_token": new_token_ledger(
            int(config["global"]["capacity"]),
            int(config["global"]["refill_rate_per_second"]),
            scale,
            0,
        ),
        "queue_version": 1,
        "clock_watermark_ms": 0,
        "rounds": {},
        "tombstones": {},
        "counters": {
            "rounds": 0,
            "dispatches": 0,
            "successes": 0,
            "failures": 0,
            "rejections": 0,
            "expirations": 0,
            "abandonments": 0,
            "dead_letters": 0,
        },
    }


class Engine:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    # ------------------------------------------------------------------
    # Common helpers
    # ------------------------------------------------------------------
    @property
    def config(self) -> dict[str, Any]:
        return self.state["config"]

    @property
    def scale(self) -> int:
        return int(self.config["fixed_point"]["scale"])

    def _advance_clock(self, requested_clock_ms: int) -> int:
        requested = _require_int(requested_clock_ms, "clock_ms", 0)
        watermark = int(self.state["clock_watermark_ms"])
        tolerance = int(self.config["fixed_point"]["tolerance_ms"])
        if requested < watermark - tolerance:
            raise GatewayError(
                "E_CLOCK_REGRESSION",
                "logical clock moved backwards beyond configured tolerance",
                requested_clock_ms=requested,
                clock_watermark_ms=watermark,
                tolerance_ms=tolerance,
            )
        effective = max(requested, watermark)
        self.state["clock_watermark_ms"] = effective
        return effective

    def _refill_all(self, clock_ms: int) -> None:
        refill_token_ledger(self.state["global_token"], clock_ms)
        for service_name in sorted(self.state["services"]):
            refill_token_ledger(
                self.state["services"][service_name]["token"], clock_ms
            )

    def _active_attempts(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        result: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for request_id in sorted(self.state["requests"], key=int):
            request = self.state["requests"][request_id]
            for attempt in request["attempts"]:
                if attempt["status"] == "inflight":
                    result.append((request, attempt))
        return result

    def _service_inflight(self, service_name: str) -> int:
        count = 0
        for request in self.state["requests"].values():
            if request["service"] != service_name:
                continue
            if any(attempt["status"] == "inflight" for attempt in request["attempts"]):
                count += 1
        return count

    def _global_inflight(self) -> int:
        return len(self._active_attempts())

    def _replica_usage(self, service_name: str) -> dict[str, int]:
        usage: dict[str, int] = {
            replica["replica_id"]: 0
            for replica in self.state["services"][service_name]["replicas"]
        }
        for request in self.state["requests"].values():
            if request["service"] != service_name:
                continue
            for attempt in request["attempts"]:
                if attempt["status"] == "inflight":
                    usage[attempt["replica_id"]] = usage.get(attempt["replica_id"], 0) + 1
        return usage

    # ------------------------------------------------------------------
    # Outcome handling
    # ------------------------------------------------------------------
    def _normalise_outcome(self, raw: Any, index: int, effective_clock: int) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise GatewayError(
                "E_INVALID_COMMAND",
                f"outcomes[{index}] must be an object",
                field=f"outcomes[{index}]",
            )
        request_id = _require_int(
            raw.get("request_id"), f"outcomes[{index}].request_id", 1
        )
        generation = _require_int(
            raw.get("generation"), f"outcomes[{index}].generation", 1
        )
        replica_id = raw.get("replica_id")
        if not isinstance(replica_id, str) or not replica_id:
            raise GatewayError(
                "E_INVALID_COMMAND",
                f"outcomes[{index}].replica_id must be a non-empty string",
                field=f"outcomes[{index}].replica_id",
                request_id=request_id,
                generation=generation,
            )
        outcome = raw.get("outcome")
        if outcome not in OUTCOME_VALUES:
            raise GatewayError(
                "E_INVALID_COMMAND",
                f"outcomes[{index}].outcome must be one of {sorted(OUTCOME_VALUES)}",
                field=f"outcomes[{index}].outcome",
                request_id=request_id,
                generation=generation,
            )
        outcome_clock = raw.get("clock_ms", effective_clock)
        outcome_clock = _require_int(
            outcome_clock, f"outcomes[{index}].clock_ms", 0
        )
        if outcome_clock != effective_clock:
            raise GatewayError(
                "E_INVALID_COMMAND",
                "outcome clock_ms must equal the enclosing logical step clock",
                field=f"outcomes[{index}].clock_ms",
                request_id=request_id,
                generation=generation,
                expected_clock_ms=effective_clock,
                outcome_clock_ms=outcome_clock,
            )
        reason = raw.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise GatewayError(
                "E_INVALID_COMMAND",
                f"outcomes[{index}].reason must be a string",
                request_id=request_id,
                generation=generation,
            )
        return {
            "request_id": request_id,
            "generation": generation,
            "replica_id": replica_id,
            "outcome": outcome,
            "clock_ms": outcome_clock,
            "reason": reason,
        }

    def _apply_outcome(self, outcome: dict[str, Any], clock_ms: int) -> dict[str, Any]:
        request_id = int(outcome["request_id"])
        generation = int(outcome["generation"])
        replica_id = outcome["replica_id"]
        requested_outcome = outcome["outcome"]
        request = self.state["requests"].get(str(request_id))
        if request is None:
            raise GatewayError(
                "E_CONFIRM_NOT_FOUND",
                f"confirmation references unknown request {request_id}",
                request_id=request_id,
                service=None,
                replica_id=replica_id,
                generation=generation,
            )
        attempt = next(
            (
                item
                for item in request["attempts"]
                if int(item["generation"]) == generation
            ),
            None,
        )
        if attempt is None:
            raise GatewayError(
                "E_CONFIRM_NOT_FOUND",
                f"confirmation references unknown dispatch generation {generation}",
                request_id=request_id,
                service=request["service"],
                replica_id=replica_id,
                generation=generation,
            )
        if attempt["replica_id"] != replica_id:
            raise GatewayError(
                "E_CONFIRM_REPLICA_MISMATCH",
                "confirmation target does not match the dispatched replica",
                request_id=request_id,
                service=request["service"],
                replica_id=replica_id,
                generation=generation,
                dispatched_replica_id=attempt["replica_id"],
            )
        if clock_ms < int(attempt["dispatch_ms"]):
            raise GatewayError(
                "E_CONFIRM_CLOCK_INVALID",
                "confirmation clock precedes dispatch clock",
                request_id=request_id,
                service=request["service"],
                replica_id=replica_id,
                generation=generation,
                dispatch_ms=attempt["dispatch_ms"],
                confirm_ms=clock_ms,
            )

        if requested_outcome in {"expired", "timeout"} and clock_ms <= int(
            attempt["deadline_ms"]
        ):
            raise GatewayError(
                "E_TIMEOUT_NOT_DUE",
                "timeout requires logical clock progress beyond the deadline and an explicit abort",
                request_id=request_id,
                service=request["service"],
                replica_id=replica_id,
                generation=generation,
                deadline_ms=attempt["deadline_ms"],
                confirm_ms=clock_ms,
            )

        if requested_outcome == "abort":
            terminal = "expired" if clock_ms > int(attempt["deadline_ms"]) else "abandoned"
            trigger = "expired" if terminal == "expired" else "abandoned"
        else:
            terminal = TERMINAL_OUTCOME[requested_outcome]
            trigger = {
                "succeeded": "success",
                "failed": "failure",
                "rejected": "rejected",
                "expired": "expired",
            }[terminal]

        if attempt["status"] != "inflight":
            if attempt["status"] == terminal:
                return {
                    "request_id": request_id,
                    "generation": generation,
                    "replica_id": replica_id,
                    "outcome": requested_outcome,
                    "status": "idempotent",
                    "terminal_attempt_status": attempt["status"],
                }
            return {
                "request_id": request_id,
                "generation": generation,
                "replica_id": replica_id,
                "outcome": requested_outcome,
                "status": "ignored_late",
                "terminal_attempt_status": attempt["status"],
            }
        if request["confirmed"]:
            return {
                "request_id": request_id,
                "generation": generation,
                "replica_id": replica_id,
                "outcome": requested_outcome,
                "status": "ignored_late",
                "terminal_attempt_status": "succeeded",
            }

        if request["dead_letter"] is not None:
            return {
                "request_id": request_id,
                "generation": generation,
                "replica_id": replica_id,
                "outcome": requested_outcome,
                "status": "ignored_late",
                "terminal_attempt_status": attempt["status"],
            }

        reason = outcome.get("reason") or trigger
        self._finish_attempt(
            request,
            attempt,
            terminal=terminal,
            trigger=trigger,
            clock_ms=clock_ms,
            reason=reason,
        )
        return {
            "request_id": request_id,
            "generation": generation,
            "replica_id": replica_id,
            "outcome": requested_outcome,
            "status": "applied",
            "terminal_attempt_status": terminal,
            "trigger": trigger,
            "clock_ms": clock_ms,
        }

    def _finish_attempt(
        self,
        request: dict[str, Any],
        attempt: dict[str, Any],
        *,
        terminal: str,
        trigger: str,
        clock_ms: int,
        reason: str,
    ) -> None:
        attempt["status"] = terminal
        attempt["outcome"] = trigger
        attempt["outcome_ms"] = clock_ms
        attempt["outcome_reason"] = reason
        request["last_transition_ms"] = clock_ms
        counters = self.state["counters"]

        if terminal == "succeeded":
            request["status"] = "confirmed"
            request["confirmed"] = True
            request["confirmed_at_ms"] = clock_ms
            request["next_eligible_ms"] = None
            attempt["next_eligible_ms"] = None
            counters["successes"] += 1
            return

        if terminal == "abandoned":
            counters["abandonments"] += 1
            if int(request["generation"]) >= int(
                self.config["backoff"]["max_generation"]
            ):
                self._dead_letter(request, attempt, "abandoned", clock_ms, reason)
            else:
                request["status"] = "queued"
                request["next_eligible_ms"] = clock_ms
                attempt["next_eligible_ms"] = clock_ms
            return

        if terminal == "failed":
            counters["failures"] += 1
        elif terminal == "rejected":
            counters["rejections"] += 1
        elif terminal == "expired":
            counters["expirations"] += 1

        if int(request["generation"]) >= int(
            self.config["backoff"]["max_generation"]
        ):
            self._dead_letter(request, attempt, trigger, clock_ms, reason)
            return

        delay, exponential, jitter = _backoff_delay(
            self.config,
            int(request["request_id"]),
            int(request["generation"]),
            str(attempt["replica_id"]),
            clock_ms,
        )
        next_eligible = clock_ms + delay
        request["status"] = "retry_wait"
        request["next_eligible_ms"] = next_eligible
        attempt["next_eligible_ms"] = next_eligible
        attempt["backoff"] = {
            "exponential_ms": exponential,
            "jitter_ms": jitter,
            "delay_ms": delay,
            "next_eligible_ms": next_eligible,
            "clock_ms": clock_ms,
        }

    def _dead_letter(
        self,
        request: dict[str, Any],
        attempt: dict[str, Any],
        trigger: str,
        clock_ms: int,
        reason: str,
    ) -> None:
        request_id = int(request["request_id"])
        request_key = str(request_id)
        if request["dead_letter"] is not None:
            return
        base = {
            "request_id": request_id,
            "service": request["service"],
            "generation": int(request["generation"]),
            "attempt_id": attempt["attempt_id"],
            "trigger": trigger,
            "reason": reason,
            "created_ms": clock_ms,
        }
        tombstone_hash = sha256_obj(base)
        tombstone = dict(base)
        tombstone["tombstone_id"] = f"tomb:{tombstone_hash[:16]}"
        tombstone["tombstone_hash"] = tombstone_hash
        request["dead_letter"] = tombstone
        request["status"] = "dead_letter"
        request["next_eligible_ms"] = None
        attempt["tombstone_id"] = tombstone["tombstone_id"]
        self.state["tombstones"][request_key] = tombstone
        self.state["counters"]["dead_letters"] += 1

    # ------------------------------------------------------------------
    # Dispatch round
    # ------------------------------------------------------------------
    def _normalise_command(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise GatewayError("E_INVALID_COMMAND", "round command must be an object")
        round_id = raw.get("round_id")
        if not isinstance(round_id, str) or not round_id:
            raise GatewayError(
                "E_INVALID_COMMAND",
                "round_id must be a non-empty string",
                field="round_id",
            )
        kind = raw.get("kind", "dispatch")
        if kind not in {"dispatch", "resume"}:
            raise GatewayError(
                "E_INVALID_COMMAND", "kind must be dispatch or resume", field="kind"
            )
        clock_ms = _require_int(raw.get("clock_ms"), "clock_ms", 0)
        budget_ms = _require_int(
            raw.get("budget_ms", self.config["round_budget_ms"]),
            "budget_ms",
            1,
        )
        abort_overdue = raw.get("abort_overdue", False)
        if not isinstance(abort_overdue, bool):
            raise GatewayError(
                "E_INVALID_COMMAND", "abort_overdue must be a boolean"
            )
        dispatch_enabled = raw.get("dispatch", True)
        if not isinstance(dispatch_enabled, bool):
            raise GatewayError("E_INVALID_COMMAND", "dispatch must be a boolean")
        recover_only = raw.get("recover_only", False)
        if not isinstance(recover_only, bool):
            raise GatewayError("E_INVALID_COMMAND", "recover_only must be a boolean")

        service_filter = raw.get("services", [])
        if not isinstance(service_filter, list) or not all(
            isinstance(value, str) and value for value in service_filter
        ):
            raise GatewayError(
                "E_INVALID_COMMAND", "services must be a list of service names"
            )
        for name in service_filter:
            if name not in self.state["services"]:
                raise GatewayError(
                    "E_SERVICE_NOT_FOUND",
                    f"dispatch references unknown service {name}",
                    service=name,
                )
        request_filter = raw.get("requests", [])
        if not isinstance(request_filter, list):
            raise GatewayError("E_INVALID_COMMAND", "requests must be a list")
        normalised_request_filter: list[int] = []
        for value in request_filter:
            if isinstance(value, bool) or not isinstance(value, int):
                raise GatewayError(
                    "E_INVALID_COMMAND", "requests entries must be integers"
                )
            normalised_request_filter.append(value)

        targets_raw = raw.get("force_targets", {})
        if not isinstance(targets_raw, dict):
            raise GatewayError("E_INVALID_COMMAND", "force_targets must be an object")
        force_targets: dict[str, str] = {}
        for request_key, replica_id in targets_raw.items():
            try:
                request_id = int(request_key)
            except (TypeError, ValueError) as exc:
                raise GatewayError(
                    "E_INVALID_COMMAND",
                    "force_targets keys must be request ids",
                    field="force_targets",
                ) from exc
            if not isinstance(replica_id, str) or not replica_id:
                raise GatewayError(
                    "E_INVALID_COMMAND",
                    "force_targets values must be replica ids",
                    request_id=request_id,
                )
            force_targets[str(request_id)] = replica_id

        max_launches_raw = raw.get("max_launches")
        if max_launches_raw is not None:
            max_launches = _require_int(max_launches_raw, "max_launches", 0)
        else:
            max_launches = None

        outcomes_raw = raw.get("outcomes", [])
        if not isinstance(outcomes_raw, list):
            raise GatewayError("E_INVALID_COMMAND", "outcomes must be a list")
        outcomes = [
            self._normalise_outcome(item, index, clock_ms)
            for index, item in enumerate(outcomes_raw)
        ]
        outcomes.sort(key=lambda value: (value["request_id"], value["generation"]))

        return {
            "round_id": round_id,
            "kind": kind,
            "clock_ms": clock_ms,
            "budget_ms": budget_ms,
            "abort_overdue": abort_overdue,
            "dispatch": dispatch_enabled,
            "recover_only": recover_only,
            "services": sorted(set(service_filter)),
            "requests": sorted(set(normalised_request_filter)),
            "force_targets": dict(sorted(force_targets.items(), key=lambda item: int(item[0]))),
            "max_launches": max_launches,
            "outcomes": outcomes,
        }

    def run_round(self, raw_command: dict[str, Any]) -> dict[str, Any]:
        backup = copy.deepcopy(self.state)
        try:
            return self._run_round_inplace(raw_command)
        except BaseException:
            self.state.clear()
            self.state.update(backup)
            raise

    def _run_round_inplace(self, raw_command: dict[str, Any]) -> dict[str, Any]:
        command = self._normalise_command(raw_command)
        round_id = command["round_id"]
        existing = self.state["rounds"].get(round_id)
        if existing is not None:
            if canonical_json(existing["command"]) != canonical_json(command):
                raise GatewayError(
                    "E_IDEMPOTENCY_CONFLICT",
                    "round_id was reused with a different command payload",
                    round_id=round_id,
                )
            result = copy.deepcopy(existing["result"])
            result["idempotent"] = True
            return result

        requested_clock = int(command["clock_ms"])
        effective_clock = self._advance_clock(requested_clock)
        self._refill_all(effective_clock)

        recovery = {
            "inflight_detected": [
                {
                    "request_id": int(request["request_id"]),
                    "service": request["service"],
                    "generation": int(attempt["generation"]),
                    "replica_id": attempt["replica_id"],
                    "deadline_ms": int(attempt["deadline_ms"]),
                }
                for request, attempt in self._active_attempts()
            ],
            "expired": [],
        }
        failed_after_start: list[dict[str, Any]] = []
        abandoned: list[dict[str, Any]] = []

        if command["abort_overdue"]:
            for request, attempt in self._active_attempts():
                if effective_clock > int(attempt["deadline_ms"]):
                    self._finish_attempt(
                        request,
                        attempt,
                        terminal="expired",
                        trigger="expired",
                        clock_ms=effective_clock,
                        reason="active-abort-overdue",
                    )
                    item = {
                        "request_id": int(request["request_id"]),
                        "service": request["service"],
                        "generation": int(attempt["generation"]),
                        "replica_id": attempt["replica_id"],
                        "deadline_ms": int(attempt["deadline_ms"]),
                        "clock_ms": effective_clock,
                    }
                    recovery["expired"].append(item)
                    failed_after_start.append(dict(item, reason="expired"))

        outcome_results: list[dict[str, Any]] = []
        for outcome in command["outcomes"]:
            result = self._apply_outcome(outcome, effective_clock)
            outcome_results.append(result)
            if result["status"] == "applied":
                if result["terminal_attempt_status"] in {
                    "failed",
                    "rejected",
                    "expired",
                }:
                    failed_after_start.append(
                        {
                            "request_id": result["request_id"],
                            "service": self.state["requests"][
                                str(result["request_id"])
                            ]["service"],
                            "generation": result["generation"],
                            "replica_id": result["replica_id"],
                            "reason": result["terminal_attempt_status"],
                            "clock_ms": effective_clock,
                        }
                    )
                elif result["terminal_attempt_status"] == "abandoned":
                    abandoned.append(
                        {
                            "request_id": result["request_id"],
                            "service": self.state["requests"][
                                str(result["request_id"])
                            ]["service"],
                            "generation": result["generation"],
                            "replica_id": result["replica_id"],
                            "reason": "abandoned",
                            "clock_ms": effective_clock,
                        }
                    )

        started: list[dict[str, Any]] = []
        not_started: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        budget_used = 0
        budget_exhausted = False
        if command["dispatch"] and not command["recover_only"]:
            started, not_started, deferred, budget_used, budget_exhausted = (
                self._dispatch_available(command, effective_clock)
            )

        result = {
            "round_id": round_id,
            "kind": command["kind"],
            "clock_ms": effective_clock,
            "requested_clock_ms": requested_clock,
            "budget_ms": int(command["budget_ms"]),
            "budget_used_ms": budget_used,
            "budget_remaining_ms": int(command["budget_ms"]) - budget_used,
            "budget_exhausted": budget_exhausted,
            "recovery": recovery,
            "outcomes": outcome_results,
            "started": started,
            "not_started": not_started,
            "deferred": deferred,
            "failed_after_start": failed_after_start,
            "abandoned": abandoned,
            "tokens": self._token_summary(),
            "idempotent": False,
        }
        self.state["rounds"][round_id] = {
            "command": command,
            "result": copy.deepcopy(result),
        }
        self.state["queue_version"] = int(self.state["queue_version"]) + 1
        self.state["counters"]["rounds"] += 1
        return result

    def _token_summary(self) -> dict[str, Any]:
        return {
            "global": token_view(self.state["global_token"]),
            "services": {
                name: token_view(self.state["services"][name]["token"])
                for name in sorted(self.state["services"])
            },
        }

    def _select_replica(
        self,
        service_name: str,
        request: dict[str, Any],
        force_targets: dict[str, str],
    ) -> tuple[dict[str, Any] | None, str | None]:
        service = self.state["services"][service_name]
        usage = self._replica_usage(service_name)
        request_key = str(request["request_id"])
        forced = force_targets.get(request_key)
        if forced is not None:
            replica = next(
                (
                    item
                    for item in service["replicas"]
                    if item["replica_id"] == forced
                ),
                None,
            )
            if replica is None:
                return None, "E_REPLICA_NOT_FOUND"
            if usage[forced] >= int(replica["slots"]):
                return None, "E_REPLICA_SLOTS_FULL"
            return replica, None

        available = [
            replica
            for replica in service["replicas"]
            if usage[replica["replica_id"]] < int(replica["slots"])
        ]
        if not available:
            return None, "E_REPLICA_SLOTS_FULL"
        available.sort(
            key=lambda replica: (
                usage[replica["replica_id"]],
                -float(replica["compute_units"]),
                replica["replica_id"],
            )
        )
        return available[0], None

    def _dispatch_available(
        self, command: dict[str, Any], clock_ms: int
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        int,
        bool,
    ]:
        service_filter = set(command["services"])
        request_filter = set(command["requests"])
        queues: dict[str, list[dict[str, Any]]] = {}
        deferred: list[dict[str, Any]] = []

        for request_id in sorted(self.state["requests"], key=int):
            request = self.state["requests"][request_id]
            if request_filter and int(request["request_id"]) not in request_filter:
                continue
            if service_filter and request["service"] not in service_filter:
                continue
            if request["confirmed"] or request["dead_letter"] is not None:
                continue
            if any(attempt["status"] == "inflight" for attempt in request["attempts"]):
                continue
            if int(request["eligible_at_ms"]) > clock_ms:
                deferred.append(
                    {
                        "request_id": int(request["request_id"]),
                        "service": request["service"],
                        "reason": "batch_window",
                        "eligible_at_ms": int(request["eligible_at_ms"]),
                        "clock_ms": clock_ms,
                    }
                )
                continue
            next_eligible = request.get("next_eligible_ms")
            if next_eligible is not None and int(next_eligible) > clock_ms:
                deferred.append(
                    {
                        "request_id": int(request["request_id"]),
                        "service": request["service"],
                        "reason": "backoff",
                        "next_eligible_ms": int(next_eligible),
                        "clock_ms": clock_ms,
                    }
                )
                continue
            queues.setdefault(request["service"], []).append(request)

        for service_name in queues:
            queues[service_name].sort(
                key=lambda item: (
                    -int(item["priority"]),
                    int(item["arrival_ms"]),
                    int(item["request_id"]),
                )
            )

        started: list[dict[str, Any]] = []
        not_started: list[dict[str, Any]] = []
        budget_remaining = int(command["budget_ms"])
        budget_used = 0
        budget_exhausted = False
        max_launches = command["max_launches"]

        def mark_remaining(reason: str, error_code: str | None = None) -> None:
            for service_name in sorted(queues):
                for request in queues[service_name]:
                    item = {
                        "request_id": int(request["request_id"]),
                        "service": service_name,
                        "reason": reason,
                    }
                    if error_code is not None:
                        item["error_code"] = error_code
                    not_started.append(item)
                queues[service_name] = []

        stop = False
        while not stop:
            progressed = False
            for service_name in sorted(list(queues)):
                if stop:
                    break
                service_queue = queues[service_name]
                if not service_queue:
                    continue

                if int(self.state["global_token"]["balance_fixed"]) < self.scale:
                    mark_remaining("global_tokens", "E_TOKEN_EXHAUSTED")
                    stop = True
                    break
                if (
                    self._global_inflight()
                    >= int(self.config["global"]["max_inflight"])
                ):
                    mark_remaining("global_inflight", "E_CONCURRENCY_LIMIT")
                    stop = True
                    break

                service = self.state["services"][service_name]
                if int(service["token"]["balance_fixed"]) < self.scale:
                    for request in service_queue:
                        not_started.append(
                            {
                                "request_id": int(request["request_id"]),
                                "service": service_name,
                                "reason": "service_tokens",
                                "error_code": "E_TOKEN_EXHAUSTED",
                            }
                        )
                    queues[service_name] = []
                    continue
                if self._service_inflight(service_name) >= int(service["max_inflight"]):
                    for request in service_queue:
                        not_started.append(
                            {
                                "request_id": int(request["request_id"]),
                                "service": service_name,
                                "reason": "service_inflight",
                                "error_code": "E_CONCURRENCY_LIMIT",
                            }
                        )
                    queues[service_name] = []
                    continue
                if max_launches is not None and len(started) >= max_launches:
                    mark_remaining("max_launches")
                    stop = True
                    break

                request = service_queue[0]
                replica, replica_error = self._select_replica(
                    service_name, request, command["force_targets"]
                )
                if replica is None:
                    service_queue.pop(0)
                    not_started.append(
                        {
                            "request_id": int(request["request_id"]),
                            "service": service_name,
                            "replica_id": command["force_targets"].get(
                                str(request["request_id"])
                            ),
                            "reason": "replica_slots"
                            if replica_error == "E_REPLICA_SLOTS_FULL"
                            else "replica_missing",
                            "error_code": replica_error,
                            "generation": int(request["generation"]),
                        }
                    )
                    progressed = True
                    continue

                cost_ms = _dispatch_cost_ms(service, replica)
                if cost_ms > budget_remaining:
                    budget_exhausted = True
                    mark_remaining("budget_exhausted")
                    stop = True
                    break

                generation = int(request["generation"]) + 1
                max_generation = int(self.config["backoff"]["max_generation"])
                if generation > max_generation:
                    self._dead_letter(
                        request,
                        {
                            "attempt_id": f"req:{request['request_id']}:exhausted",
                        },
                        "exhausted",
                        clock_ms,
                        "generation-limit",
                    )
                    service_queue.pop(0)
                    progressed = True
                    continue

                global_after = charge_token_ledger(
                    self.state["global_token"], self.scale
                )
                service_after = charge_token_ledger(service["token"], self.scale)
                request["generation"] = generation
                attempt_id = f"req:{request['request_id']}:gen:{generation}"
                attempt = {
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "replica_id": replica["replica_id"],
                    "dispatch_ms": clock_ms,
                    "deadline_ms": clock_ms
                    + int(self.config["dispatch_timeout_ms"]),
                    "cost_ms": cost_ms,
                    "status": "inflight",
                    "outcome": None,
                    "outcome_ms": None,
                    "outcome_reason": None,
                    "next_eligible_ms": None,
                    "backoff": None,
                    "tombstone_id": None,
                    "quota": {
                        "scale": self.scale,
                        "global_cost_fixed": self.scale,
                        "service_cost_fixed": self.scale,
                        "global_balance_after_fixed": global_after,
                        "service_balance_after_fixed": service_after,
                        "model_input_tokens": int(request["input_tokens"]),
                        "model_output_tokens": int(request["output_tokens"]),
                        "model_token_budget": int(request["input_tokens"])
                        + int(request["output_tokens"]),
                    },
                }
                request["attempts"].append(attempt)
                request["status"] = "inflight"
                request["next_eligible_ms"] = None
                request["last_transition_ms"] = clock_ms
                service_queue.pop(0)
                budget_remaining -= cost_ms
                budget_used += cost_ms
                self.state["counters"]["dispatches"] += 1
                started.append(
                    {
                        "request_id": int(request["request_id"]),
                        "service": service_name,
                        "generation": generation,
                        "replica_id": replica["replica_id"],
                        "attempt_id": attempt_id,
                        "dispatch_ms": clock_ms,
                        "deadline_ms": attempt["deadline_ms"],
                        "cost_ms": cost_ms,
                        "priority": int(request["priority"]),
                        "token_cost": {
                            "global_fixed": self.scale,
                            "service_fixed": self.scale,
                            "scale": self.scale,
                        },
                        "global_balance_after_fixed": global_after,
                        "service_balance_after_fixed": service_after,
                    }
                )
                progressed = True

            if not any(queues.values()):
                break
            if stop:
                break
            if not progressed:
                not_started.extend(
                    {
                        "request_id": int(request["request_id"]),
                        "service": service_name,
                        "reason": "no_progress",
                    }
                    for service_name in sorted(queues)
                    for request in queues[service_name]
                )
                break

        return started, not_started, deferred, budget_used, budget_exhausted


def build_status(state: dict[str, Any], *, version: int | None = None) -> dict[str, Any]:
    services: dict[str, Any] = {}
    service_inflight: dict[str, int] = {}
    replica_inflight: dict[tuple[str, str], int] = {}
    for request in state["requests"].values():
        for attempt in request["attempts"]:
            if attempt["status"] == "inflight":
                service_inflight[request["service"]] = (
                    service_inflight.get(request["service"], 0) + 1
                )
                key = (request["service"], attempt["replica_id"])
                replica_inflight[key] = replica_inflight.get(key, 0) + 1

    for service_name in sorted(state["services"]):
        service = state["services"][service_name]
        replicas = []
        for replica in service["replicas"]:
            used = replica_inflight.get((service_name, replica["replica_id"]), 0)
            replicas.append(
                {
                    "replica_id": replica["replica_id"],
                    "slots": int(replica["slots"]),
                    "used_slots": used,
                    "free_slots": int(replica["slots"]) - used,
                    "compute_units": replica["compute_units"],
                }
            )
        services[service_name] = {
            "model_version": service["model_version"],
            "batch_window_ms": int(service["batch_window_ms"]),
            "max_inflight": int(service["max_inflight"]),
            "inflight": service_inflight.get(service_name, 0),
            "token": token_view(service["token"]),
            "replicas": replicas,
        }

    requests: dict[str, Any] = {}
    pending_queue: list[dict[str, Any]] = []
    for request_id in sorted(state["requests"], key=int):
        request = state["requests"][request_id]
        request_view = {
            "request_id": int(request["request_id"]),
            "service": request["service"],
            "arrival_ms": int(request["arrival_ms"]),
            "eligible_at_ms": int(request["eligible_at_ms"]),
            "input_tokens": int(request["input_tokens"]),
            "output_tokens": int(request["output_tokens"]),
            "priority": int(request["priority"]),
            "generation": int(request["generation"]),
            "status": request["status"],
            "next_eligible_ms": request["next_eligible_ms"],
            "confirmed": bool(request["confirmed"]),
            "confirmed_at_ms": request["confirmed_at_ms"],
            "dead_letter": request["dead_letter"],
            "attempts": request["attempts"],
        }
        requests[request_id] = request_view
        if request["status"] in {"queued", "retry_wait"}:
            pending_queue.append(
                {
                    "request_id": int(request["request_id"]),
                    "service": request["service"],
                    "status": request["status"],
                    "next_eligible_ms": request["next_eligible_ms"],
                }
            )

    result = {
        "ok": True,
        "schema_version": 1,
        "db_id": state["db_id"],
        "config_version": int(state["config_version"]),
        "queue_version": int(state["queue_version"]),
        "logical_clock_watermark_ms": int(state["clock_watermark_ms"]),
        "global_token": token_view(state["global_token"]),
        "global_max_inflight": int(state["config"]["global"]["max_inflight"]),
        "services": services,
        "requests": requests,
        "pending_queue": pending_queue,
        "tombstones": {
            key: state["tombstones"][key] for key in sorted(state["tombstones"], key=int)
        },
        "counters": state["counters"],
        "inflight_global": sum(service_inflight.values()),
        "round_count": len(state["rounds"]),
    }
    if version is not None:
        result["ledger_version"] = version
    return result
