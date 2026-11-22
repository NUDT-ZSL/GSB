from __future__ import annotations

from dataclasses import replace

from flightfusion.models import SensorSequence
from flightfusion.preprocess import (
    preprocess_sensor,
    target_times,
    time_grid_digest,
)
from flightfusion.serialization import canonical_bytes


def test_preprocess_is_byte_deterministic(state):
    target = target_times(state.dataset, 100.0)
    sensor = state.dataset.sequences["mag"]
    description = state.descriptions["mag"]
    first = preprocess_sensor(sensor, description, target)
    second = preprocess_sensor(sensor, description, target)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["alignment_indices"] == second["alignment_indices"]
    assert time_grid_digest(target) == first["grid_digest"]


def test_axis_reorder_and_calibration_are_applied(state):
    description = state.descriptions["accel"]
    # Raw values [4, 2, 1] with order y,x,z become canonical [2, 4, 1].
    sequence = SensorSequence(
        sensor_id="accel",
        sensor_type="accel",
        sample_rate=1.0,
        times=(0.0,),
        values=((4.0, 2.0, 1.0),),
    )
    payload = preprocess_sensor(sequence, description, (0.0,))
    expected = (
        2.0 * description.scale[0] + description.bias[0],
        4.0 * description.scale[1] + description.bias[1],
        1.0 * description.scale[2] + description.bias[2],
    )
    assert payload["values"][0] == [expected[0], expected[1], expected[2]]
    assert payload["axis_mapping"] == {"x": 1, "y": 0, "z": 2}


def test_duplicate_timestamp_tie_uses_lowest_sample_index(state):
    sequence = SensorSequence(
        sensor_id="gyro",
        sensor_type="gyro",
        sample_rate=10.0,
        times=(0.1, 0.0, 0.1, 0.2),
        values=((3.0, 3.0, 3.0), (1.0, 1.0, 1.0), (2.0, 2.0, 2.0), (4.0, 4.0, 4.0)),
    )
    payload = preprocess_sensor(
        sequence,
        state.descriptions["gyro"],
        (0.1,),
    )
    # Duplicate 0.1 has sample indices 0 and 2; the lowest index wins.
    assert payload["alignment_indices"] == [0]


def test_sample_rate_metadata_uses_degraded_rate(state):
    description = replace(state.descriptions["accel"], sample_rate=0.0)
    sequence = state.dataset.sequences["accel"]
    target = target_times(state.dataset, 100.0)
    payload = preprocess_sensor(
        sequence,
        description,
        target,
        effective_sample_rate=sequence.sample_rate,
    )
    assert payload["sample_rate"] == sequence.sample_rate

