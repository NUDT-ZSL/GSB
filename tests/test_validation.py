from __future__ import annotations

import json
from pathlib import Path

import pytest

from quicsim.cli import main
from quicsim.engine import simulate_scene
from quicsim.errors import (
    E_ACK_RANGE,
    E_BAD_SCENE,
    E_FC_REGRESS,
    E_PTO_UNSENT,
    EngineError,
)
from quicsim.scene import parse_scene

from .helpers import event, make_scene


def _raw_scene(events, *, connection=None, congestion=None):
    scene = make_scene("invalid", events, connection=connection, congestion=congestion)
    return scene.to_source_dict()


@pytest.mark.parametrize(
    ("connection", "expected_code"),
    [
        ({"initial_max_data": 0}, E_BAD_SCENE),
        ({"initial_max_stream_data": 0}, E_BAD_SCENE),
        ({"max_udp_payload_size": 1199}, E_BAD_SCENE),
        ({"max_udp_payload_size": 65528}, E_BAD_SCENE),
    ],
)
def test_connection_parameter_validation(connection, expected_code):
    raw = _raw_scene([event(1, 0, "write_stream", stream_id=0, offset=0, length=1)])
    raw["connection"].update(connection)
    with pytest.raises(EngineError) as error:
        parse_scene(raw)
    assert error.value.code == expected_code


def test_ack_range_lower_above_upper_is_rejected():
    raw = _raw_scene(
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=100),
            event(2, 0, "send", space="application", count=1),
            event(
                3,
                1,
                "ack",
                ranges=[{"space": "application", "smallest": 0, "largest": 0}],
            ),
        ]
    )
    raw["events"][2]["ranges"][0]["smallest"] = 2
    raw["events"][2]["ranges"][0]["largest"] = 1
    with pytest.raises(EngineError) as error:
        parse_scene(raw)
    assert error.value.code == E_ACK_RANGE


def test_ack_range_beyond_sent_packet_is_rejected():
    scene = make_scene(
        "ack-future",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=100),
            event(2, 0, "send", space="application", count=1),
            event(
                3,
                1,
                "ack",
                ranges=[{"space": "application", "smallest": 0, "largest": 1}],
            ),
        ],
    )
    with pytest.raises(EngineError) as error:
        simulate_scene(scene)
    assert error.value.code == E_ACK_RANGE
    assert error.value.details["max_sent_packet_number"] == 0


def test_probe_cannot_reference_unsent_packet():
    scene = make_scene(
        "pto-unsent",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=100),
            event(2, 0, "send", space="application", count=1),
            event(
                3,
                1,
                "start_probe",
                packet={"space": "application", "number": 4},
            ),
        ],
    )
    with pytest.raises(EngineError) as error:
        simulate_scene(scene)
    assert error.value.code == E_PTO_UNSENT


def test_max_data_regression_is_rejected():
    scene = make_scene(
        "fc-regress",
        [
            event(1, 0, "max_data", max_data=500),
        ],
        connection={"initial_max_data": 1000},
    )
    with pytest.raises(EngineError) as error:
        simulate_scene(scene)
    assert error.value.code == E_FC_REGRESS


def test_max_stream_data_regression_is_rejected():
    scene = make_scene(
        "stream-fc-regress",
        [
            event(1, 0, "write_stream", stream_id=0, offset=0, length=100),
            event(2, 0, "max_stream_data", stream_id=0, max_stream_data=50),
        ],
        connection={"initial_max_stream_data": 100},
    )
    with pytest.raises(EngineError) as error:
        simulate_scene(scene)
    assert error.value.code == E_FC_REGRESS


def test_invalid_scene_does_not_create_database(tmp_path: Path):
    scene_path = tmp_path / "invalid.json"
    db_path = tmp_path / "state" / "invalid.sqlite"
    scene_path.write_text(
        json.dumps(
            _raw_scene(
                [
                    event(1, 0, "write_stream", stream_id=0, offset=0, length=100),
                    event(2, 0, "send", space="application", count=1),
                    event(
                        3,
                        1,
                        "ack",
                        ranges=[
                            {
                                "space": "application",
                                "smallest": 0,
                                "largest": 2,
                            }
                        ],
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )
    assert main(["run", "--scene", str(scene_path), "--db", str(db_path)]) == 2
    assert not db_path.exists()
    assert not Path(f"{db_path}.lock").exists()

