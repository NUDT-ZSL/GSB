from __future__ import annotations

from dataclasses import replace

from flightfusion.graph import FusionGraph
from flightfusion.serialization import canonical_bytes


def test_fusion_is_deterministic_and_reasonable(state, cache_dir):
    graph = FusionGraph(cache_dir)
    first = graph.build(state, force_recompute=True)
    second = graph.build(state, force_recompute=True)
    assert first.output_bytes() == second.output_bytes()
    assert first.output_bytes() == canonical_bytes(first.output)
    assert first.output["stats"]["attitude_error_deg_mean"] < 2.0
    assert first.output["stats"]["attitude_error_deg_max"] < 3.0
    assert first.output["stats"]["fusion_residual_mean"] < 0.001
    assert first.output["sensor_versions"]["gyro"]["description_version"] == "v1"
    assert first.output["sensor_versions"]["accel"]["description_version"] == "v2"
    assert first.output["sensor_versions"]["mag"]["description_version"] == "v3"
    assert first.output["config_version"] == state.fusion_config.config_version


def test_output_contains_quaternion_euler_and_residual(state, cache_dir):
    output = FusionGraph(cache_dir).build(state).output
    assert len(output["quaternions"]) == len(output["euler_deg"])
    assert len(output["residual"]) == len(output["times"])
    for quaternion in output["quaternions"]:
        norm = sum(item * item for item in quaternion) ** 0.5
        assert abs(norm - 1.0) < 1e-10


def test_config_gain_and_weights_affect_config_hash_but_not_description_versions(
    state, cache_dir
):
    graph = FusionGraph(cache_dir)
    base = graph.build(state)
    changed = graph.build(
        state.replace_config(
            replace(state.fusion_config, config_version="fusion-custom")
        )
    )
    assert base.output["config_hash"] != changed.output["config_hash"]
    assert base.output["sensor_versions"] == changed.output["sensor_versions"]

