from __future__ import annotations

import copy

from table_features.pipeline import Pipeline


def test_same_key_reuses_and_key_composition_is_inspectable(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    first = pipeline.run(rows, cfg)
    second = pipeline.run(rows, cfg)
    first_statuses = [lookup.status for lookup in first.diagnostics.cache_lookups]
    second_statuses = [lookup.status for lookup in second.diagnostics.cache_lookups]
    assert first_statuses and all(status != "hit" for status in first_statuses)
    assert second_statuses and all(status == "hit" for status in second_statuses)
    assert first.selected_features == second.selected_features
    assert first.scores == second.scores

    entries = pipeline.cache.inspect_feature("color")
    assert entries
    for entry in entries:
        components = entry["components"]["components"]
        assert components["sample_content_digest"]
        assert components["config_version"] == cfg.config_version
        assert components["slots_digest"]
        assert components["seed"] == cfg.seed
        assert len(entry["cache_key"]) == 64
        assert len(entry["content_hash"]) == 64

    score_entries = pipeline.cache.inspect_feature("cat__region__target")
    assert any(entry["stage"] == "selection_score" for entry in score_entries)


def test_different_sample_digest_recomputes_only_dependency(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    changed = copy.deepcopy(rows)
    changed[3]["age"] += 1
    result = pipeline.run(changed, cfg)
    status = {
        f"{lookup.stage}:{lookup.feature}": lookup.status
        for lookup in result.diagnostics.cache_lookups
    }
    assert status["standardize:age"] != "hit"
    assert status["standardize:income"] == "hit"
    assert status["encode:color"] == "hit"
    assert status["cross:region_channel"] == "hit"
    assert status["select:_selection"] != "hit"


def test_corrupt_cache_object_returns_code_and_feature_then_repairs(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    entry = next(
        entry for entry in pipeline.cache.inspect_feature("color") if entry["stage"] == "encode"
    )
    object_path = tmp_path / "cache" / "objects" / f"{entry['content_hash']}.json"
    object_path.write_bytes(object_path.read_bytes() + b"corrupt")

    result = pipeline.run(rows, cfg)
    error = next(error for error in result.errors if error.code == "E_CACHE_CORRUPT")
    assert error.feature == "color"
    lookup = next(
        lookup
        for lookup in result.diagnostics.cache_lookups
        if lookup.stage == "encode" and lookup.feature == "color"
    )
    assert lookup.status == "corrupt_recomputed"
    pipeline.cache.inspect_feature("color")

    repaired = pipeline.run(rows, cfg)
    assert "E_CACHE_CORRUPT" not in repaired.error_codes
    status = {
        f"{lookup.stage}:{lookup.feature}": lookup.status
        for lookup in repaired.diagnostics.cache_lookups
    }
    assert status["encode:color"] == "hit"


def test_tampered_cache_index_components_are_rejected(tmp_path, rows, cfg):
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True)
    pipeline.run(rows, cfg)
    index_path = tmp_path / "cache" / "index.json"
    index = __import__("json").loads(index_path.read_text(encoding="utf-8"))
    entry = next(
        value
        for value in index["entries"].values()
        if value["stage"] == "encode" and value["feature"] == "color"
    )
    entry["components"]["components"]["seed"] = 99
    index_path.write_text(__import__("json").dumps(index), encoding="utf-8")
    result = pipeline.run(rows, cfg)
    error = next(error for error in result.errors if error.code == "E_CACHE_CORRUPT")
    assert error.feature == "color"

