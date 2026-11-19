from __future__ import annotations

from typing import Any

from .errors import (
    E_BAD_ACK,
    E_BAD_SCRIPT,
    E_BAD_TRANSITION,
    E_CLOCK_REWIND,
    E_INCONSISTENCY,
    E_RETRY_EXHAUSTED,
    E_TIMEOUT_IDLE,
    E_TIMEOUT_NOT_DUE,
    EngineError,
)
from .fixed import format_fixed, round_ratio, to_fixed
from .scene import (
    Scene,
    SceneEvent,
    digest_json,
)
from .state import Segment, TcpState


DATA_STATES = frozenset(
    {
        "ESTABLISHED",
        "FIN-WAIT-1",
        "FIN-WAIT-2",
        "CLOSE-WAIT",
        "LAST-ACK",
        "TIME-WAIT",
    }
)


def initial_state(scene: Scene) -> TcpState:
    scale = scene.congestion.fixed_point_scale
    return TcpState(
        tcp_state=scene.initial_state,
        cwnd_fixed=to_fixed(scene.congestion.initial_cwnd, scale),
        ssthresh_fixed=to_fixed(scene.congestion.initial_ssthresh, scale),
        peer_window=scene.link.receiver_window,
        rto_ms=scene.congestion.initial_rto_ms,
    )


def _window_values(state: TcpState, scene: Scene) -> dict[str, Any]:
    scale = scene.congestion.fixed_point_scale
    cwnd_bytes = state.cwnd_fixed // scale
    flight = state.in_flight
    unsent = state.unsent
    limit = min(cwnd_bytes, state.peer_window)
    additional = max(0, limit - flight)

    if state.tcp_state == "FAILED":
        reason = "failed"
    elif state.tcp_state != "ESTABLISHED":
        reason = f"state:{state.tcp_state}"
    elif unsent == 0:
        reason = "app_empty"
    elif additional > 0:
        reason = "none"
    elif flight >= cwnd_bytes and flight >= state.peer_window:
        reason = "both"
    elif flight >= cwnd_bytes:
        reason = "congestion"
    else:
        reason = "receiver_window"

    sendable_now = 0
    if state.tcp_state == "ESTABLISHED" and unsent > 0:
        sendable_now = min(additional, unsent)
    return {
        "cwnd_fixed": state.cwnd_fixed,
        "cwnd_bytes": format_fixed(state.cwnd_fixed, scale),
        "cwnd_bytes_floor": cwnd_bytes,
        "ssthresh_fixed": state.ssthresh_fixed,
        "ssthresh_bytes": format_fixed(state.ssthresh_fixed, scale),
        "receiver_window_bytes": state.peer_window,
        "flight_limit_bytes": limit,
        "additional_sendable_bytes": additional if state.tcp_state == "ESTABLISHED" else 0,
        "sendable_now_bytes": sendable_now,
        "in_flight_bytes": flight,
        "unsent_bytes": unsent,
        "blocked_reason": reason,
    }


def observe(state: TcpState, scene: Scene) -> dict[str, Any]:
    values = {
        "tcp_state": state.tcp_state,
        "snd_una": state.snd_una,
        "snd_nxt": state.snd_nxt,
        "app_end": state.app_end,
        "retry_count": state.retry_count,
        "rto_ms": state.rto_ms,
        "deadline_ms": state.deadline_ms,
        "dup_ack_count": state.dup_ack_count,
        "in_recovery": state.in_recovery,
        "recover_seq": state.recover_seq,
        "outstanding_segments": len(state.segments),
        "peer_data_received": state.peer_data_received,
        "peer_data_read": state.peer_data_read,
        "failed_code": state.failed_code,
    }
    values.update(_window_values(state, scene))
    return values


def _require_state(
    state: TcpState, event: SceneEvent, allowed: set[str] | frozenset[str]
) -> None:
    if state.tcp_state not in allowed:
        raise EngineError(
            E_BAD_TRANSITION,
            f"event {event.kind!r} is illegal in state {state.tcp_state}",
            {
                "event_index": event.index,
                "event_id": event.event_id,
                "event_type": event.kind,
                "state": state.tcp_state,
                "allowed_states": sorted(allowed),
            },
        )


def _transition(
    state: TcpState,
    event: SceneEvent,
    to_state: str,
    reason: str,
) -> dict[str, Any] | None:
    from_state = state.tcp_state
    if from_state == to_state:
        return None
    state.tcp_state = to_state
    return {
        "event_index": event.index,
        "event_id": event.event_id,
        "event_type": event.kind,
        "ts_ms": event.ts_ms,
        "from_state": from_state,
        "to_state": to_state,
        "reason": reason,
    }


def _set_window(state: TcpState, event: SceneEvent, scene: Scene, raw_window: int) -> int:
    if scene.link.window_scaling:
        effective = raw_window << scene.link.window_scale
    else:
        effective = raw_window
    state.peer_window = effective
    return effective


def _send_pending(
    state: TcpState, scene: Scene, ts_ms: int
) -> list[dict[str, int]]:
    if state.tcp_state != "ESTABLISHED":
        return []
    scale = scene.congestion.fixed_point_scale
    cwnd_bytes = state.cwnd_fixed // scale
    capacity = min(cwnd_bytes, state.peer_window)
    room = max(0, capacity - state.in_flight)
    count = min(room, state.unsent)
    if count <= 0:
        return []

    had_flight = state.in_flight > 0
    sent: list[dict[str, int]] = []
    remaining = count
    while remaining > 0:
        chunk = min(scene.congestion.mss, remaining)
        start = state.snd_nxt
        end = start + chunk
        state.segments.append(
            Segment(start=start, end=end, sent_at_ms=ts_ms, retransmits=0)
        )
        state.snd_nxt = end
        sent.append({"start": start, "end": end, "bytes": chunk})
        remaining -= chunk

    if not had_flight and state.in_flight > 0:
        state.deadline_ms = ts_ms + state.rto_ms
    return sent


def _capped_ssthresh(
    flight_bytes: int, scene: Scene
) -> tuple[int, dict[str, Any]]:
    half, audit = round_ratio(flight_bytes, 2, "floor")
    floor_bytes = max(half, 2 * scene.congestion.mss)
    audit["floor_bytes"] = floor_bytes
    audit["minimum_bytes"] = 2 * scene.congestion.mss
    return floor_bytes, audit


def _grow_cwnd(
    state: TcpState, scene: Scene, acked_bytes: int
) -> list[dict[str, Any]]:
    if acked_bytes <= 0:
        return []
    scale = scene.congestion.fixed_point_scale
    mss_fixed = to_fixed(scene.congestion.mss, scale)
    remaining = acked_bytes
    steps: list[dict[str, Any]] = []

    if state.cwnd_fixed < state.ssthresh_fixed:
        room_fixed = state.ssthresh_fixed - state.cwnd_fixed
        room_bytes = room_fixed // scale
        slow_bytes = min(remaining, room_bytes)
        if slow_bytes > 0:
            before = state.cwnd_fixed
            delta = slow_bytes * scale
            state.cwnd_fixed += delta
            remaining -= slow_bytes
            steps.append(
                {
                    "phase": "slow_start",
                    "acked_bytes": slow_bytes,
                    "cwnd_before_fixed": before,
                    "delta_fixed": delta,
                    "cwnd_after_fixed": state.cwnd_fixed,
                    "rounding": {
                        "mode": "exact",
                        "direction": "exact",
                        "remainder": 0,
                    },
                }
            )

    if remaining > 0:
        numerator = mss_fixed * to_fixed(remaining, scale)
        delta, rounding = round_ratio(
            numerator,
            state.cwnd_fixed,
            scene.congestion.ca_increment_rounding,
        )
        before = state.cwnd_fixed
        state.cwnd_fixed += delta
        steps.append(
            {
                "phase": "congestion_avoidance",
                "acked_bytes": remaining,
                "cwnd_before_fixed": before,
                "delta_fixed": delta,
                "cwnd_after_fixed": state.cwnd_fixed,
                "rounding": rounding,
            }
        )
    return steps


def _oldest_unacked_range(
    state: TcpState, scene: Scene
) -> tuple[int, int] | None:
    for segment in state.segments:
        if segment.end <= state.snd_una:
            continue
        start = max(segment.start, state.snd_una)
        end = min(segment.end, start + scene.congestion.mss)
        if end > start:
            return start, end
    if state.snd_nxt > state.snd_una:
        start = state.snd_una
        end = min(state.snd_nxt, start + scene.congestion.mss)
        return start, end
    return None


def _retransmit_oldest(
    state: TcpState,
    scene: Scene,
    ts_ms: int,
    reason: str,
    attempt: int,
) -> dict[str, Any] | None:
    chosen = _oldest_unacked_range(state, scene)
    if chosen is None:
        return None
    start, end = chosen
    increment = 1
    for segment in state.segments:
        if segment.start <= start < segment.end or segment.start == start:
            segment.retransmits += increment
            break
    return {
        "reason": reason,
        "ts_ms": ts_ms,
        "seq_start": start,
        "seq_end": end,
        "attempt": attempt,
        "rto_ms": state.rto_ms,
        "cwnd_fixed": state.cwnd_fixed,
    }


def _handle_ack(
    state: TcpState,
    event: SceneEvent,
    scene: Scene,
    ts_ms: int,
    window_steps: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    ack_no = event.payload["ack_no"]
    if ack_no < state.snd_una:
        raise EngineError(
            E_BAD_ACK,
            "ACK number is older than snd_una",
            {
                "event_index": event.index,
                "ack_no": ack_no,
                "snd_una": state.snd_una,
            },
        )
    if ack_no > state.snd_nxt:
        raise EngineError(
            E_BAD_ACK,
            "ACK number is beyond the highest sent byte",
            {
                "event_index": event.index,
                "ack_no": ack_no,
                "snd_nxt": state.snd_nxt,
            },
        )

    if "window" in event.payload:
        _set_window(state, event, scene, event.payload["window"])

    previous_una = state.snd_una
    acked = ack_no - previous_una
    if acked == 0:
        state.dup_ack_count += 1
    else:
        state.snd_una = ack_no
        state.trim_acked(ack_no)
        state.dup_ack_count = 0
        if scene.congestion.retry_scope == "consecutive":
            state.retry_count = 0
        state.rto_ms = scene.congestion.initial_rto_ms

    retransmission: dict[str, Any] | None = None
    if acked > 0 and state.in_recovery:
        if ack_no >= state.recover_seq:
            before = state.cwnd_fixed
            state.cwnd_fixed = state.ssthresh_fixed
            state.in_recovery = False
            window_steps.append(
                {
                    "phase": "fast_recovery_exit",
                    "acked_bytes": acked,
                    "cwnd_before_fixed": before,
                    "delta_fixed": state.cwnd_fixed - before,
                    "cwnd_after_fixed": state.cwnd_fixed,
                    "rounding": {"mode": "exact", "direction": "exact", "remainder": 0},
                }
            )
        else:
            before = state.cwnd_fixed
            state.cwnd_fixed = state.ssthresh_fixed + to_fixed(
                scene.congestion.dup_ack_threshold * scene.congestion.mss,
                scene.congestion.fixed_point_scale,
            )
            state.in_recovery = True
            window_steps.append(
                {
                    "phase": "fast_recovery_partial_ack",
                    "acked_bytes": acked,
                    "cwnd_before_fixed": before,
                    "delta_fixed": state.cwnd_fixed - before,
                    "cwnd_after_fixed": state.cwnd_fixed,
                    "rounding": {"mode": "exact", "direction": "exact", "remainder": 0},
                }
            )
            retransmission = _retransmit_oldest(
                state,
                scene,
                ts_ms,
                "partial_ack_retransmit",
                state.retry_count + 1,
            )
            state.deadline_ms = ts_ms + state.rto_ms
    elif acked > 0:
        window_steps.extend(_grow_cwnd(state, scene, acked))

    if state.in_flight == 0:
        state.deadline_ms = None
        state.dup_ack_count = 0
        state.in_recovery = False
    elif acked > 0 and scene.congestion.rto_reset_on_ack:
        state.deadline_ms = ts_ms + state.rto_ms

    return retransmission, True


def _handle_dup_ack(
    state: TcpState,
    event: SceneEvent,
    scene: Scene,
    ts_ms: int,
    window_steps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if "ack_no" in event.payload and event.payload["ack_no"] != state.snd_una:
        raise EngineError(
            E_BAD_ACK,
            "duplicate ACK does not match snd_una",
            {
                "event_index": event.index,
                "ack_no": event.payload["ack_no"],
                "snd_una": state.snd_una,
            },
        )
    if state.in_flight == 0:
        return None

    count = event.payload["count"]
    state.dup_ack_count += count
    if not scene.congestion.fast_retransmit:
        return None
    threshold = scene.congestion.dup_ack_threshold

    if state.in_recovery:
        before = state.cwnd_fixed
        delta = to_fixed(
            count * scene.congestion.mss, scene.congestion.fixed_point_scale
        )
        state.cwnd_fixed += delta
        window_steps.append(
            {
                "phase": "fast_recovery_inflate",
                "acked_bytes": 0,
                "cwnd_before_fixed": before,
                "delta_fixed": delta,
                "cwnd_after_fixed": state.cwnd_fixed,
                "rounding": {"mode": "exact", "direction": "exact", "remainder": 0},
            }
        )
        return None

    if state.dup_ack_count < threshold:
        return None

    if state.dup_ack_count >= threshold:
        ssthresh_bytes, audit = _capped_ssthresh(state.in_flight, scene)
        state.ssthresh_fixed = to_fixed(
            ssthresh_bytes, scene.congestion.fixed_point_scale
        )
        before = state.cwnd_fixed
        if scene.congestion.fast_recovery:
            inflation = to_fixed(
                threshold * scene.congestion.mss,
                scene.congestion.fixed_point_scale,
            )
            state.cwnd_fixed = state.ssthresh_fixed + inflation
            state.in_recovery = True
            state.recover_seq = state.snd_nxt
            phase = "fast_retransmit_recovery"
        else:
            state.cwnd_fixed = state.ssthresh_fixed
            state.in_recovery = False
            phase = "fast_retransmit"
        state.dup_ack_count = 0
        window_steps.append(
            {
                "phase": phase,
                "acked_bytes": 0,
                "cwnd_before_fixed": before,
                "delta_fixed": state.cwnd_fixed - before,
                "cwnd_after_fixed": state.cwnd_fixed,
                "ssthresh_fixed": state.ssthresh_fixed,
                "ssthresh_audit": audit,
                "rounding": audit,
            }
        )
        retransmission = _retransmit_oldest(
            state, scene, ts_ms, "fast_retransmit", state.retry_count + 1
        )
        state.deadline_ms = ts_ms + state.rto_ms
        return retransmission
    return None


def _handle_timeout(
    state: TcpState,
    event: SceneEvent,
    scene: Scene,
    ts_ms: int,
    window_steps: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if state.in_flight == 0:
        raise EngineError(
            E_TIMEOUT_IDLE,
            "timeout event has no unacknowledged data to retransmit",
            {"event_index": event.index, "event_id": event.event_id},
        )
    if state.deadline_ms is None:
        raise EngineError(
            E_TIMEOUT_IDLE,
            "timeout event has no armed retransmission timer",
            {"event_index": event.index, "event_id": event.event_id},
        )
    if ts_ms < state.deadline_ms:
        raise EngineError(
            E_TIMEOUT_NOT_DUE,
            "timeout event arrived before the injected logical-clock deadline",
            {
                "event_index": event.index,
                "event_ts_ms": event.ts_ms,
                "effective_ts_ms": ts_ms,
                "deadline_ms": state.deadline_ms,
            },
        )

    references: list[tuple[int, int]] = []
    if "seq" in event.payload:
        references.append((event.payload["seq"], event.payload["seq"] + 1))
    if "range" in event.payload:
        references.append(tuple(event.payload["range"]))
    for start, end in references:
        if start < state.snd_una or end > state.snd_nxt:
            raise EngineError(
                E_BAD_SCRIPT,
                "timeout references a byte range that is not currently sent and unacknowledged",
                {
                    "event_index": event.index,
                    "range": [start, end],
                    "snd_una": state.snd_una,
                    "snd_nxt": state.snd_nxt,
                },
            )

    if state.retry_count + 1 > scene.congestion.max_retries:
        state.retry_count += 1
        state.failed_code = E_RETRY_EXHAUSTED
        state.tcp_state = "FAILED"
        state.deadline_ms = None
        window_steps.append(
            {
                "phase": "retry_budget_exhausted",
                "acked_bytes": 0,
                "cwnd_before_fixed": state.cwnd_fixed,
                "delta_fixed": 0,
                "cwnd_after_fixed": state.cwnd_fixed,
                "rounding": {"mode": "exact", "direction": "exact", "remainder": 0},
            }
        )
        fatal = {
            "code": E_RETRY_EXHAUSTED,
            "message": "retransmission retry budget exhausted",
            "retry_count": state.retry_count,
            "max_retries": scene.congestion.max_retries,
        }
        return None, fatal

    state.retry_count += 1
    ssthresh_bytes, audit = _capped_ssthresh(state.in_flight, scene)
    before = state.cwnd_fixed
    state.ssthresh_fixed = to_fixed(
        ssthresh_bytes, scene.congestion.fixed_point_scale
    )
    state.cwnd_fixed = to_fixed(
        scene.congestion.timeout_cwnd, scene.congestion.fixed_point_scale
    )
    new_rto = min(
        scene.congestion.max_rto_ms,
        state.rto_ms * scene.congestion.rto_backoff,
    )
    state.rto_ms = new_rto
    state.deadline_ms = ts_ms + new_rto
    state.dup_ack_count = 0
    state.in_recovery = False
    window_steps.append(
        {
            "phase": "timeout_backoff",
            "acked_bytes": 0,
            "cwnd_before_fixed": before,
            "delta_fixed": state.cwnd_fixed - before,
            "cwnd_after_fixed": state.cwnd_fixed,
            "ssthresh_fixed": state.ssthresh_fixed,
            "rto_ms": new_rto,
            "ssthresh_audit": audit,
            "rounding": audit,
        }
    )
    retransmission = _retransmit_oldest(
        state, scene, ts_ms, "timeout", state.retry_count
    )
    return retransmission, None


class Simulator:
    """Pure single-event TCP state transition engine."""

    def __init__(self, scene: Scene, state: TcpState | None = None) -> None:
        self.scene = scene
        self.state = (state or initial_state(scene)).clone()

    def clone_for_replay(self) -> "Simulator":
        return Simulator(self.scene, self.state)

    def advance(self, event: SceneEvent) -> dict[str, Any]:
        before = observe(self.state, self.scene)
        candidate = self.state.clone()
        tolerance = self.scene.congestion.clock_tolerance_ms
        if event.ts_ms + tolerance < candidate.last_ts_ms:
            raise EngineError(
                E_CLOCK_REWIND,
                "logical clock moved backwards beyond the configured tolerance",
                {
                    "event_index": event.index,
                    "event_ts_ms": event.ts_ms,
                    "last_ts_ms": candidate.last_ts_ms,
                    "tolerance_ms": tolerance,
                },
            )
        ts_ms = max(event.ts_ms, candidate.last_ts_ms)
        transition: dict[str, Any] | None = None
        retransmission: dict[str, Any] | None = None
        window_steps: list[dict[str, Any]] = []
        fatal: dict[str, Any] | None = None
        sent: list[dict[str, int]] = []

        if candidate.failed_code is not None:
            raise EngineError(
                candidate.failed_code,
                "connection is already in a failed state",
                {
                    "event_index": event.index,
                    "failed_code": candidate.failed_code,
                },
            )

        kind = event.kind
        if kind == "listen":
            _require_state(candidate, event, {"CLOSED", "LISTEN"})
            transition = _transition(candidate, event, "LISTEN", "application_listen")
        elif kind == "connect":
            _require_state(candidate, event, {"LISTEN", "CLOSED"})
            transition = _transition(candidate, event, "SYN-SENT", "active_open")
        elif kind == "syn_received":
            _require_state(candidate, event, {"LISTEN"})
            transition = _transition(
                candidate, event, "SYN-RECEIVED", "passive_syn_received"
            )
        elif kind == "establish":
            _require_state(candidate, event, {"SYN-SENT", "SYN-RECEIVED"})
            transition = _transition(
                candidate, event, "ESTABLISHED", "handshake_complete"
            )
        elif kind == "app_write":
            _require_state(candidate, event, {"ESTABLISHED"})
            candidate.app_end += event.payload["bytes"]
        elif kind == "app_read":
            _require_state(candidate, event, {"ESTABLISHED", "CLOSE-WAIT"})
            unread = candidate.peer_data_received - candidate.peer_data_read
            if event.payload["bytes"] > unread:
                raise EngineError(
                    E_BAD_SCRIPT,
                    "app_read exceeds received but unread peer bytes",
                    {
                        "event_index": event.index,
                        "requested": event.payload["bytes"],
                        "unread": unread,
                    },
                )
            candidate.peer_data_read += event.payload["bytes"]
        elif kind == "ack":
            _require_state(candidate, event, DATA_STATES)
            retransmission, _ = _handle_ack(
                candidate, event, self.scene, ts_ms, window_steps
            )
        elif kind == "dup_ack":
            _require_state(candidate, event, {"ESTABLISHED"})
            retransmission = _handle_dup_ack(
                candidate, event, self.scene, ts_ms, window_steps
            )
        elif kind == "timeout":
            _require_state(candidate, event, DATA_STATES)
            retransmission, fatal = _handle_timeout(
                candidate, event, self.scene, ts_ms, window_steps
            )
        elif kind == "advertised_window":
            _require_state(candidate, event, DATA_STATES | {"SYN-RECEIVED"})
            _set_window(candidate, event, self.scene, event.payload["window"])
        elif kind == "peer_data":
            _require_state(
                candidate, event, {"ESTABLISHED", "FIN-WAIT-1", "FIN-WAIT-2"}
            )
            candidate.peer_data_received += event.payload["bytes"]
        elif kind == "app_close":
            _require_state(candidate, event, {"ESTABLISHED", "CLOSE-WAIT"})
            target = (
                "FIN-WAIT-1"
                if candidate.tcp_state == "ESTABLISHED"
                else "LAST-ACK"
            )
            transition = _transition(candidate, event, target, "application_close")
        elif kind == "peer_fin":
            _require_state(
                candidate,
                event,
                {"ESTABLISHED", "FIN-WAIT-1", "FIN-WAIT-2", "CLOSE-WAIT"},
            )
            if candidate.tcp_state in {"ESTABLISHED", "CLOSE-WAIT"}:
                transition = _transition(
                    candidate, event, "CLOSE-WAIT", "peer_fin_received"
                )
            else:
                transition = _transition(
                    candidate, event, "TIME-WAIT", "peer_fin_simultaneous_close"
                )
        elif kind == "fin_ack":
            _require_state(candidate, event, {"FIN-WAIT-1", "LAST-ACK"})
            target = (
                "FIN-WAIT-2" if candidate.tcp_state == "FIN-WAIT-1" else "CLOSED"
            )
            transition = _transition(candidate, event, target, "local_fin_acked")
        elif kind == "time_wait_expire":
            _require_state(candidate, event, {"TIME-WAIT"})
            transition = _transition(candidate, event, "CLOSED", "time_wait_expired")
        elif kind == "reset":
            _require_state(
                candidate,
                event,
                set(DATA_STATES) | {"SYN-SENT", "SYN-RECEIVED", "CLOSED"},
            )
            transition = _transition(candidate, event, "CLOSED", "connection_reset")
            candidate.deadline_ms = None
            candidate.in_recovery = False
        else:
            raise EngineError(
                E_BAD_SCRIPT,
                f"unsupported event type: {kind}",
                {"event_index": event.index, "type": kind},
            )

        if fatal is None and kind in {"app_write", "ack", "advertised_window"}:
            sent = _send_pending(candidate, self.scene, ts_ms)

        candidate.last_ts_ms = ts_ms
        after = observe(candidate, self.scene)
        result: dict[str, Any] = {
            "event": event.canonical(),
            "event_digest": event.digest,
            "before": before,
            "after": after,
            "transition": transition,
            "window_steps": window_steps,
            "retransmission": retransmission,
            "sent": sent,
            "fatal_error": fatal,
            "state": candidate.to_dict(),
            "dependency": {
                "source_digest": self.scene.source_digest,
                "event_digest": event.digest,
                "cwnd_before_fixed": before["cwnd_fixed"],
                "cwnd_after_fixed": after["cwnd_fixed"],
                "receiver_window_before_bytes": before["receiver_window_bytes"],
                "receiver_window_after_bytes": after["receiver_window_bytes"],
                "flight_before_bytes": before["in_flight_bytes"],
            },
        }
        self.state = candidate
        return result


def compare_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "after": result["after"],
        "window_steps": result["window_steps"],
        "retransmission": result["retransmission"],
        "sent": result["sent"],
        "fatal_error": result["fatal_error"],
        "state": result["state"],
        "transition": result["transition"],
    }


def replay_prefix(scene: Scene, end_exclusive: int) -> tuple[Simulator, list[dict[str, Any]]]:
    simulator = Simulator(scene)
    results: list[dict[str, Any]] = []
    for event in scene.events[:end_exclusive]:
        results.append(simulator.advance(event))
    return simulator, results


def assert_consistent(
    scene: Scene,
    incremental_result: dict[str, Any],
    event: SceneEvent,
) -> None:
    """Fully recompute the prefix and compare it with the incremental result."""

    reference, results = replay_prefix(scene, event.index + 1)
    reference_result = results[-1]
    left = compare_payload(incremental_result)
    right = compare_payload(reference_result)
    if digest_json(left) == digest_json(right):
        return
    raise EngineError(
        E_INCONSISTENCY,
        "event-driven trajectory diverged from full prefix recomputation",
        {
            "first_divergence": {
                "event_index": event.index,
                "event_id": event.event_id,
                "event_type": event.kind,
                "ts_ms": event.ts_ms,
                "event_driven": left,
                "full_recompute": right,
                "dependency": {
                    "event": event.canonical(),
                    "before": incremental_result["before"],
                    "source_digest": scene.source_digest,
                },
            }
        },
    )

