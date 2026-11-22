from __future__ import annotations

import hashlib
import json

from h2diag.engine import Engine
from h2diag.scheduler import compute_ranks
from h2diag.validation import normalize_scenario


def test_sample_reaches_declared_final_states(main_scenario: dict) -> None:
    scenario = normalize_scenario(main_scenario)
    state = Engine(scenario).run_all()
    assert {key: value["state"] for key, value in state["streams"].items()} == {
        "1": "closed",
        "3": "closed",
        "5": "closed",
    }
    assert state["connection"]["send_window"] == 440
    assert state["connection"]["recv_window"] == 560
    assert state["streams"]["3"]["reset"] is True
    assert state["streams"]["3"]["reclaimed_to_connection"] == 12
    assert state["streams"]["3"]["send_window"] == 0
    assert state["streams"]["3"]["recv_window"] == 0


def test_header_reassembly_matches_direct_concatenation(main_scenario: dict) -> None:
    scenario = normalize_scenario(main_scenario)
    state = Engine(scenario).run_all()
    block = state["headers"]["history"][0]
    direct = bytes.fromhex("".join(block["fragments_hex"]))
    assert direct.hex() == block["block_hex"]
    assert block["digest"] == "sha256:" + hashlib.sha256(direct).hexdigest()
    assert block["size"] == len(direct)
    assert block["start_seq"] == 3 and block["end_seq"] == 4


def test_weighted_rank_is_stable_and_deterministic(main_scenario: dict) -> None:
    scenario = normalize_scenario(main_scenario)
    engine = Engine(scenario)
    state = engine.run_events(scenario["events"][:6])
    ranks = compute_ranks(state["streams"], state["connection"])
    assert [item["stream_id"] for item in ranks] == [1, 3, 5]
    assert [item["weight"] for item in ranks] == [32, 16, 8]


def test_two_layer_window_never_goes_below_zero(main_scenario: dict) -> None:
    scenario = normalize_scenario(main_scenario)
    state = Engine(scenario).run_all()
    assert state["connection"]["send_window"] >= 0
    assert state["connection"]["recv_window"] >= 0
    for stream in state["streams"].values():
        assert stream["send_window"] >= 0
        assert stream["recv_window"] >= 0


def test_snapshot_json_is_stable(main_scenario: dict) -> None:
    scenario = normalize_scenario(main_scenario)
    first = Engine(scenario).run_all()
    second = Engine(normalize_scenario(json.loads(json.dumps(main_scenario)))).run_all()
    assert first["state_digest"] == second["state_digest"]


def test_rst_stream_reclaims_unacknowledged_credit() -> None:
    scenario = normalize_scenario(
        {
            "format": "h2diag.scenario/v1",
            "scenario_id": "rst-refund",
            "connection_id": "rst-refund",
            "connection": {
                "initial_send_window": 100,
                "initial_recv_window": 100,
                "max_send_window": 200,
                "max_recv_window": 200,
                "settings": {"initial_window_size": 100},
            },
            "streams": [
                {
                    "stream_id": 1,
                    "initiator": "client",
                    "initial_send_window": 100,
                    "initial_recv_window": 100,
                    "weight": 16,
                    "expected_final_state": "closed",
                }
            ],
            "events": [
                {
                    "seq": 1,
                    "clock_ms": 0,
                    "type": "HEADERS",
                    "direction": "send",
                    "stream_id": 1,
                    "end_headers": True,
                    "fragment_hex": "00",
                },
                {
                    "seq": 2,
                    "clock_ms": 1,
                    "type": "DATA",
                    "direction": "send",
                    "stream_id": 1,
                    "length": 40,
                },
                {
                    "seq": 3,
                    "clock_ms": 2,
                    "type": "RST_STREAM",
                    "direction": "recv",
                    "stream_id": 1,
                    "error_code": 8,
                },
            ],
        }
    )
    state = Engine(scenario).run_all()
    assert state["connection"]["send_window"] == 100
    assert state["connection"]["send_in_flight"] == 0
    assert state["streams"]["1"]["send_window"] == 0
    assert state["streams"]["1"]["reset"] is True
