from __future__ import annotations

from quicsim.engine import Engine, simulate_scene

from .helpers import event, make_scene


def test_cross_space_ack_ranges_are_split_merged_and_deduplicated():
    scene = make_scene(
        "cross-space",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=4000),
            event(2, 1, "send", space="application", count=3),
            event(
                3,
                2,
                "send",
                space="initial",
                count=1,
                ack_ranges=[
                    {"space": "initial", "smallest": 0, "largest": 0}
                ],
            ),
            event(
                4,
                3,
                "send",
                space="handshake",
                count=1,
                ack_ranges=[
                    {"space": "handshake", "smallest": 0, "largest": 0}
                ],
            ),
            event(
                5,
                4,
                "ack",
                ranges=[
                    {"space": "application", "smallest": 0, "largest": 1},
                    {"space": "application", "smallest": 1, "largest": 1},
                    {"space": "initial", "smallest": 0, "largest": 0},
                    {"space": "handshake", "smallest": 0, "largest": 0},
                    {"space": "application", "smallest": 0, "largest": 0},
                ],
            ),
        ],
    )
    snapshots = simulate_scene(scene)
    final = snapshots[-1]
    assert final["packet_spaces"]["application"]["max_sent_packet_number"] == 2
    assert final["packet_spaces"]["initial"]["max_sent_packet_number"] == 0
    assert final["packet_spaces"]["handshake"]["max_sent_packet_number"] == 0
    assert (
        final["packet_spaces"]["application"]["acknowledged_ranges"] == [[0, 1]]
    )
    assert final["packet_spaces"]["initial"]["acknowledged_ranges"] == [[0, 0]]
    assert final["packet_spaces"]["handshake"]["acknowledged_ranges"] == [[0, 0]]
    assert final["counters"]["packets_acked"] == 4
    app_packets = [
        packet for packet in final["packets"] if packet["space"] == "application"
    ]
    assert [packet["status"] for packet in app_packets] == [
        "acked",
        "acked",
        "in_flight",
    ]
    for packet in app_packets[:2]:
        assert packet["transitions"][-1] == {
            "status": "acked",
            "trigger": "ack",
            "event_seq": 5,
            "time_ms": 4,
        }


def test_acked_packets_never_enter_loss_detection():
    scene = make_scene(
        "acked-not-lost",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=10000),
            event(2, 0, "send", space="application", count=6),
            event(
                3,
                1,
                "ack",
                ranges=[{"space": "application", "smallest": 5, "largest": 5}],
            ),
        ],
        congestion={"packet_threshold": 3, "initial_cwnd_packets": 10},
    )
    final = simulate_scene(scene)[-1]
    statuses = {
        packet["number"]: packet["status"]
        for packet in final["packets"]
        if packet["space"] == "application"
    }
    assert statuses[5] == "acked"
    assert statuses[0] == "lost"
    assert statuses[1] == "lost"
    assert statuses[2] == "lost"
    assert statuses[3] == "in_flight"
    assert statuses[4] == "in_flight"
    assert final["counters"]["packets_lost"] == 3
    for packet in final["packets"]:
        lost_transitions = [
            transition
            for transition in packet["transitions"]
            if transition["status"] == "lost"
        ]
        assert len(lost_transitions) <= 1


def test_packet_state_can_be_reconstructed_from_transitions():
    scene = make_scene(
        "transition-replay",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=3000),
            event(2, 1, "send", space="application", count=3),
            event(
                3,
                2,
                "ack",
                ranges=[{"space": "application", "smallest": 0, "largest": 0}],
            ),
        ],
    )
    engine = Engine(scene)
    for item in scene.events:
        snapshot = engine.apply_event(item)
    for packet in snapshot["packets"]:
        rebuilt_status = packet["transitions"][-1]["status"]
        assert rebuilt_status == packet["status"]
        assert packet["transitions"][0]["event_seq"] == packet["sent_event_seq"]

