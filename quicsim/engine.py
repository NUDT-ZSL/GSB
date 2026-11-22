from __future__ import annotations

import json
from typing import Any

from .errors import (
    E_ACK_RANGE,
    E_BAD_SCENE,
    E_CONNECTION_CLOSED,
    E_FC_REGRESS,
    E_PTO_BACKOFF_LIMIT,
    E_PTO_UNSENT,
    E_STREAM_LIMIT,
    EngineError,
)
from .model import (
    SCALE,
    EngineState,
    Packet,
    StreamState,
    intersect_ranges,
    merge_inclusive_ranges,
    merge_ranges,
    new_state,
    range_length,
)
from .scene import PACKET_SPACES, Scene, canonical_json

SPACE_ORDER = {name: index for index, name in enumerate(PACKET_SPACES)}


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


class Engine:
    """Single deterministic event-driven QUIC state machine."""

    def __init__(self, scene: Scene, state: EngineState | None = None):
        self.scene = scene
        if state is None:
            initial_rtt = scene.congestion.pto_initial_rtt_ms
            self.state = new_state(
                max_data=scene.connection.initial_max_data,
                path_mtu=scene.connection.max_udp_payload_size,
                cwnd_packets=scene.congestion.initial_cwnd_packets,
                ssthresh_packets=scene.congestion.initial_ssthresh_packets,
                srtt_ms=initial_rtt,
                rttvar_ms=max(
                    scene.congestion.pto_granularity_ms, initial_rtt // 2
                ),
            )
        else:
            self.state = EngineState.from_dict(state.to_dict())

    @property
    def packet_capacity(self) -> int:
        return self.state.path_mtu - self.scene.connection.packet_overhead_bytes

    def current_pto_ms(self) -> int:
        ack_contribution = _ceil_div(
            self.state.latest_ack_delay_ms * self.scene.connection.ack_delay_factor_num,
            self.scene.connection.ack_delay_factor_den,
        )
        base = self.state.srtt_ms + max(
            4 * self.state.rttvar_ms, self.scene.congestion.pto_granularity_ms
        )
        return max(self.scene.congestion.pto_min_rtt_ms, base) + ack_contribution

    def _ensure_stream(self, stream_id: int, event_seq: int) -> StreamState:
        stream = self.state.streams.get(stream_id)
        if stream is not None:
            return stream
        if len(self.state.streams) >= self.scene.connection.initial_max_streams:
            raise EngineError(
                E_STREAM_LIMIT,
                "initial_max_streams would be exceeded",
                event_seq=event_seq,
                details={
                    "stream_id": stream_id,
                    "active_streams": len(self.state.streams),
                    "initial_max_streams": self.scene.connection.initial_max_streams,
                },
            )
        stream = StreamState(
            stream_id=stream_id,
            max_stream_data=self.scene.connection.initial_max_stream_data,
        )
        self.state.streams[stream_id] = stream
        return stream

    def _resolve_packet_ref(
        self, event: dict[str, Any], require_sent: bool = True
    ) -> Packet | None:
        ref = event.get("packet")
        if ref is None:
            return None
        space_name = ref["space"]
        number = ref["number"]
        space = self.state.spaces[space_name]
        if require_sent and number > space.max_sent_pn:
            raise EngineError(
                E_PTO_UNSENT,
                "probe event references a packet number that has not been sent",
                event_seq=event["seq"],
                details={
                    "space": space_name,
                    "packet_number": number,
                    "max_sent_packet_number": space.max_sent_pn,
                },
            )
        return space.packets.get(number)

    def _ensure_open(self, event: dict[str, Any]) -> None:
        if self.state.connection.closed:
            raise EngineError(
                E_CONNECTION_CLOSED,
                "event cannot be applied after connection close",
                event_seq=event["seq"],
                details={"event_type": event["type"]},
            )

    def _new_packet(
        self,
        space_name: str,
        frames: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> Packet:
        space = self.state.spaces[space_name]
        number = space.next_pn
        space.next_pn += 1
        packet = Packet(
            space=space_name,
            number=number,
            status="in_flight",
            sent_at_ms=event["time_ms"],
            sent_event_seq=event["seq"],
            frames=frames,
            transitions=[
                {
                    "status": "in_flight",
                    "trigger": "send",
                    "event_seq": event["seq"],
                    "time_ms": event["time_ms"],
                }
            ],
        )
        space.packets[number] = packet
        self.state.counters["packets_sent"] = (
            self.state.counters.get("packets_sent", 0) + 1
        )
        return packet

    def _handle_write(self, event: dict[str, Any]) -> None:
        self._ensure_open(event)
        stream = self._ensure_stream(event["stream_id"], event["seq"])
        stream.add_write(event["offset"], event["length"], event["fin"])

    def _sendable_plan(self) -> dict[str, Any]:
        connection_remaining = max(
            0, self.state.max_data - self.state.connection_used_bytes()
        )
        conn_allocated = 0
        stream_plans: list[dict[str, Any]] = []
        limiters: set[str] = set()
        total_bytes = 0
        new_bytes = 0

        for stream_id in sorted(self.state.streams):
            stream = self.state.streams[stream_id]
            local_credit = max(0, stream.max_stream_data - stream.highest_sent_offset)
            candidates = intersect_ranges(
                stream.candidate_send_ranges(), [(0, stream.max_stream_data)]
            )
            accepted: list[list[int]] = []
            stream_new_bytes = 0
            for start, end in candidates:
                if start < stream.highest_sent_offset:
                    free_end = min(end, stream.highest_sent_offset)
                    if start < free_end:
                        accepted.append([start, free_end])
                    start = max(start, stream.highest_sent_offset)
                if start >= end:
                    continue
                extension = end - start
                conn_space = max(0, connection_remaining - conn_allocated)
                take = min(extension, local_credit, conn_space)
                if take > 0:
                    accepted.append([start, start + take])
                    local_credit -= take
                    conn_allocated += take
                    stream_new_bytes += take
                if take < extension:
                    if conn_space <= extension:
                        limiters.add("flow_control_connection")
                    if local_credit <= extension:
                        limiters.add("flow_control_stream")
            accepted = merge_ranges(accepted)
            accepted_bytes = range_length(accepted)
            total_bytes += accepted_bytes
            stream_plans.append(
                {
                    "stream_id": stream_id,
                    "send_offset": stream.highest_sent_offset,
                    "stream_credit_remaining_bytes": local_credit,
                    "sendable_ranges": accepted,
                    "sendable_bytes": accepted_bytes,
                    "flow_controlled_new_bytes": stream_new_bytes,
                    "already_consumed_bytes": accepted_bytes - stream_new_bytes,
                    "pending_unsent_bytes": range_length(stream.unsent_ranges()),
                    "retransmit_bytes": range_length(stream.retransmit_ranges),
                }
            )

        slots = max(
            0,
            self.state.congestion.cwnd_fixed // SCALE
            - self.state.inflight_count(),
        )
        packets_by_data = (
            _ceil_div(total_bytes, self.packet_capacity) if total_bytes > 0 else 0
        )
        sendable_packets = min(slots, packets_by_data)
        if slots == 0 and total_bytes > 0:
            limiters.add("congestion_window")
        if total_bytes == 0:
            limiters.add("no_data")
        if slots < packets_by_data:
            limiters.add("congestion_window")

        return {
            "streams": stream_plans,
            "sendable_bytes": total_bytes,
            "flow_controlled_new_bytes": conn_allocated,
            "already_consumed_bytes": total_bytes - conn_allocated,
            "sendable_packets": sendable_packets,
            "cwnd_remaining_packets": slots,
            "connection_remaining_bytes": max(
                0, self.state.max_data - self.state.connection_used_bytes()
            ),
            "limiters": sorted(limiters),
        }

    def _handle_send(self, event: dict[str, Any]) -> None:
        self._ensure_open(event)
        plan = self._sendable_plan()
        slots = max(
            0,
            self.state.congestion.cwnd_fixed // SCALE
            - self.state.inflight_count(),
        )
        packet_limit = min(event["count"], slots)
        ack_frame = None
        if event.get("ack_ranges"):
            ack_frame = {"type": "ACK", "ranges": event["ack_ranges"]}

        if packet_limit <= 0:
            return

        queue: list[list[int | str]] = []
        for stream_plan in plan["streams"]:
            for start, end in stream_plan["sendable_ranges"]:
                queue.append([stream_plan["stream_id"], start, end])

        packets_created = 0
        while packets_created < packet_limit and (queue or (ack_frame and packets_created == 0)):
            frames: list[dict[str, Any]] = []
            if ack_frame and packets_created == 0:
                frames.append(ack_frame)
            remaining_capacity = self.packet_capacity
            while queue and remaining_capacity > 0:
                stream_id = int(queue[0][0])
                start = int(queue[0][1])
                end = int(queue[0][2])
                take = min(remaining_capacity, end - start)
                if take <= 0:
                    queue.pop(0)
                    continue
                chunk_end = start + take
                stream = self.state.streams[stream_id]
                was_retransmit = bool(
                    intersect_ranges(stream.retransmit_ranges, [(start, chunk_end)])
                )
                frame: dict[str, Any] = {
                    "type": "STREAM",
                    "stream_id": stream_id,
                    "offset": start,
                    "length": take,
                    "fin": bool(stream.fin and stream.fin_offset == chunk_end),
                }
                if was_retransmit:
                    frame["retransmit"] = True
                frames.append(frame)
                stream.mark_sent(start, chunk_end)
                remaining_capacity -= take
                if chunk_end == end:
                    queue.pop(0)
                else:
                    queue[0][1] = chunk_end
            if not frames:
                break
            self._new_packet(event["space"], frames, event)
            packets_created += 1

    def _validate_ack_ranges(self, event: dict[str, Any]) -> None:
        for item in event["ranges"]:
            space_name = item["space"]
            space = self.state.spaces[space_name]
            smallest = item["smallest"]
            largest = item["largest"]
            if smallest > largest or largest > space.max_sent_pn:
                raise EngineError(
                    E_ACK_RANGE,
                    "ACK range is outside the sent packet-number range",
                    event_seq=event["seq"],
                    details={
                        "space": space_name,
                        "smallest": smallest,
                        "largest": largest,
                        "max_sent_packet_number": space.max_sent_pn,
                    },
                )

    def _update_rtt(self, packet: Packet, event: dict[str, Any]) -> None:
        sample = max(
            0,
            event["received_at_ms"] - packet.sent_at_ms - event["ack_delay_ms"],
        )
        if self.state.latest_rtt_ms is None:
            self.state.srtt_ms = max(self.scene.congestion.pto_min_rtt_ms, sample)
            self.state.rttvar_ms = max(
                self.scene.congestion.pto_granularity_ms, sample // 2
            )
        else:
            self.state.rttvar_ms = (
                3 * self.state.rttvar_ms
                + abs(self.state.srtt_ms - sample)
            ) // 4
            self.state.srtt_ms = (7 * self.state.srtt_ms + sample) // 8
            self.state.srtt_ms = max(
                self.scene.congestion.pto_min_rtt_ms, self.state.srtt_ms
            )
        self.state.latest_rtt_ms = sample
        self.state.latest_ack_delay_ms = event["ack_delay_ms"]

    def _increase_cwnd(self, packet: Packet, event: dict[str, Any]) -> None:
        congestion = self.state.congestion
        if congestion.cwnd_fixed < congestion.ssthresh_fixed:
            congestion.cwnd_fixed += SCALE
            return

        carry_before = congestion.carry_fixed
        numerator = (
            self.scene.congestion.ca_alpha_num * SCALE * SCALE + carry_before
        )
        denominator = self.scene.congestion.ca_alpha_den * congestion.cwnd_fixed
        increment, remainder = divmod(numerator, denominator)
        cwnd_before = congestion.cwnd_fixed
        congestion.cwnd_fixed += increment
        congestion.carry_fixed = remainder
        entry = {
            "seq": congestion.next_entry_seq,
            "kind": "congestion_avoidance_increment",
            "event_seq": event["seq"],
            "time_ms": event["time_ms"],
            "packet_space": packet.space,
            "packet_number": packet.number,
            "scale": SCALE,
            "alpha_num": self.scene.congestion.ca_alpha_num,
            "alpha_den": self.scene.congestion.ca_alpha_den,
            "numerator": numerator,
            "denominator": denominator,
            "quotient": increment,
            "remainder": remainder,
            "carry_before_fixed": carry_before,
            "carry_after_fixed": remainder,
            "cwnd_before_fixed": cwnd_before,
            "cwnd_after_fixed": congestion.cwnd_fixed,
            "rounding": "floor_toward_zero",
        }
        congestion.rounding_entries.append(entry)
        congestion.next_entry_seq += 1

    def _mark_acked(self, packet: Packet, event: dict[str, Any]) -> bool:
        if packet.status == "acked":
            self.state.counters["duplicate_acks"] = (
                self.state.counters.get("duplicate_acks", 0) + 1
            )
            return False
        if packet.status == "lost":
            self.state.counters["late_acks_after_loss"] = (
                self.state.counters.get("late_acks_after_loss", 0) + 1
            )
            return False
        if packet.status != "in_flight":
            return False
        packet.status = "acked"
        packet.acked_at_ms = event["time_ms"]
        packet.ack_event_seq = event["seq"]
        packet.transitions.append(
            {
                "status": "acked",
                "trigger": "ack",
                "event_seq": event["seq"],
                "time_ms": event["time_ms"],
            }
        )
        self.state.counters["packets_acked"] = (
            self.state.counters.get("packets_acked", 0) + 1
        )
        self._increase_cwnd(packet, event)
        return True

    def _handle_ack(self, event: dict[str, Any]) -> None:
        self._validate_ack_ranges(event)
        by_space: dict[str, list[list[int]]] = {name: [] for name in PACKET_SPACES}
        for item in event["ranges"]:
            by_space[item["space"]].append(
                [item["smallest"], item["largest"] + 1]
            )

        newly_acked: list[Packet] = []
        for space_name in PACKET_SPACES:
            incoming = merge_ranges(by_space[space_name])
            incoming_inclusive = [[start, end - 1] for start, end in incoming]
            if not incoming:
                continue
            space = self.state.spaces[space_name]
            for start, end_exclusive in incoming:
                for number in range(start, end_exclusive):
                    packet = space.packets.get(number)
                    if packet is None:
                        raise EngineError(
                            E_ACK_RANGE,
                            "ACK range contains a packet number that was never sent",
                            event_seq=event["seq"],
                            details={"space": space_name, "packet_number": number},
                        )
                    if self._mark_acked(packet, event):
                        newly_acked.append(packet)
            space.acknowledged_ranges = merge_inclusive_ranges(
                [*space.acknowledged_ranges, *incoming_inclusive]
            )
            largest = incoming_inclusive[-1][1]
            if largest > space.largest_acked:
                space.largest_acked = largest

        if not newly_acked:
            return

        rtt_packet = max(
            newly_acked,
            key=lambda packet: (
                packet.sent_at_ms,
                SPACE_ORDER[packet.space],
                packet.number,
            ),
        )
        self._update_rtt(rtt_packet, event)

        if self.state.probe.status == "active":
            self.state.probe.status = "acknowledged"
            self.state.probe.deadline_ms = None
            self.state.probe.backoff = 1
            self.state.probe.last_outcome = "acknowledged"
            self.state.probe.outcome_event_seq = event["seq"]

        for space_name in PACKET_SPACES:
            space = self.state.spaces[space_name]
            if not by_space[space_name] or space.largest_acked < 0:
                continue
            for number in sorted(space.packets):
                packet = space.packets[number]
                if (
                    packet.status == "in_flight"
                    and space.largest_acked - packet.number
                    >= self.scene.congestion.packet_threshold
                ):
                    self._mark_lost(
                        packet,
                        source="packet_threshold",
                        event=event,
                        detail={
                            "largest_acked": space.largest_acked,
                            "packet_threshold": self.scene.congestion.packet_threshold,
                        },
                    )

    def _mark_lost(
        self,
        packet: Packet,
        *,
        source: str,
        event: dict[str, Any],
        detail: dict[str, Any],
    ) -> bool:
        if packet.status != "in_flight":
            return False
        packet.status = "lost"
        packet.lost_at_ms = event["time_ms"]
        packet.lost_event_seq = event["seq"]
        packet.loss_source = source
        packet.transitions.append(
            {
                "status": "lost",
                "trigger": source,
                "event_seq": event["seq"],
                "time_ms": event["time_ms"],
                "detail": detail,
            }
        )
        for frame in packet.frames:
            if frame.get("type") != "STREAM":
                continue
            stream = self.state.streams[frame["stream_id"]]
            stream.mark_retransmit_needed(
                frame["offset"], frame["offset"] + frame["length"]
            )
        self.state.counters["packets_lost"] = (
            self.state.counters.get("packets_lost", 0) + 1
        )
        self.state.counters[f"loss_source_{source}"] = (
            self.state.counters.get(f"loss_source_{source}", 0) + 1
        )
        return True

    def _apply_loss_congestion(
        self,
        lost_packets: list[Packet],
        event: dict[str, Any],
    ) -> None:
        if not lost_packets:
            return
        congestion = self.state.congestion
        cwnd_before = congestion.cwnd_fixed
        ssthresh_before = congestion.ssthresh_fixed
        carry_before = congestion.carry_fixed
        reduced = (
            cwnd_before * self.scene.congestion.loss_reduction_num
        ) // self.scene.congestion.loss_reduction_den
        new_ssthresh = max(
            self.scene.congestion.min_cwnd_packets * SCALE, reduced
        )
        congestion.cwnd_fixed = new_ssthresh
        congestion.ssthresh_fixed = new_ssthresh
        congestion.carry_fixed = 0
        congestion.congestion_events.append(
            {
                "event_seq": event["seq"],
                "time_ms": event["time_ms"],
                "kind": "loss_reduction",
                "packets": [
                    {"space": packet.space, "number": packet.number}
                    for packet in sorted(
                        lost_packets, key=lambda item: (SPACE_ORDER[item.space], item.number)
                    )
                ],
                "sources": sorted({packet.loss_source or "unknown" for packet in lost_packets}),
                "cwnd_before_fixed": cwnd_before,
                "cwnd_after_fixed": congestion.cwnd_fixed,
                "ssthresh_before_fixed": ssthresh_before,
                "ssthresh_after_fixed": congestion.ssthresh_fixed,
                "reduction_num": self.scene.congestion.loss_reduction_num,
                "reduction_den": self.scene.congestion.loss_reduction_den,
                "carry_before_fixed": carry_before,
                "carry_after_fixed": 0,
            }
        )

    def _detect_persistent_congestion(self, event: dict[str, Any]) -> None:
        threshold = self.scene.congestion.persistent_threshold_ms
        if threshold <= 0:
            return
        for space_name in PACKET_SPACES:
            space = self.state.spaces[space_name]
            lost_numbers = sorted(
                number
                for number, packet in space.packets.items()
                if packet.status == "lost"
            )
            runs: list[list[int]] = []
            for number in lost_numbers:
                if not runs or number != runs[-1][-1] + 1:
                    runs.append([number])
                else:
                    runs[-1].append(number)
            for run in runs:
                if len(run) < 2:
                    continue
                first, last = run[0], run[-1]
                signature = f"{space_name}:{first}-{last}"
                if signature in self.state.persistent_signatures:
                    continue
                sent_times = [space.packets[number].sent_at_ms for number in run]
                if max(sent_times) - min(sent_times) < threshold:
                    continue
                self.state.persistent_signatures.append(signature)
                congestion = self.state.congestion
                cwnd_before = congestion.cwnd_fixed
                ssthresh_before = congestion.ssthresh_fixed
                new_ssthresh = max(
                    self.scene.congestion.min_cwnd_packets * SCALE,
                    (cwnd_before * self.scene.congestion.loss_reduction_num)
                    // self.scene.congestion.loss_reduction_den,
                )
                congestion.cwnd_fixed = (
                    self.scene.congestion.min_cwnd_packets * SCALE
                )
                congestion.ssthresh_fixed = new_ssthresh
                carriage = congestion.carry_fixed
                congestion.carry_fixed = 0
                congestion.congestion_events.append(
                    {
                        "event_seq": event["seq"],
                        "time_ms": event["time_ms"],
                        "kind": "persistent_congestion",
                        "signature": signature,
                        "threshold_ms": threshold,
                        "sent_time_span_ms": max(sent_times) - min(sent_times),
                        "cwnd_before_fixed": cwnd_before,
                        "cwnd_after_fixed": congestion.cwnd_fixed,
                        "ssthresh_before_fixed": ssthresh_before,
                        "ssthresh_after_fixed": congestion.ssthresh_fixed,
                        "carry_before_fixed": carriage,
                        "carry_after_fixed": 0,
                    }
                )

    def _detect_time_losses(self, event: dict[str, Any]) -> None:
        if self.state.connection.closed or self.state.connection.failure_code:
            return
        pto_ms = self.current_pto_ms()
        now = event["time_ms"]
        for packet in sorted(
            self.state.inflight_packets(),
            key=lambda item: (SPACE_ORDER[item.space], item.number),
        ):
            if now - packet.sent_at_ms > pto_ms:
                self._mark_lost(
                    packet,
                    source="time_threshold",
                    event=event,
                    detail={"pto_ms": pto_ms, "unacked_ms": now - packet.sent_at_ms},
                )

    def _handle_probe_start(self, event: dict[str, Any]) -> None:
        self._ensure_open(event)
        target = self._resolve_packet_ref(event)
        if target is None:
            inflight = sorted(
                self.state.inflight_packets(),
                key=lambda item: (SPACE_ORDER[item.space], item.number),
            )
            if not inflight:
                self.state.probe.status = "none"
                self.state.probe.last_outcome = "not_started_no_inflight_packet"
                self.state.probe.outcome_event_seq = event["seq"]
                return
            target = inflight[0]
        self.state.probe.status = "active"
        self.state.probe.backoff = 1
        self.state.probe.started_at_ms = event["time_ms"]
        self.state.probe.deadline_ms = event["time_ms"] + self.current_pto_ms()
        self.state.probe.target = {"space": target.space, "number": target.number}
        self.state.probe.last_outcome = "started"
        self.state.probe.outcome_event_seq = event["seq"]

    def _handle_probe_timeout(self, event: dict[str, Any]) -> None:
        self._resolve_packet_ref(event)
        probe = self.state.probe
        if probe.status != "active" or probe.deadline_ms is None:
            probe.last_outcome = "not_started"
            probe.outcome_event_seq = event["seq"]
            return
        if event["time_ms"] < probe.deadline_ms:
            probe.last_outcome = "not_due"
            probe.outcome_event_seq = event["seq"]
            return
        self.state.counters["probe_timeouts"] = (
            self.state.counters.get("probe_timeouts", 0) + 1
        )
        next_backoff = probe.backoff * 2
        if next_backoff > self.scene.congestion.max_backoff:
            probe.status = "failed"
            probe.deadline_ms = None
            probe.last_outcome = "backoff_limit_exceeded"
            probe.outcome_event_seq = event["seq"]
            self.state.connection.failure_code = E_PTO_BACKOFF_LIMIT
            self.state.connection.failure_detail = {
                "event_seq": event["seq"],
                "time_ms": event["time_ms"],
                "previous_backoff": probe.backoff,
                "requested_backoff": next_backoff,
                "max_backoff": self.scene.congestion.max_backoff,
            }
            self.state.counters["probe_backoff_limit_failures"] = (
                self.state.counters.get("probe_backoff_limit_failures", 0) + 1
            )
            return
        probe.backoff = next_backoff
        probe.status = "active"
        probe.deadline_ms = event["time_ms"] + self.current_pto_ms() * probe.backoff
        probe.last_outcome = "timed_out"
        probe.outcome_event_seq = event["seq"]

    def _handle_probe_abandon(self, event: dict[str, Any]) -> None:
        probe = self.state.probe
        if probe.status == "active":
            probe.last_outcome = "abandoned_after_start"
        else:
            probe.last_outcome = "abandoned_before_start"
        probe.status = "abandoned"
        probe.deadline_ms = None
        probe.backoff = 1
        probe.outcome_event_seq = event["seq"]

    def _handle_path_mtu(self, event: dict[str, Any]) -> None:
        self._ensure_open(event)
        if event["mtu"] <= self.scene.connection.packet_overhead_bytes:
            raise EngineError(
                E_BAD_SCENE,
                "path MTU must leave positive packet payload capacity",
                event_seq=event["seq"],
                details={
                    "mtu": event["mtu"],
                    "packet_overhead_bytes": self.scene.connection.packet_overhead_bytes,
                },
            )
        self.state.path_mtu = event["mtu"]

    def _handle_max_data(self, event: dict[str, Any]) -> None:
        self._ensure_open(event)
        if event["max_data"] < self.state.max_data:
            raise EngineError(
                E_FC_REGRESS,
                "MAX_DATA must not reduce the connection flow-control limit",
                event_seq=event["seq"],
                details={
                    "current_max_data": self.state.max_data,
                    "requested_max_data": event["max_data"],
                },
            )
        self.state.max_data = event["max_data"]

    def _handle_max_stream_data(self, event: dict[str, Any]) -> None:
        self._ensure_open(event)
        stream = self._ensure_stream(event["stream_id"], event["seq"])
        if event["max_stream_data"] < stream.max_stream_data:
            raise EngineError(
                E_FC_REGRESS,
                "MAX_STREAM_DATA must not reduce a stream flow-control limit",
                event_seq=event["seq"],
                details={
                    "stream_id": event["stream_id"],
                    "current_max_stream_data": stream.max_stream_data,
                    "requested_max_stream_data": event["max_stream_data"],
                },
            )
        stream.max_stream_data = event["max_stream_data"]

    def _handle_close(self, event: dict[str, Any]) -> None:
        self.state.connection.closed = True
        self.state.connection.close_error_code = event["error_code"]
        self.state.connection.close_reason = event.get("reason")
        if self.state.probe.status == "active":
            self.state.probe.status = "closed"
            self.state.probe.deadline_ms = None

    def _space_summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in PACKET_SPACES:
            space = self.state.spaces[name]
            inflight = [
                {
                    "number": packet.number,
                    "sent_at_ms": packet.sent_at_ms,
                    "frames": packet.frames,
                }
                for packet in sorted(
                    (
                        packet
                        for packet in space.packets.values()
                        if packet.status == "in_flight"
                    ),
                    key=lambda item: item.number,
                )
            ]
            result[name] = {
                "next_packet_number": space.next_pn,
                "max_sent_packet_number": space.max_sent_pn,
                "largest_acked_packet_number": space.largest_acked,
                "acknowledged_ranges": space.acknowledged_ranges,
                "inflight_count": len(inflight),
                "inflight": inflight,
                "packet_count": len(space.packets),
                "acked_count": sum(
                    1 for packet in space.packets.values() if packet.status == "acked"
                ),
                "lost_count": sum(
                    1 for packet in space.packets.values() if packet.status == "lost"
                ),
            }
        return result

    def _snapshot(
        self,
        event: dict[str, Any],
        observations: dict[str, Any],
    ) -> dict[str, Any]:
        plan = self._sendable_plan()
        congestion = self.state.congestion
        inflight = self.state.inflight_count()
        cwnd_packets_floor = congestion.cwnd_fixed // SCALE
        remaining_packets = max(0, cwnd_packets_floor - inflight)
        current_event_entries = [
            entry["seq"]
            for entry in congestion.rounding_entries
            if entry["event_seq"] == event["seq"]
        ]
        current_rounding_entries = [
            entry
            for entry in congestion.rounding_entries
            if entry["event_seq"] == event["seq"]
        ]
        current_congestion_events = [
            item
            for item in congestion.congestion_events
            if item["event_seq"] == event["seq"]
        ]
        streams = []
        for stream_plan in plan["streams"]:
            stream = self.state.streams[stream_plan["stream_id"]]
            streams.append(
                {
                    **stream_plan,
                    "max_stream_data": stream.max_stream_data,
                    "highest_sent_offset": stream.highest_sent_offset,
                    "flow_control_remaining_bytes": max(
                        0, stream.max_stream_data - stream.highest_sent_offset
                    ),
                    "write_ranges": stream.write_ranges,
                    "sent_ranges": stream.sent_ranges,
                    "retransmit_ranges": stream.retransmit_ranges,
                }
            )
        observations = dict(observations)
        observations["rounding_entry_seqs"] = current_event_entries
        snapshot = {
            "schema": "quicsim-trace-v1",
            "event_seq": event["seq"],
            "time_ms": event["time_ms"],
            "event_type": event["type"],
            "events_processed": self.state.events_processed,
            "event_observations": observations,
            "packet_spaces": self._space_summary(),
            "packets": [
                packet.to_dict()
                for name in PACKET_SPACES
                for packet in (
                    self.state.spaces[name].packets[number]
                    for number in sorted(self.state.spaces[name].packets)
                )
            ],
            "packet_digest": self.state.packet_digest(),
            "inflight_digest": self.state.inflight_digest(),
            "congestion": {
                "cwnd_fixed": congestion.cwnd_fixed,
                "cwnd_scale": SCALE,
                "cwnd_packets_floor": cwnd_packets_floor,
                "cwnd_packets_repr": f"{cwnd_packets_floor}.{congestion.cwnd_fixed % SCALE:06d}",
                "ssthresh_fixed": congestion.ssthresh_fixed,
                "ssthresh_packets_floor": congestion.ssthresh_fixed // SCALE,
                "carry_fixed": congestion.carry_fixed,
                "phase": (
                    "slow_start"
                    if congestion.cwnd_fixed < congestion.ssthresh_fixed
                    else "congestion_avoidance"
                ),
                "inflight_packets": inflight,
                "remaining_packets": remaining_packets,
                "remaining_fixed": max(
                    0, congestion.cwnd_fixed - inflight * SCALE
                ),
                "rounding_entry_count": len(congestion.rounding_entries),
                "congestion_event_count": len(congestion.congestion_events),
                "rounding_entries_for_event": current_rounding_entries,
                "congestion_events_for_event": current_congestion_events,
            },
            "flow_control": {
                "max_data": self.state.max_data,
                "used_bytes": self.state.connection_used_bytes(),
                "remaining_bytes": max(
                    0, self.state.max_data - self.state.connection_used_bytes()
                ),
                "streams": streams,
            },
            "sendable": {
                "sendable_packets": plan["sendable_packets"],
                "sendable_bytes": plan["sendable_bytes"],
                "flow_controlled_new_bytes": plan["flow_controlled_new_bytes"],
                "already_consumed_bytes": plan["already_consumed_bytes"],
                "cwnd_remaining_packets": plan["cwnd_remaining_packets"],
                "connection_remaining_bytes": plan["connection_remaining_bytes"],
                "limiters": plan["limiters"],
                "stream_ranges": [
                    {
                        "stream_id": item["stream_id"],
                        "ranges": item["sendable_ranges"],
                        "bytes": item["sendable_bytes"],
                    }
                    for item in plan["streams"]
                ],
            },
            "rtt": {
                "srtt_ms": self.state.srtt_ms,
                "rttvar_ms": self.state.rttvar_ms,
                "latest_rtt_ms": self.state.latest_rtt_ms,
                "latest_ack_delay_ms": self.state.latest_ack_delay_ms,
                "pto_ms": self.current_pto_ms(),
            },
            "probe": self.state.probe.to_dict(),
            "connection": {
                **self.state.connection.to_dict(),
                "status": (
                    "failed"
                    if self.state.connection.failure_code
                    else "closed"
                    if self.state.connection.closed
                    else "open"
                ),
            },
            "counters": dict(sorted(self.state.counters.items())),
        }
        return json.loads(canonical_json(snapshot))

    def apply_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self.state.time_ms = event["time_ms"]
        observations: dict[str, Any] = {
            "event": event,
            "ack_ranges": None,
            "ack_received_at_ms": None,
            "ack_delay_ms": None,
            "losses": [],
        }

        if (
            self.state.connection.failure_code
            and event["type"] != "connection_close"
        ):
            observations["skipped_due_to_failure"] = self.state.connection.failure_code
            self.state.events_processed = event["seq"]
            return self._snapshot(event, observations)

        before_packet_status = {
            (packet.space, packet.number): packet.status
            for space in self.state.spaces.values()
            for packet in space.packets.values()
        }
        event_type = event["type"]
        if event_type == "write_stream":
            self._handle_write(event)
        elif event_type == "send":
            self._handle_send(event)
        elif event_type == "ack":
            by_space: dict[str, list[list[int]]] = {
                name: [] for name in PACKET_SPACES
            }
            for item in event["ranges"]:
                by_space[item["space"]].append([item["smallest"], item["largest"]])
            observations["ack_ranges"] = {
                name: merge_inclusive_ranges(
                    (start, end - 1) for start, end in ranges
                )
                for name, ranges in by_space.items()
                if ranges
            }
            observations["ack_received_at_ms"] = event["received_at_ms"]
            observations["ack_delay_ms"] = event["ack_delay_ms"]
            self._handle_ack(event)
        elif event_type == "start_probe":
            self._handle_probe_start(event)
        elif event_type == "probe_timeout":
            self._handle_probe_timeout(event)
        elif event_type == "abandon_probe":
            self._handle_probe_abandon(event)
        elif event_type == "path_mtu":
            self._handle_path_mtu(event)
        elif event_type == "max_data":
            self._handle_max_data(event)
        elif event_type == "max_stream_data":
            self._handle_max_stream_data(event)
        elif event_type == "connection_close":
            self._handle_close(event)
        else:
            raise EngineError(
                E_BAD_SCENE,
                f"unknown event type: {event_type}",
                event_seq=event["seq"],
            )

        self._detect_time_losses(event)

        lost_now = [
            packet
            for space in self.state.spaces.values()
            for packet in space.packets.values()
            if before_packet_status.get((packet.space, packet.number)) == "in_flight"
            and packet.status == "lost"
        ]
        if lost_now:
            observations["losses"] = [
                {
                    "space": packet.space,
                    "number": packet.number,
                    "source": packet.loss_source,
                    "event_seq": packet.lost_event_seq,
                    "time_ms": packet.lost_at_ms,
                }
                for packet in sorted(
                    lost_now,
                    key=lambda item: (SPACE_ORDER[item.space], item.number),
                )
            ]
            self._apply_loss_congestion(lost_now, event)
            self._detect_persistent_congestion(event)

        self.state.events_processed = event["seq"]
        return self._snapshot(event, observations)


def simulate_scene(scene: Scene) -> list[dict[str, Any]]:
    engine = Engine(scene)
    return [engine.apply_event(event) for event in scene.events]


def recompute_prefix(scene: Scene, event_index: int) -> tuple[EngineState, dict[str, Any]]:
    engine = Engine(scene)
    snapshot: dict[str, Any] | None = None
    for index in range(event_index + 1):
        snapshot = engine.apply_event(scene.events[index])
    if snapshot is None:
        raise EngineError(E_BAD_SCENE, "cannot recompute an empty event prefix")
    return engine.state, snapshot


def diff_snapshots(
    incremental: dict[str, Any], full: dict[str, Any], path: str = ""
) -> dict[str, Any] | None:
    if type(incremental) is not type(full):
        return {
            "field": path or "$",
            "incremental_value": incremental,
            "full_value": full,
        }
    if isinstance(incremental, dict):
        keys = sorted(set(incremental) | set(full))
        for key in keys:
            if key not in incremental:
                return {
                    "field": f"{path}.{key}" if path else key,
                    "incremental_value": "<missing>",
                    "full_value": full[key],
                }
            if key not in full:
                return {
                    "field": f"{path}.{key}" if path else key,
                    "incremental_value": incremental[key],
                    "full_value": "<missing>",
                }
            difference = diff_snapshots(
                incremental[key], full[key], f"{path}.{key}" if path else key
            )
            if difference is not None:
                return difference
        return None
    if isinstance(incremental, list):
        if len(incremental) != len(full):
            return {
                "field": f"{path}.length" if path else "length",
                "incremental_value": len(incremental),
                "full_value": len(full),
            }
        for index, (left, right) in enumerate(zip(incremental, full)):
            difference = diff_snapshots(
                left, right, f"{path}[{index}]" if path else f"[{index}]"
            )
            if difference is not None:
                return difference
        return None
    if incremental != full:
        return {
            "field": path or "$",
            "incremental_value": incremental,
            "full_value": full,
        }
    return None
