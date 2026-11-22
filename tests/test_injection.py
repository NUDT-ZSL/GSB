from __future__ import annotations

from flightfusion.graph import FusionGraph
from flightfusion.injection import InjectionPlan


def test_four_stage_injection_and_upstream_propagation(state, cache_dir):
    plan = InjectionPlan.from_specs(
        [
            "parse.axis_missing@mag",
            "preprocess.calibration_rollback@gyro",
            "preprocess.sample_rate_nonpositive@accel",
            "fusion.gain_out_of_range",
            "output.residual_missing",
        ]
    )
    graph = FusionGraph(cache_dir)
    result = graph.build(state, injection_plan=plan)
    stages = {item["stage"] for item in result.diagnostics}
    assert {"parse", "preprocess", "fusion", "output"}.issubset(stages)
    codes = {item["code"] for item in result.diagnostics}
    assert {
        "E_AXIS_MAPPING_INVALID",
        "E_SAMPLE_RATE_INVALID",
        "E_CALIBRATION_ROLLBACK",
        "E_GAIN_OUT_OF_RANGE",
        "E_OUTPUT_FIELD_MISSING",
        "E_UPSTREAM_DEGRADED",
    }.issubset(codes)
    propagated = [
        item for item in result.diagnostics if item["code"] == "E_UPSTREAM_DEGRADED"
    ]
    assert propagated
    assert all(item["upstream_code"] for item in propagated)
    assert result.output["residual_field_degraded"] is True
    assert result.output_bytes() == graph.build(
        state, injection_plan=plan, force_recompute=True
    ).output_bytes()


def test_injection_records_have_stage_sensor_field_and_degradation(state, cache_dir):
    plan = InjectionPlan.from_specs(["parse.axis_missing@mag"])
    graph = FusionGraph(cache_dir)
    graph.build(state)
    result = graph.build(state, injection_plan=plan)
    direct = next(
        item
        for item in result.diagnostics
        if item["code"] == "E_AXIS_MAPPING_INVALID"
    )
    assert direct["stage"] == "parse"
    assert direct["sensor_id"] == "mag"
    assert direct["field"] == "axis_order"
    assert direct["degradation"] == "fallback_identity_axis_mapping"
    assert "parse:mag" in result.recomputed_nodes
    assert "parse:gyro" in result.reused_nodes


def test_output_only_injection_does_not_invalidate_upstream(state, cache_dir):
    graph = FusionGraph(cache_dir)
    graph.build(state)
    result = graph.build(
        state,
        injection_plan=InjectionPlan.from_specs(["output.residual_missing"]),
    )
    assert result.recomputed_nodes == ["output:attitude"]
    assert "fusion:merge" in result.reused_nodes

