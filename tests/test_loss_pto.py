from __future__ import annotations

from quicsim.engine import simulate_scene
from quicsim.errors import E_PTO_BACKOFF_LIMIT
from quicsim.model import SCALE

from .helpers import event, make_scene


def test_packet_and_time_loss_sources_deduplicate():
    scene = make_scene(
        "loss-dedupe",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=10000),
            event(2, 0, "send", space="application", count=6),
            event(
                3,
                1,
                "ack",
                ranges=[{"space": "application", "smallest": 5, "largest": 5}],
            ),
            event(4, 1000, "max_data", max_data=110000),
        ],
        congestion={
            "packet_threshold": 3,
            "initial_cwnd_packets": 10,
            "pto": {"initial_rtt_ms": 50, "min_rtt_ms": 5},
        },
    )
    snapshots = simulate_scene(scene)
    packet_loss_event = snapshots[2]
    assert [
        (loss["number"], loss["source"])
        for loss in packet_loss_event["event_observations"]["losses"]
    ] == [(0, "packet_threshold"), (1, "packet_threshold"), (2, "packet_threshold")]
    time_loss_event = snapshots[3]
    assert [
        (loss["number"], loss["source"])
        for loss in time_loss_event["event_observations"]["losses"]
    ] == [(3, "time_threshold"), (4, "time_threshold")]
    for packet in time_loss_event["packets"]:
        assert sum(
            1
            for transition in packet["transitions"]
            if transition["status"] == "lost"
        ) == (1 if packet["number"] <= 4 else 0)


def test_persistent_congestion_reduces_to_minimum_window():
    scene = make_scene(
        "persistent",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=500),
            event(2, 0, "send", space="application", count=1),
            event(3, 50, "write_stream", stream_id=0, offset=500, length=500),
            event(4, 50, "send", space="application", count=1),
            event(5, 100, "write_stream", stream_id=0, offset=1000, length=500),
            event(6, 100, "send", space="application", count=1),
            event(7, 150, "write_stream", stream_id=0, offset=1500, length=500),
            event(8, 150, "send", space="application", count=1),
            event(
                9,
                151,
                "ack",
                ranges=[{"space": "application", "smallest": 3, "largest": 3}],
            ),
        ],
        congestion={
            "initial_cwnd_packets": 10,
            "initial_ssthresh_packets": 20,
            "min_cwnd_packets": 2,
            "packet_threshold": 3,
            "persistent_congestion": {
                "threshold_ms": 100,
                "loss_reduction_num": 1,
                "loss_reduction_den": 2,
            },
            "pto": {"initial_rtt_ms": 5000},
        },
    )
    final = simulate_scene(scene)[-1]
    assert final["congestion"]["cwnd_fixed"] == 2 * SCALE
    assert final["congestion"]["congestion_event_count"] >= 2
    engine = None
    from quicsim.engine import Engine

    engine = Engine(scene)
    for item in scene.events:
        snapshot = engine.apply_event(item)
    kinds = [
        item["kind"] for item in engine.state.congestion.congestion_events
    ]
    assert "loss_reduction" in kinds
    assert "persistent_congestion" in kinds


def test_delayed_ack_extends_pto_and_does_not_trigger_early():
    scene = make_scene(
        "delayed-ack",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=3000),
            event(2, 0, "send", space="application", count=2),
            event(
                3,
                10,
                "ack",
                ack_delay_ms=30,
                received_at_ms=10,
                ranges=[{"space": "application", "smallest": 0, "largest": 0}],
            ),
            event(4, 70, "max_data", max_data=110000),
            event(5, 75, "max_data", max_data=120000),
        ],
        connection={"ack_delay_factor": 2},
        congestion={
            "initial_cwnd_packets": 10,
            "packet_threshold": 1000,
            "pto": {"initial_rtt_ms": 100, "min_rtt_ms": 10, "granularity_ms": 1},
        },
    )
    snapshots = simulate_scene(scene)
    assert snapshots[2]["rtt"]["pto_ms"] == 74
    assert snapshots[3]["event_observations"]["losses"] == []
    packet1 = next(packet for packet in snapshots[3]["packets"] if packet["number"] == 1)
    assert packet1["status"] == "in_flight"
    assert [
        (loss["number"], loss["source"])
        for loss in snapshots[4]["event_observations"]["losses"]
    ] == [(1, "time_threshold")]


def test_probe_outcomes_and_backoff_limit():
    scene = make_scene(
        "pto-limit",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=1000),
            event(2, 0, "send", space="application", count=1),
            event(3, 1, "abandon_probe"),
            event(4, 2, "probe_timeout"),
            event(5, 3, "start_probe", packet={"space": "application", "number": 0}),
            event(6, 10000, "probe_timeout"),
            event(7, 20000, "probe_timeout"),
            event(8, 20001, "connection_close", error_code=0),
        ],
        congestion={
            "packet_threshold": 1000,
            "pto": {"initial_rtt_ms": 100, "min_rtt_ms": 10, "max_backoff": 2},
        },
    )
    snapshots = simulate_scene(scene)
    assert snapshots[2]["probe"]["last_outcome"] == "abandoned_before_start"
    assert snapshots[3]["probe"]["last_outcome"] == "not_started"
    assert snapshots[3]["counters"].get("probe_timeouts", 0) == 0
    assert snapshots[5]["probe"]["last_outcome"] == "timed_out"
    assert snapshots[5]["probe"]["backoff"] == 2
    assert snapshots[6]["connection"]["failure_code"] == E_PTO_BACKOFF_LIMIT
    assert snapshots[6]["connection"]["status"] == "failed"
    assert snapshots[6]["probe"]["last_outcome"] == "backoff_limit_exceeded"

