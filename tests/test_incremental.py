from __future__ import annotations

import copy

from table_features.config import parse_config
from table_features.pipeline import Pipeline


def test_numeric_row_change_does_not_affect_cross_features(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    changed = copy.deepcopy(rows)
    changed[5]["age"] += 3
    result, incremental = pipeline.run_incremental(rows, cfg, changed, cfg)
    assert incremental.plan.affected_features["cross"] == []
    assert "cross:region_channel" in incremental.observed_reused_features
    assert "standardize:age" in incremental.observed_recomputed_features
    assert not (
        set(incremental.observed_recomputed_features)
        & set(incremental.observed_reused_features)
    )
    assert set(result.selected_features).issubset(set(result.columns))


def test_category_encoding_change_reuses_raw_cross_expansion(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    raw = copy.deepcopy(cfg.raw)
    raw["category_features"][0] = {
        "name": "color",
        "encoding": "target",
        "smoothing": 5.0,
        "folds": 3,
        "seed": 11,
    }
    changed_cfg = parse_config(raw)
    _, incremental = pipeline.run_incremental(rows, cfg, rows, changed_cfg)
    assert incremental.plan.affected_features["encode"] == ["color"]
    assert incremental.plan.affected_features["cross"] == []
    assert "encode:color" in incremental.observed_recomputed_features
    assert "cross:region_channel" in incremental.observed_reused_features
    assert "cross:color_region_channel" in incremental.observed_reused_features


def test_selector_change_recomputes_only_selection(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    raw = copy.deepcopy(cfg.raw)
    raw["selector"] = {
        "type": "variance",
        "mode": "top_k",
        "target_count": 6,
        "threshold": 0.05,
        "bins": 4,
        "default_variance_threshold": 0.0,
    }
    changed_cfg = parse_config(raw)
    _, incremental = pipeline.run_incremental(rows, cfg, rows, changed_cfg)
    assert incremental.plan.affected_stages == ["select"]
    assert incremental.observed_recomputed_features == ["select:_selection"]
    assert all(
        feature in incremental.observed_reused_features
        for feature in (
            "encode:color",
            "standardize:age",
            "cross:region_channel",
        )
    )


def test_adding_slot_reuses_encoding_and_existing_cross(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    raw = copy.deepcopy(cfg.raw)
    raw["cross_slots"].append(
        {
            "slot_id": "color_channel",
            "columns": ["color", "channel"],
            "representation": "one_hot",
            "column_prefix": "x",
            "name_separator": "__",
            "value_separator": "|",
        }
    )
    changed_cfg = parse_config(raw)
    _, incremental = pipeline.run_incremental(rows, cfg, rows, changed_cfg)
    assert incremental.plan.affected_features["cross"] == ["color_channel"]
    assert "cross:color_channel" in incremental.observed_recomputed_features
    assert "cross:region_channel" in incremental.observed_reused_features
    assert "encode:color" in incremental.observed_reused_features
    assert "standardize:age" in incremental.observed_reused_features


def test_label_change_recomputes_target_encoding_but_not_one_hot(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    changed = copy.deepcopy(rows)
    changed[0]["label"] = 1 - changed[0]["label"]
    _, incremental = pipeline.run_incremental(rows, cfg, changed, cfg)
    assert "encode:region" in incremental.observed_recomputed_features
    assert "encode:color" in incremental.observed_reused_features
    assert "encode:channel" in incremental.observed_reused_features
    assert "cross:region_channel" in incremental.observed_reused_features


def test_config_version_bump_conservatively_invalidates_all(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    raw = copy.deepcopy(cfg.raw)
    raw["config_version"] = 2
    changed_cfg = parse_config(raw)
    _, incremental = pipeline.run_incremental(rows, cfg, rows, changed_cfg)
    assert incremental.plan.affected_stages == [
        "encode",
        "standardize",
        "cross",
        "select",
    ]
    assert incremental.observed_reused_features == []
    assert set(incremental.observed_recomputed_features) == set(incremental.plan.universe)


def test_statistics_version_change_invalidates_statistics_and_downstream(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    raw = copy.deepcopy(cfg.raw)
    raw["statistics_version"] = 2
    changed_cfg = parse_config(raw)
    _, incremental = pipeline.run_incremental(rows, cfg, rows, changed_cfg)
    assert incremental.plan.affected_stages == [
        "encode",
        "standardize",
        "cross",
        "select",
    ]
    assert incremental.observed_reused_features == []


def test_selection_version_change_invalidates_only_selection(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    raw = copy.deepcopy(cfg.raw)
    raw["selection_version"] = 2
    changed_cfg = parse_config(raw)
    _, incremental = pipeline.run_incremental(rows, cfg, rows, changed_cfg)
    assert incremental.plan.affected_stages == ["select"]
    assert incremental.observed_recomputed_features == ["select:_selection"]
    assert "encode:color" in incremental.observed_reused_features
    assert "cross:region_channel" in incremental.observed_reused_features

