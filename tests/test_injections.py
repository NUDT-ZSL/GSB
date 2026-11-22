from __future__ import annotations

import numpy as np

from table_features.injection import load_injection_config, parse_injection_config
from table_features.pipeline import Pipeline


def test_four_stage_injection_records_and_degradation_chain(tmp_path, rows, cfg, project_root):
    injection = load_injection_config(project_root / "config" / "injections.json")
    result = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True).run(
        rows, cfg, injection
    )
    by_stage = {}
    for error in result.errors:
        by_stage.setdefault(error.stage, error)
    assert by_stage["encode"].code == "E_ENCODE_VALUE_RANGE"
    assert by_stage["encode"].feature == "color"
    assert by_stage["encode"].row_id == "row-0007"
    assert by_stage["standardize"].code == "E_STANDARDIZE_VALUE_INVALID"
    assert by_stage["standardize"].feature == "income"
    assert by_stage["standardize"].row_id == "row-0003"
    assert by_stage["cross"].code == "E_SLOT_REF_INVALID"
    assert by_stage["cross"].feature == "region_channel"
    select_errors = [error for error in result.errors if error.stage == "select"]
    assert any(
        error.code == "E_CONFIG_INVALID" and error.feature == "selector.threshold"
        for error in select_errors
    )
    assert all(error.code != "E_SELECT_TARGET_INVALID" for error in select_errors)

    degrade_categories = {item.category for item in result.degradations}
    assert "skip_failed_encoded_feature" in degrade_categories
    assert "skip_failed_standardized_feature" in degrade_categories
    assert "skip_invalid_cross_slot" in degrade_categories
    assert "fallback_variance_default" in degrade_categories
    assert "use_available_feature_matrix" in degrade_categories
    assert set(result.selected_features).issubset(set(result.columns))


def test_degradation_preserves_successfully_encoded_columns(tmp_path, rows, cfg, project_root):
    injection = load_injection_config(project_root / "config" / "injections.json")
    clean = Pipeline(tmp_path / "cache-clean", tmp_path / "snap-clean", clear=True).run(
        rows, cfg
    )
    degraded = Pipeline(
        tmp_path / "cache-injected",
        tmp_path / "snap-injected",
        clear=True,
    ).run(rows, cfg, injection)
    clean_columns = {column: index for index, column in enumerate(clean.columns)}
    degraded_columns = {column: index for index, column in enumerate(degraded.columns)}
    for column in ("cat__region__target", "num__age", "num__tenure"):
        assert column in degraded_columns
        np.testing.assert_array_equal(
            clean.matrix[:, clean_columns[column]],
            degraded.matrix[:, degraded_columns[column]],
        )


def test_unclassified_injection_is_aggregated_and_falls_back(tmp_path, rows, cfg, project_root):
    injection = load_injection_config(
        project_root / "config" / "injections_unclassified.json"
    )
    result = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True).run(
        rows, cfg, injection
    )
    assert "E_UNCLASSIFIED" in result.error_codes
    assert result.unclassified_errors
    record = result.unclassified_errors[0]
    assert record.stage == "select"
    assert record.feature == "selector"
    assert any(
        degrade.category == "fallback_variance_after_unclassified"
        and degrade.triggered_by == "E_UNCLASSIFIED"
        for degrade in result.degradations
    )
    assert result.selection is not None
    assert set(result.selected_features).issubset(set(result.columns))


def test_target_fold_injection_is_classified_at_encode_stage(tmp_path, rows, cfg):
    injection = parse_injection_config(
        {
            "encode": [
                {
                    "kind": "target_folds_non_positive",
                    "feature": "region",
                    "code": "E_CONFIG_INVALID",
                }
            ]
        }
    )
    result = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True).run(
        rows, cfg, injection
    )
    error = next(error for error in result.errors if error.stage == "encode")
    assert error.code == "E_CONFIG_INVALID"
    assert error.feature == "region"
    assert all(block.logical_feature != "region" for block in result.category_blocks)
    assert any(
        degrade.category == "skip_cross_slot_due_to_encode_failure"
        and degrade.triggered_by == "E_CONFIG_INVALID"
        for degrade in result.degradations
    )
    assert set(result.selected_features).issubset(set(result.columns))

