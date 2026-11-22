from __future__ import annotations

import json

from quicsim.engine import simulate_scene
from quicsim.model import SCALE
from quicsim.scene import canonical_json

from .helpers import event, make_scene


def test_congestion_avoidance_preserves_rounding_carry():
    packet_count = 40
    events = [
        event(
            1,
            0,
            "write_stream",
            stream_id=0,
            offset=0,
            length=44000,
        ),
        event(2, 0, "send", space="application", count=packet_count),
    ]
    seq = 3
    for number in range(packet_count):
        events.append(
            event(
                seq,
                2,
                "ack",
                ranges=[
                    {
                        "space": "application",
                        "smallest": number,
                        "largest": number,
                    }
                ],
            )
        )
        seq += 1
    scene = make_scene(
        "carry",
        events,
        connection={"initial_max_data": 100000, "initial_max_stream_data": 50000},
        congestion={
            "initial_cwnd_packets": 50,
            "initial_ssthresh_packets": 40,
            "packet_threshold": 1000,
            "congestion_avoidance": {"alpha_num": 1, "alpha_den": 1},
            "pto": {"initial_rtt_ms": 10000},
        },
    )
    first = simulate_scene(scene)
    second = simulate_scene(scene)
    assert canonical_json(first) == canonical_json(second)

    final = first[-1]
    entries = final["event_observations"]["rounding_entry_seqs"]
    assert len(entries) == 1
    assert final["congestion"]["rounding_entry_count"] == packet_count

    state = None
    engine = None
    from quicsim.engine import Engine

    engine = Engine(scene)
    for item in scene.events:
        snapshot = engine.apply_event(item)
    ledger = engine.state.congestion.rounding_entries
    assert len(ledger) == packet_count
    for index, entry in enumerate(ledger):
        assert entry["rounding"] == "floor_toward_zero"
        assert entry["scale"] == SCALE
        assert entry["remainder"] == entry["carry_after_fixed"]
        if index:
            assert entry["carry_before_fixed"] == ledger[index - 1]["remainder"]
            assert entry["cwnd_before_fixed"] == ledger[index - 1]["cwnd_after_fixed"]
    expected = (
        scene.congestion.initial_cwnd_packets * SCALE
        + sum(entry["quotient"] for entry in ledger)
    )
    assert engine.state.congestion.cwnd_fixed == expected


def test_flow_control_and_cwnd_are_joint_but_do_not_roll_back_each_other():
    scene = make_scene(
        "joint",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=2000),
            event(2, 1, "send", space="application", count=10),
            event(
                3,
                2,
                "ack",
                ranges=[{"space": "application", "smallest": 0, "largest": 0}],
            ),
            event(4, 3, "max_data", max_data=2000),
            event(
                5,
                4,
                "max_stream_data",
                stream_id=0,
                max_stream_data=2000,
            ),
            event(6, 5, "send", space="application", count=10),
        ],
        connection={
            "initial_max_data": 1000,
            "initial_max_stream_data": 1000,
            "initial_max_streams": 2,
        },
        congestion={"initial_cwnd_packets": 10, "initial_ssthresh_packets": 20},
    )
    snapshots = simulate_scene(scene)
    after_first_send = snapshots[1]
    assert after_first_send["sendable"]["sendable_bytes"] == 0
    assert after_first_send["flow_control"]["remaining_bytes"] == 0
    assert after_first_send["congestion"]["cwnd_fixed"] == 10 * SCALE
    assert after_first_send["congestion"]["inflight_packets"] == 1

    after_ack = snapshots[2]
    cwnd_after_ack = after_ack["congestion"]["cwnd_fixed"]
    assert cwnd_after_ack == 11 * SCALE
    assert after_ack["flow_control"]["remaining_bytes"] == 0

    before_resend = snapshots[4]
    assert before_resend["congestion"]["cwnd_fixed"] == cwnd_after_ack
    assert before_resend["sendable"]["sendable_packets"] == 1
    assert before_resend["sendable"]["sendable_bytes"] == 1000
    assert before_resend["sendable"]["flow_controlled_new_bytes"] == 1000
    assert (
        before_resend["sendable"]["flow_controlled_new_bytes"]
        <= before_resend["flow_control"]["remaining_bytes"]
    )
    assert before_resend["sendable"]["cwnd_remaining_packets"] == 11

    after_resend = snapshots[5]
    assert after_resend["congestion"]["cwnd_fixed"] == cwnd_after_ack
    assert after_resend["flow_control"]["remaining_bytes"] == 0
    assert after_resend["congestion"]["inflight_packets"] == 1


def test_sendable_output_is_stably_sorted():
    scene = make_scene(
        "stable-order",
        [
            event(1, 0, "write_stream", stream_id=3, offset=0, length=200),
            event(2, 0, "write_stream", stream_id=1, offset=0, length=200),
            event(3, 0, "write_stream", stream_id=2, offset=0, length=200),
        ],
    )
    final = simulate_scene(scene)[-1]
    assert [item["stream_id"] for item in final["flow_control"]["streams"]] == [
        1,
        2,
        3,
    ]
    assert [item["stream_id"] for item in final["sendable"]["stream_ranges"]] == [
        1,
        2,
        3,
    ]
    first = json.dumps(final, sort_keys=True)
    second = json.dumps(simulate_scene(scene)[-1], sort_keys=True)
    assert first == second

