from __future__ import annotations

from dataclasses import replace

from flightfusion.graph import FusionGraph


def _build_pair(graph, state):
    incremental = graph.build(state)
    full = graph.build(state, force_recompute=True)
    assert incremental.output_bytes() == full.output_bytes()
    return incremental


def test_cache_hit_reuses_every_node(state, cache_dir):
    graph = FusionGraph(cache_dir)
    first = graph.build(state)
    second = graph.build(state)
    assert all(node.status == "recomputed" for node in first.nodes.values())
    assert set(second.reused_nodes) == set(second.nodes)
    assert not second.recomputed_nodes
    assert first.output_bytes() == second.output_bytes()


def test_noise_model_change_invalidates_only_sensor_suffix(state, cache_dir):
    graph = FusionGraph(cache_dir)
    graph.build(state)
    mag = state.descriptions["mag"]
    changed = state.replace_description(
        "mag",
        replace(
            mag,
            noise_model={**mag.noise_model, "white_noise_std": 0.12},
        ),
    )
    result = _build_pair(graph, changed)
    assert result.affected_sensors == ["mag"]
    assert set(result.recomputed_nodes) == {
        "parse:mag",
        "preprocess:mag",
        "fusion:mag",
        "fusion:merge",
        "output:attitude",
    }
    assert "parse:gyro" in result.reused_nodes
    assert "preprocess:accel" in result.reused_nodes


def test_axis_order_correction_is_traceable(state, cache_dir):
    graph = FusionGraph(cache_dir)
    graph.build(state)
    accel = state.descriptions["accel"]
    changed = state.replace_description(
        "accel",
        replace(accel, axis_order=("x", "y", "z")),
    )
    result = _build_pair(graph, changed)
    assert result.affected_sensors == ["accel"]
    assert "parse:accel" in result.recomputed_nodes
    assert "parse:mag" in result.reused_nodes


def test_calibration_upgrade_is_traceable(state, cache_dir):
    graph = FusionGraph(cache_dir)
    graph.build(state)
    gyro = state.descriptions["gyro"]
    changed = state.replace_description("gyro", gyro.with_calibration_version(4))
    result = _build_pair(graph, changed)
    assert result.affected_sensors == ["gyro"]
    assert result.nodes["parse:gyro"].payload["sensor"]["calibration_version"] == 4


def test_weight_only_change_keeps_unaffected_sensor_nodes(state, cache_dir):
    graph = FusionGraph(cache_dir)
    graph.build(state)
    changed_config = replace(
        state.fusion_config,
        config_version="fusion-weight-only",
        accel_weight=0.63,
    )
    changed = state.replace_config(changed_config)
    result = _build_pair(graph, changed)
    assert result.affected_sensors == ["accel"]
    assert set(result.recomputed_nodes) == {
        "fusion:accel",
        "fusion:merge",
        "output:attitude",
    }
    for sensor_id in ("gyro", "accel", "mag"):
        assert f"parse:{sensor_id}" in result.reused_nodes
        assert f"preprocess:{sensor_id}" in result.reused_nodes
    assert "fusion:gyro" in result.reused_nodes
    assert "fusion:mag" in result.reused_nodes


def test_cache_key_includes_required_lineage(state, cache_dir):
    result = FusionGraph(cache_dir).build(state)
    parse = result.nodes["parse:mag"]
    preprocess = result.nodes["preprocess:mag"]
    contribution = result.nodes["fusion:mag"]
    assert parse.key != preprocess.key
    assert parse.key_material["raw_digest"]
    assert parse.key_material["description_version"] == "v3"
    assert parse.key_material["axis_mapping"] == {"x": 0, "y": 2, "z": 1}
    assert preprocess.key_material["upstream_hash"] == parse.content_hash
    assert contribution.key_material["upstream_hash"] == preprocess.content_hash
    assert (
        result.nodes["fusion:merge"].key_material["config_version"]
        == state.fusion_config.config_version
    )
    assert (
        result.nodes["fusion:merge"].key_material["config_hash"]
        == state.fusion_config.content_hash()
    )
    assert preprocess.depends_on == ("parse:mag",)
    assert contribution.depends_on == ("preprocess:mag",)
    assert result.nodes["fusion:merge"].depends_on == (
        "fusion:accel",
        "fusion:gyro",
        "fusion:mag",
    )


def test_cache_query_by_key_returns_content_hash(state, cache_dir):
    graph = FusionGraph(cache_dir)
    result = graph.build(state)
    key = result.nodes["output:attitude"].key
    entry = graph.cache.query(key)
    assert entry is not None
    assert entry["key"] == key
    assert entry["content_hash"] == result.nodes["output:attitude"].content_hash
    assert entry["payload"]["stats"]["sample_count"] > 0

