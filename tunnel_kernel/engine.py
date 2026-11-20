"""Deterministic forwarding engine driven exclusively by an injected clock."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Sequence

from .codec import (
    HEADER_SIZE,
    FragmentPacket,
    decode_outer,
    fragment_payload,
    parse_fragment,
    pack_fragments,
)
from .config import LinkChange, LinkProfile, Peer, Policy
from .errors import (
    E_ABORTED,
    E_BAD_MTU,
    E_IO,
    E_RETRY_EXHAUSTED,
    E_ROUND_BUDGET,
    E_STATE_CORRUPT,
    BusinessError,
    fail,
)
from .store import Store
from .transport import Transport, make_transport


@dataclass(slots=True)
class LogicalClock:
    now_ms: int
    step_ms: int

    def advance(self) -> int:
        self.now_ms += self.step_ms
        return self.now_ms

    def advance_to(self, value: int) -> int:
        if value > self.now_ms:
            self.now_ms = value
        return self.now_ms


class ForwardEngine:
    """Send persisted fragments and update their committed watermarks."""

    MAX_ROUNDS = 10_000

    def __init__(
        self,
        *,
        store: Store,
        peer: Peer,
        link: LinkProfile,
        policy: Policy,
        scenario_changes: Sequence[LinkChange] = (),
        transport: Transport | None = None,
        abort_event: threading.Event | None = None,
        crash_after_sends: int | None = None,
    ) -> None:
        self.store = store
        self.peer = peer
        self.link = link
        self.policy = policy
        self.changes = sorted(
            (
                change
                for change in scenario_changes
                if change.peer == peer.name
            ),
            key=lambda change: change.at_ms,
        )
        self.transport = transport
        self.abort_event = abort_event
        self.crash_after_sends = crash_after_sends
        self._applied_changes = 0
        self._send_count = 0

    def forward(self, tunnel_id: str, *, resume: bool = False) -> dict[str, Any]:
        with self.store.tunnel_session_lock(tunnel_id):
            state = self.store.get_state(tunnel_id)
            if state["peer"] != self.peer.name:
                fail(
                    E_STATE_CORRUPT,
                    "persisted peer does not match the configured peer",
                    details={
                        "configured": self.peer.name,
                        "persisted": state["peer"],
                    },
                )
            if state["status"] == "complete":
                if state["trace"]:
                    return state["trace"]
            if state["status"] == "failed":
                raise BusinessError(
                    state["failure_code"] or E_RETRY_EXHAUSTED,
                    "tunnel is in a failed state",
                    details={"tunnel_id": tunnel_id},
                )

            owns_transport = self.transport is None
            transport = self.transport or make_transport(
                self.peer,
                self.policy,
                abort_event=self.abort_event,
            )
            clock = LogicalClock(
                now_ms=max(self.policy.clock_start_ms, state["logical_clock_ms"]),
                step_ms=self.policy.logical_clock_step_ms,
            )
            trace: dict[str, Any] = {"events": []}
            try:
                return self._run(
                    tunnel_id,
                    state=state,
                    clock=clock,
                    trace=trace,
                    transport=transport,
                    resume=resume,
                )
            finally:
                if owns_transport:
                    transport.close()

    def _run(
        self,
        tunnel_id: str,
        *,
        state: dict[str, Any],
        clock: LogicalClock,
        trace: dict[str, Any],
        transport: Transport,
        resume: bool,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = trace["events"]
        events.append(
            {
                "algorithm": state["algorithm"],
                "clock_start_ms": clock.now_ms,
                "fragment_threshold_bytes": self.policy.fragment_threshold_bytes,
                "initial_mtu": state["current_mtu"],
                "type": "plan",
            }
        )

        if resume:
            recovered = self.store.recover_inflight(tunnel_id, now_ms=clock.now_ms)
            events.append(
                {
                    "recovered_inflight": recovered,
                    "time_ms": clock.now_ms,
                    "type": "recover",
                }
            )
            state = self.store.get_state(tunnel_id)

        self._check_abort()
        current_state = state
        if current_state["status"] == "complete":
            return self._complete_result(tunnel_id, current_state, trace)

        for round_index in range(self.MAX_ROUNDS):
            self._check_abort()
            self._apply_due_link_changes(
                tunnel_id,
                clock=clock,
                events=events,
            )
            current_state = self.store.get_state(tunnel_id)
            if current_state["status"] == "complete":
                return self._complete_result(tunnel_id, current_state, trace)

            rows = self.store.get_fragment_rows(tunnel_id)
            pending = [row for row in rows if row["status"] == "pending"]
            inflight = [row for row in rows if row["status"] == "inflight"]
            if not pending:
                if inflight:
                    fail(
                        E_STATE_CORRUPT,
                        "in-flight fragments remain without an owner",
                        details={
                            "inflight_ranges": current_state["inflight_ranges"],
                            "tunnel_id": tunnel_id,
                        },
                    )
                fail(
                    E_STATE_CORRUPT,
                    "tunnel has no pending or in-flight fragments but is not complete",
                    details={"tunnel_id": tunnel_id},
                )

            ready = self._ready_pending(pending, now_ms=clock.now_ms)
            if not ready:
                next_due = min(
                    self._due_time(row) for row in pending if int(row["attempts"]) > 0
                )
                from_ms = clock.now_ms
                clock.advance_to(next_due)
                events.append(
                    {
                        "from_ms": from_ms,
                        "to_ms": clock.now_ms,
                        "type": "backoff",
                    }
                )
                self.store.record_clock(tunnel_id, clock.now_ms)
                continue

            slots = max(
                1,
                min(
                    self.policy.max_concurrency,
                    self.policy.round_budget_ms
                    // self.policy.logical_clock_step_ms,
                ),
            )
            selected = ready[:slots]
            reservations: list[dict[str, Any]] = []
            for row in selected:
                reservation = self.store.reserve_fragment(
                    tunnel_id,
                    int(row["seq"]),
                    now_ms=clock.now_ms,
                )
                if not reservation["idempotent"]:
                    reservations.append(reservation)
            if not reservations:
                continue

            packets = [parse_fragment(item["packet"]) for item in reservations]
            envelopes = self._build_envelopes(
                packets,
                algorithm=current_state["algorithm"],
                mtu=current_state["current_mtu"],
            )
            events.append(
                {
                    "algorithm": current_state["algorithm"],
                    "concurrency_limit": self.policy.max_concurrency,
                    "round_budget_ms": self.policy.round_budget_ms,
                    "round_index": round_index,
                    "slots": slots,
                    "time_ms": clock.now_ms,
                    "type": "round",
                }
            )

            confirmed: set[int] = set()
            sent_sequences: set[int] = set()
            complete_summary: dict[str, Any] | None = None
            window_end = clock.now_ms + self.policy.round_budget_ms
            for envelope, envelope_packets in envelopes:
                sequences = [packet.sequence for packet in envelope_packets]
                sent_sequences.update(sequences)
                self._check_abort()
                events.append(
                    {
                        "aggregated": len(sequences) > 1,
                        "algorithm": current_state["algorithm"],
                        "attempts": [
                            self._attempt_for(reservations, sequence)
                            for sequence in sequences
                        ],
                        "sequences": sequences,
                        "time_ms": clock.now_ms,
                        "type": "send",
                        "wire_bytes": len(envelope),
                        "window_end_ms": window_end,
                        "window_start_ms": clock.now_ms,
                    }
                )
                response = transport.send(envelope)
                self._send_count += 1
                if (
                    self.crash_after_sends is not None
                    and self._send_count >= self.crash_after_sends
                ):
                    os._exit(137)
                if response is None:
                    events.append(
                        {
                            "io_timeout_ms": self.policy.io_timeout_ms,
                            "sequences": sequences,
                            "time_ms": clock.now_ms,
                            "type": "timeout",
                        }
                    )
                    continue
                code = response.get("code")
                if code != "OK":
                    raise BusinessError(
                        str(code or E_IO),
                        str(response.get("message", "echo service rejected datagram")),
                        details={"response": response, "sequences": sequences},
                    )
                response_confirmed = {
                    int(value) for value in response.get("confirmed_sequences", [])
                }
                confirmed.update(response_confirmed)
                if response.get("complete"):
                    complete_summary = response

            if confirmed:
                accepted = self.store.apply_ack(
                    tunnel_id,
                    acked_sequences=sorted(confirmed),
                    summary=complete_summary or {"complete": False},
                    now_ms=clock.now_ms,
                )
            else:
                accepted = self.store.get_state(tunnel_id)

            unconfirmed = sent_sequences - confirmed
            if unconfirmed:
                self.store.release_inflight(
                    tunnel_id,
                    sorted(unconfirmed),
                    now_ms=clock.now_ms,
                )
            events.append(
                {
                    "confirmed": sorted(confirmed),
                    "confirmed_through": accepted["confirmed_through"],
                    "sequences_unconfirmed": sorted(unconfirmed),
                    "time_ms": clock.now_ms,
                    "type": "ack",
                }
            )
            clock.advance_to(clock.now_ms + self.policy.logical_clock_step_ms)
            self.store.record_clock(tunnel_id, clock.now_ms)
            current_state = self.store.get_state(tunnel_id)
            if current_state["status"] == "complete":
                return self._complete_result(tunnel_id, current_state, trace)

        self.store.mark_failed(
            tunnel_id,
            E_ROUND_BUDGET,
            now_ms=clock.now_ms,
        )
        fail(
            E_ROUND_BUDGET,
            "forwarding exceeded the maximum deterministic round count",
            details={"tunnel_id": tunnel_id},
        )

    def _build_envelopes(
        self,
        packets: Sequence[FragmentPacket],
        *,
        algorithm: str,
        mtu: int,
    ) -> list[tuple[bytes, list[FragmentPacket]]]:
        if algorithm == "direct":
            return [(packet.raw, [packet]) for packet in packets]
        grouped: list[tuple[bytes, list[FragmentPacket]]] = []
        try:
            envelopes = pack_fragments(packets, mtu=mtu)
        except BusinessError as exc:
            if exc.code != E_BAD_MTU:
                raise
            # Fragments that were already sent once keep their original bytes.
            # If a later MTU downshift makes them larger than the new aggregate
            # budget, resend them as standalone legacy datagrams.
            return [(packet.raw, [packet]) for packet in packets]
        for envelope in envelopes:
            _, decoded = decode_outer(envelope)
            grouped.append((envelope, decoded))
        return grouped

    def _attempt_for(
        self, reservations: Sequence[dict[str, Any]], sequence: int
    ) -> int:
        for reservation in reservations:
            if reservation["sequence"] == sequence:
                return int(reservation["attempt"])
        return 0

    def _due_time(self, row: dict[str, Any]) -> int:
        attempts = int(row["attempts"])
        last_sent = row["last_sent_ms"]
        if attempts <= 0 or last_sent is None:
            return 0
        backoff = min(
            self.policy.max_backoff_ms,
            self.policy.retransmit_backoff_ms * (2 ** max(0, attempts - 1)),
        )
        return int(last_sent) + backoff

    def _ready_pending(
        self, rows: Sequence[dict[str, Any]], *, now_ms: int
    ) -> list[dict[str, Any]]:
        ready = [
            row
            for row in rows
            if int(row["attempts"]) == 0 or self._due_time(row) <= now_ms
        ]
        return sorted(ready, key=lambda row: int(row["seq"]))

    def _apply_due_link_changes(
        self,
        tunnel_id: str,
        *,
        clock: LogicalClock,
        events: list[dict[str, Any]],
    ) -> None:
        while (
            self._applied_changes < len(self.changes)
            and self.changes[self._applied_changes].at_ms <= clock.now_ms
        ):
            change = self.changes[self._applied_changes]
            self._applied_changes += 1
            old_link = self.link
            self.link = LinkProfile(
                rtt_ms=change.rtt_ms if change.rtt_ms is not None else old_link.rtt_ms,
                loss_rate=(
                    change.loss_rate
                    if change.loss_rate is not None
                    else old_link.loss_rate
                ),
                bandwidth_mbps=(
                    change.bandwidth_mbps
                    if change.bandwidth_mbps is not None
                    else old_link.bandwidth_mbps
                ),
                jitter_ms=(
                    change.jitter_ms
                    if change.jitter_ms is not None
                    else old_link.jitter_ms
                ),
            )
            state = self.store.get_state(tunnel_id)
            current_mtu = int(state["current_mtu"])
            candidate: int | None = None
            reason: str | None = None
            if change.mtu is not None:
                candidate = change.mtu
                reason = "explicit_mtu"
            else:
                tolerance = self.policy.tolerance
                loss_changed = (
                    abs(self.link.loss_rate - old_link.loss_rate)
                    >= tolerance.loss_rate_delta
                    and self.link.loss_rate > tolerance.loss_rate_trigger
                )
                rtt_changed = (
                    abs(self.link.rtt_ms - old_link.rtt_ms)
                    >= tolerance.rtt_ms_delta
                    and self.link.rtt_ms > tolerance.rtt_ms_trigger
                )
                if loss_changed or rtt_changed:
                    candidate = max(
                        tolerance.min_mtu,
                        current_mtu - tolerance.mtu_down_step,
                    )
                    reasons: list[str] = []
                    if loss_changed:
                        reasons.append("loss_rate")
                    if rtt_changed:
                        reasons.append("rtt_ms")
                    reason = "+".join(reasons)

            minimum_mtu = (
                HEADER_SIZE
                + self.peer.crypto_overhead(self.policy.crypto_overhead_bytes)
                + 1
            )
            if candidate is not None and candidate < current_mtu:
                if candidate < minimum_mtu:
                    fail(
                        E_BAD_MTU,
                        "adaptive MTU would leave no fragment payload capacity",
                        details={"candidate": candidate, "minimum": minimum_mtu},
                    )
                context = self.store.get_replan_context(tunnel_id)
                payload = context["payload"]
                frontier = int(context["frontier"])
                base_sequence = int(context["base_sequence"])
                replanned_sequences: list[int] = []
                if frontier < len(payload):
                    remaining = payload[frontier:]
                    new_fragments = fragment_payload(
                        remaining,
                        message_id=context["message_id"],
                        mtu=candidate,
                        fragment_threshold_bytes=self.policy.fragment_threshold_bytes,
                        encrypted=self.peer.encrypted,
                        crypto_overhead_bytes=self.policy.crypto_overhead_bytes,
                        start_sequence=base_sequence,
                        original_len=len(payload),
                        original_digest=state["payload_digest"],
                    )
                    replanned_sequences = [
                        packet.sequence for packet in new_fragments
                    ]
                    self.store.replan_tail(
                        tunnel_id,
                        new_mtu=candidate,
                        expected_frontier=frontier,
                        base_sequence=base_sequence,
                        fragments=new_fragments,
                        now_ms=clock.now_ms,
                    )
                else:
                    self.store.update_mtu(
                        tunnel_id,
                        candidate,
                        now_ms=clock.now_ms,
                    )
                events.append(
                    {
                        "base_sequence": base_sequence,
                        "frontier": frontier,
                        "from_mtu": current_mtu,
                        "reason": reason,
                        "replanned_sequences": replanned_sequences,
                        "time_ms": clock.now_ms,
                        "to_mtu": candidate,
                        "type": "mtu_change",
                    }
                )
            else:
                events.append(
                    {
                        "loss_rate": self.link.loss_rate,
                        "reason": "below_downshift_threshold"
                        if candidate is None
                        else "requested_mtu_not_lower",
                        "rtt_ms": self.link.rtt_ms,
                        "time_ms": clock.now_ms,
                        "type": "link_change",
                    }
                )

    def _check_abort(self) -> None:
        if self.abort_event is not None and self.abort_event.is_set():
            raise BusinessError(E_ABORTED, "forwarding was actively aborted")

    def _complete_result(
        self,
        tunnel_id: str,
        state: dict[str, Any],
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        if state["status"] != "complete":
            fail(E_STATE_CORRUPT, "tunnel did not reach complete state")
        if state["reassembly_digest"] != state["payload_digest"]:
            fail(
                E_STATE_CORRUPT,
                "complete tunnel digest mismatch",
                details={"tunnel_id": tunnel_id},
            )
        result = {
            "algorithm": state["algorithm"],
            "confirmed_bytes": state["confirmed_bytes"],
            "confirmed_through": state["confirmed_through"],
            "events": trace["events"],
            "expected_fragments": state["expected_fragments"],
            "reassembly_digest": state["reassembly_digest"],
            "recovery_count": state["recovery_count"],
            "retransmit_count": state["retransmit_count"],
            "status": "complete",
            "tunnel_id": tunnel_id,
        }
        self.store.save_trace(tunnel_id, result)
        return result
