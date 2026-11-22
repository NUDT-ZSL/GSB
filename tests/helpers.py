from __future__ import annotations

import copy
from typing import Any

from quicsim.scene import parse_scene


BASE_CONNECTION: dict[str, Any] = {
    "initial_max_data": 100000,
    "initial_max_streams": 8,
    "initial_max_stream_data": 50000,
    "max_udp_payload_size": 1200,
    "packet_overhead_bytes": 100,
    "ack_delay_factor": 1,
}

BASE_CONGESTION: dict[str, Any] = {
    "initial_cwnd_packets": 10,
    "initial_ssthresh_packets": 20,
    "min_cwnd_packets": 2,
    "packet_threshold": 1000,
    "congestion_avoidance": {"alpha_num": 1, "alpha_den": 1},
    "persistent_congestion": {
        "threshold_ms": 0,
        "loss_reduction_num": 1,
        "loss_reduction_den": 2,
    },
    "pto": {
        "initial_rtt_ms": 100,
        "min_rtt_ms": 10,
        "granularity_ms": 1,
        "max_backoff": 8,
    },
}


def event(seq: int, time_ms: int, event_type: str, **kwargs: Any) -> dict[str, Any]:
    return {"seq": seq, "time_ms": time_ms, "type": event_type, **kwargs}


def make_scene(
    scene_id: str,
    events: list[dict[str, Any]],
    *,
    connection: dict[str, Any] | None = None,
    congestion: dict[str, Any] | None = None,
    observations: dict[str, Any] | None = None,
):
    connection_obj = copy.deepcopy(BASE_CONNECTION)
    if connection:
        connection_obj.update(connection)
    congestion_obj = copy.deepcopy(BASE_CONGESTION)
    if congestion:
        for key, value in congestion.items():
            if isinstance(value, dict) and isinstance(congestion_obj.get(key), dict):
                congestion_obj[key].update(value)
            else:
                congestion_obj[key] = value
    return parse_scene(
        {
            "scene_id": scene_id,
            "connection": connection_obj,
            "congestion": congestion_obj,
            "observations": observations or {"snapshot_every_event": True},
            "events": events,
        }
    )

