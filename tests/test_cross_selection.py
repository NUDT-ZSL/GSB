from __future__ import annotations

import copy

import pytest

from table_features.config import parse_config
from table_features.errors import E_SLOT_DUP, BusinessError
from table_features.pipeline import Pipeline


def test_cross_columns_are_stable_and_memory_is_calculated(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    first = pipeline.run(rows, cfg)
    second = pipeline.run(rows, cfg)
    assert [block.columns for block in first.cross_blocks] == [
        block.columns for block in second.cross_blocks
    ]
    for block in first.cross_blocks:
        params = block.params
        assert params["column_count"] == len(block.columns)
        assert params["memory_estimate_bytes"] == block.matrix.nbytes
        assert params["memory_formula"] == "rows * columns * 8 bytes (dense float64)"
        assert params["columns"] == block.columns
    report = first.to_report()
    assert report["cross_summary"]["column_count"] == sum(
        len(block.columns) for block in first.cross_blocks
    )
    assert report["cross_summary"]["memory_estimate_bytes"] == sum(
        block.matrix.nbytes for block in first.cross_blocks
    )


def test_missing_cross_column_is_attributed_and_degraded(tmp_path, rows, cfg):
    raw = copy.deepcopy(cfg.raw)
    raw["cross_slots"][0]["columns"] = ["missing_region", "channel"]
    broken = parse_config(raw)
    result = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True).run(rows, broken)
    error = next(error for error in result.errors if error.code == "E_SLOT_REF_INVALID")
    assert error.stage == "cross"
    assert error.feature == "region_channel"
    assert "missing_region" in error.details["missing_columns"]
    assert any(
        item.category == "skip_invalid_cross_slot"
        and item.triggered_by == "E_SLOT_REF_INVALID"
        for item in result.degradations
    )
    assert set(result.selected_features).issubset(set(result.columns))


def test_duplicate_cross_combination_is_rejected(cfg):
    raw = copy.deepcopy(cfg.raw)
    raw["cross_slots"].append(
        {
            "slot_id": "region_channel_duplicate",
            "columns": ["channel", "region"],
            "representation": "one_hot",
            "column_prefix": "x",
            "name_separator": "__",
            "value_separator": "|",
        }
    )
    with pytest.raises(BusinessError) as caught:
        parse_config(raw)
    assert caught.value.record.code == E_SLOT_DUP
    assert caught.value.record.stage == "cross"
    assert caught.value.record.feature == "region_channel_duplicate"


def test_variance_top_k_has_exact_count_and_name_tie_break(tmp_path, rows, cfg):
    raw = copy.deepcopy(cfg.raw)
    raw["selector"] = {
        "type": "variance",
        "mode": "top_k",
        "target_count": 7,
        "threshold": 0.0,
        "bins": 4,
        "default_variance_threshold": 0.0,
    }
    result = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True).run(
        rows, parse_config(raw)
    )
    assert len(result.selected_features) == 7
    assert set(result.selected_features).issubset(set(result.columns))
    ranked = sorted(result.scores, key=lambda name: (-result.scores[name], name))
    assert result.selected_features == ranked[:7]
    for left, right in zip(ranked, ranked[1:]):
        if result.scores[left] == result.scores[right]:
            assert left < right
    assert result.selected_features == [
        name for name in result.selected_features if name in set(result.columns)
    ]


def test_variance_threshold_mode_selects_every_column_above_threshold(tmp_path, rows, cfg):
    raw = copy.deepcopy(cfg.raw)
    raw["selector"] = {
        "type": "variance",
        "mode": "threshold",
        "threshold": 0.2,
        "bins": 4,
        "default_variance_threshold": 0.0,
    }
    result = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True).run(
        rows, parse_config(raw)
    )
    expected = {name for name, score in result.scores.items() if score >= 0.2}
    assert set(result.selected_features) == expected
    assert result.selected_features == sorted(
        expected,
        key=lambda name: (-result.scores[name], name),
    )


def test_target_count_larger_than_available_returns_business_error(tmp_path, rows, cfg):
    raw = copy.deepcopy(cfg.raw)
    raw["selector"]["target_count"] = 10_000
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    result = pipeline.run(rows, parse_config(raw))
    error = next(error for error in result.errors if error.code == "E_SELECT_TARGET_INVALID")
    assert error.feature == "selector.target_count"
    assert error.details["target_count"] == 10_000
    assert result.selected_features
    assert set(result.selected_features).issubset(set(result.columns))
    assert any(
        degrade.category == "fallback_available_features"
        and degrade.triggered_by == "E_SELECT_TARGET_INVALID"
        for degrade in result.degradations
    )
    rerun = pipeline.run(rows, parse_config(raw))
    assert rerun.error_codes.count("E_SELECT_TARGET_INVALID") == 1
    assert rerun.selected_features == result.selected_features


@pytest.mark.parametrize(
    ("field", "value", "expected_field"),
    [
        ("threshold", -0.5, "selector.threshold"),
        ("bins", 1, "selector.bins"),
    ],
)
def test_invalid_selection_parameters_are_classified(cfg, field, value, expected_field):
    raw = copy.deepcopy(cfg.raw)
    raw["selector"][field] = value
    with pytest.raises(BusinessError) as caught:
        parse_config(raw)
    assert caught.value.record.code == "E_CONFIG_INVALID"
    assert caught.value.record.stage == "select"
    assert caught.value.record.feature == expected_field
    assert caught.value.record.details["field"] == expected_field


def test_mutual_info_selection_records_context(tmp_path, rows, cfg):
    result = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True).run(rows, cfg)
    assert len(result.selected_features) == cfg.selector.target_count
    assert result.selection is not None
    assert result.selection.method == "mutual_info"
    assert result.selection.score_context["requested_bins"] == 4
    assert set(result.selection.score_context["bins"]) == set(result.columns)
    assert result.selection.matrix_version == result.matrix_digest

