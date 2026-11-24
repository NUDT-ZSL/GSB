"""Device binding and dual-quota renewal adjudication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .constants import SECONDS_TO_MILLIS_ERROR_MS, WINDOW_ENDPOINT_ERROR_MS
from .errors import BusinessError
from .fingerprint import compare_fingerprints
from .models import RenewalRequest, TicketDeclaration
from .normalize import NormalizedDataset


@dataclass
class RenewalState:
    """The minimum state needed to resume quota adjudication."""

    window_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    last_accepted_ms: dict[str, int] = field(default_factory=dict)
    last_clock_error_ms: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_counts": {
                device: dict(sorted(windows.items()))
                for device, windows in sorted(self.window_counts.items())
            },
            "last_accepted_ms": dict(sorted(self.last_accepted_ms.items())),
            "last_clock_error_ms": dict(sorted(self.last_clock_error_ms.items())),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RenewalState":
        return cls(
            window_counts={
                str(device): {
                    str(window): int(count)
                    for window, count in sorted(windows.items())
                }
                for device, windows in sorted(value.get("window_counts", {}).items())
            },
            last_accepted_ms={
                str(ticket): int(timestamp)
                for ticket, timestamp in sorted(
                    value.get("last_accepted_ms", {}).items()
                )
            },
            last_clock_error_ms={
                str(ticket): int(error)
                for ticket, error in sorted(
                    value.get("last_clock_error_ms", {}).items()
                )
            },
        )

    def accept(
        self,
        *,
        device_id: str,
        window_key: str,
        ticket_id: str,
        observed_at_ms: int,
        clock_error_ms: int,
    ) -> None:
        windows = self.window_counts.setdefault(device_id, {})
        windows[window_key] = windows.get(window_key, 0) + 1
        self.last_accepted_ms[ticket_id] = observed_at_ms
        self.last_clock_error_ms[ticket_id] = clock_error_ms


@dataclass(frozen=True)
class Adjudication:
    report: dict[str, Any]
    event: dict[str, Any]


def _decision_counts(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {"accept": 0, "reject": 0, "ambiguous": 0}
    for event in events:
        decision = str(event.get("decision"))
        counts[decision] = counts.get(decision, 0) + 1
    return counts


def _code_counts(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        code = event.get("code")
        if code is not None:
            counts[str(code)] = counts.get(str(code), 0) + 1
    return dict(sorted(counts.items()))


def build_summary(
    *,
    watermark: int,
    events: list[dict[str, Any]],
    state: RenewalState,
) -> dict[str, Any]:
    return {
        "watermark": watermark,
        "decision_counts": _decision_counts(events),
        "code_counts": _code_counts(events),
        "processed_request_ids": sorted(
            str(event["request_id"]) for event in events
        ),
        "state": state.as_dict(),
    }


def replay_events(events: Iterable[dict[str, Any]]) -> RenewalState:
    """Rebuild quota state from checkpointed events without replaying decisions."""

    state = RenewalState()
    for event in events:
        if event.get("decision") != "accept":
            continue
        state.accept(
            device_id=str(event["device_id"]),
            window_key=str(event["window_key"]),
            ticket_id=str(event["ticket_id"]),
            observed_at_ms=int(event["observed_at_ms"]),
            clock_error_ms=int(event.get("clock_error_ms", 0)),
        )
    return state


class RenewalEngine:
    def __init__(self, dataset: NormalizedDataset) -> None:
        if dataset.count_quota is None or dataset.interval_quota is None:
            raise BusinessError(
                "E_INVALID_RECORD",
                "both count and interval quota declarations are required",
            )
        self.dataset = dataset

    def _ticket(self, ticket_id: str) -> TicketDeclaration:
        ticket = self.dataset.tickets.get(ticket_id)
        if ticket is None:
            raise BusinessError(
                "E_UNKNOWN_TICKET",
                f"ticket {ticket_id!r} is not available",
                details={"ticket_id": ticket_id},
            )
        return ticket

    def _quota_diagnostics(
        self,
        request: RenewalRequest,
        ticket: TicketDeclaration,
        state: RenewalState,
    ) -> dict[str, Any]:
        assert self.dataset.count_quota is not None
        assert self.dataset.interval_quota is not None
        profile = self.dataset.platforms[request.fingerprint.platform]
        clock_error_ms = profile.clock_quantum_ms // 2
        count_quota = self.dataset.count_quota
        interval_quota = self.dataset.interval_quota

        anchor_ms = count_quota.window_anchor_seconds * 1_000
        window_ms = count_quota.window_seconds * 1_000
        window_index = (request.observed_at_ms - anchor_ms) // window_ms
        window_start_ms = anchor_ms + window_index * window_ms
        window_end_ms = window_start_ms + window_ms
        window_key = (
            f"{count_quota.window_anchor_seconds}:"
            f"{count_quota.window_seconds}:{window_index}"
        )
        boundary_distance_ms = min(
            request.observed_at_ms - window_start_ms,
            window_end_ms - request.observed_at_ms,
        )
        seconds_window_error_ms = WINDOW_ENDPOINT_ERROR_MS
        window_error_ms = seconds_window_error_ms + clock_error_ms
        window_ambiguous = boundary_distance_ms <= window_error_ms
        prior_count = state.window_counts.get(request.device_id, {}).get(window_key, 0)
        count_margin_slots = (
            count_quota.max_renewals_per_window - prior_count
        )
        count_exceeded = count_margin_slots <= 0

        interval_anchor_ms = state.last_accepted_ms.get(
            ticket.ticket_id,
            ticket.issued_at_ms,
        )
        interval_anchor_clock_error_ms = state.last_clock_error_ms.get(
            ticket.ticket_id,
            0,
        )
        elapsed_ms = request.observed_at_ms - interval_anchor_ms
        interval_margin_ms = elapsed_ms - interval_quota.min_interval_ms
        interval_error_ms = clock_error_ms + interval_anchor_clock_error_ms
        interval_ambiguous = (
            interval_error_ms > 0
            and abs(interval_margin_ms) <= interval_error_ms
        )
        interval_exceeded = interval_margin_ms < -interval_error_ms

        if window_ambiguous or interval_ambiguous:
            quota_uncertain = True
        else:
            quota_uncertain = False

        first_rejection: str | None = None
        if count_exceeded:
            first_rejection = "count_window"
        elif interval_exceeded:
            first_rejection = "min_interval"

        return {
            "window_key": window_key,
            "clock_error_ms": clock_error_ms,
            "count": {
                "window_index": window_index,
                "window_start_ms": window_start_ms,
                "window_end_ms": window_end_ms,
                "boundary_distance_ms": boundary_distance_ms,
                "prior_count": prior_count,
                "limit": count_quota.max_renewals_per_window,
                "margin_slots": count_margin_slots,
                "exceeded": count_exceeded,
                "ambiguous": window_ambiguous,
                "error_bound_ms": window_error_ms,
                "seconds_conversion_step_ms": 1_000,
                "seconds_conversion_error_ms": seconds_window_error_ms,
                "clock_alignment_error_ms": clock_error_ms,
            },
            "interval": {
                "anchor_ms": interval_anchor_ms,
                "anchor_source": (
                    "last_accepted_renewal"
                    if ticket.ticket_id in state.last_accepted_ms
                    else "ticket_issued_at"
                ),
                "elapsed_ms": elapsed_ms,
                "minimum_ms": interval_quota.min_interval_ms,
                "margin_ms": interval_margin_ms,
                "exceeded": interval_exceeded,
                "ambiguous": interval_ambiguous,
                "error_bound_ms": interval_error_ms,
                "clock_alignment_error_ms": interval_error_ms,
            },
            "uncertain": quota_uncertain,
            "first_rejection": first_rejection,
        }

    def adjudicate(
        self,
        request: RenewalRequest,
        state: RenewalState | None = None,
    ) -> Adjudication:
        if state is None:
            state = RenewalState()
        if request.ticket_id not in self.dataset.tickets:
            report = {
                "request_id": request.request_id,
                "batch_id": request.batch_id,
                "ticket_id": request.ticket_id,
                "device_id": request.device_id,
                "observed_at_ms": request.observed_at_ms,
                "decision": "reject",
                "code": "E_UNKNOWN_TICKET",
                "failure_stage": "ticket_lookup",
                "checks": {
                    "device_binding": {
                        "result": "unavailable",
                        "ticket_device_id": None,
                        "request_device_id": request.device_id,
                    },
                    "fingerprint": {
                        "result": "unavailable",
                        "bound": None,
                        "request": request.fingerprint.as_dict(),
                    },
                    "ticket": {"result": "unknown"},
                    "quota": {"result": "unavailable"},
                },
                "quota_margins": {
                    "count_slots": None,
                    "interval_ms": None,
                    "count_window_boundary_ms": None,
                },
            }
            event = {
                "batch_id": request.batch_id,
                "request_id": request.request_id,
                "ticket_id": request.ticket_id,
                "device_id": request.device_id,
                "observed_at_ms": request.observed_at_ms,
                "decision": "reject",
                "code": "E_UNKNOWN_TICKET",
                "window_key": None,
                "clock_error_ms": 0,
                "successor_ticket_id": request.successor_ticket_id,
            }
            return Adjudication(report=report, event=event)
        ticket = self._ticket(request.ticket_id)
        decision = "accept"
        code: str | None = None
        failure_stage: str | None = None

        device_result = "match"
        if request.device_id != ticket.device_id:
            device_result = "mismatch"
            decision = "reject"
            code = "E_DEVICE_MISMATCH"
            failure_stage = "device_binding"

        fingerprint_result = None
        bound_declaration = self.dataset.fingerprints.get(ticket.device_id)
        if bound_declaration is None:
            decision = "reject"
            code = "E_UNKNOWN_DEVICE"
            failure_stage = "device_binding"
        else:
            fingerprint_result = compare_fingerprints(
                request.fingerprint,
                bound_declaration.fingerprint,
            )
            if fingerprint_result.result == "mismatch":
                decision = "reject"
                code = "E_DEVICE_MISMATCH"
                failure_stage = "fingerprint"
            elif fingerprint_result.result == "ambiguous":
                decision = "ambiguous"
                code = "E_FP_AMBIGUOUS"
                failure_stage = "fingerprint"

        generation_result = "valid"
        if decision == "accept" and ticket.generation >= ticket.renewal_limit:
            generation_result = "exceeded"
            decision = "reject"
            code = "E_GENERATION_EXCEEDED"
            failure_stage = "generation"

        validity_result = "valid"
        if decision == "accept":
            if request.observed_at_ms < ticket.issued_at_ms:
                validity_result = "not_yet_valid"
                decision = "reject"
                code = "E_NOT_YET_VALID"
                failure_stage = "ticket_validity"
            elif request.observed_at_ms >= ticket.expires_at_ms:
                validity_result = "expired"
                decision = "reject"
                code = "E_TICKET_EXPIRED"
                failure_stage = "ticket_validity"

        quota = self._quota_diagnostics(request, ticket, state)
        if decision == "accept":
            if quota["uncertain"]:
                decision = "ambiguous"
                code = "E_QUOTA_AMBIGUOUS"
                failure_stage = "quota"
            elif quota["count"]["exceeded"]:
                decision = "reject"
                code = "E_QUOTA_COUNT"
                failure_stage = "count_window"
            elif quota["interval"]["exceeded"]:
                decision = "reject"
                code = "E_QUOTA_INTERVAL"
                failure_stage = "min_interval"

        if decision == "accept":
            state.accept(
                device_id=request.device_id,
                window_key=quota["window_key"],
                ticket_id=ticket.ticket_id,
                observed_at_ms=request.observed_at_ms,
                clock_error_ms=quota["clock_error_ms"],
            )

        bound_dict = (
            bound_declaration.fingerprint.as_dict()
            if bound_declaration is not None
            else None
        )
        report = {
            "request_id": request.request_id,
            "batch_id": request.batch_id,
            "ticket_id": request.ticket_id,
            "device_id": request.device_id,
            "observed_at_ms": request.observed_at_ms,
            "decision": decision,
            "code": code,
            "failure_stage": failure_stage,
            "checks": {
                "device_binding": {
                    "result": device_result,
                    "ticket_device_id": ticket.device_id,
                    "request_device_id": request.device_id,
                },
                "fingerprint": (
                    fingerprint_result.as_dict()
                    if fingerprint_result is not None
                    else {
                        "result": "unavailable",
                        "bound": bound_dict,
                        "request": request.fingerprint.as_dict(),
                    }
                ),
                "ticket": {
                    "generation": ticket.generation,
                    "renewal_limit": ticket.renewal_limit,
                    "issued_at_ms": ticket.issued_at_ms,
                    "expires_at_ms": ticket.expires_at_ms,
                    "generation_result": generation_result,
                    "validity_result": validity_result,
                },
                "quota": quota,
            },
            "quota_margins": {
                "count_slots": quota["count"]["margin_slots"],
                "interval_ms": quota["interval"]["margin_ms"],
                "count_window_boundary_ms": quota["count"]["boundary_distance_ms"],
            },
        }
        event = {
            "batch_id": request.batch_id,
            "request_id": request.request_id,
            "ticket_id": request.ticket_id,
            "device_id": request.device_id,
            "observed_at_ms": request.observed_at_ms,
            "decision": decision,
            "code": code,
            "window_key": quota["window_key"],
            "clock_error_ms": quota["clock_error_ms"],
            "successor_ticket_id": request.successor_ticket_id,
        }
        return Adjudication(report=report, event=event)

    def adjudicate_all(
        self,
        requests: Iterable[RenewalRequest],
        state: RenewalState | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], RenewalState]:
        current_state = state or RenewalState()
        reports: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for request in requests:
            adjudication = self.adjudicate(request, current_state)
            reports.append(adjudication.report)
            events.append(adjudication.event)
        return reports, events, current_state

