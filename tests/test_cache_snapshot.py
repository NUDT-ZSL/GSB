from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from intent_engine.errors import ConfigMismatchError

from .conftest import execute


def test_cache_reuses_all_stages_when_nothing_changes(tmp_path):
    state = tmp_path / "state"
    execute(state_dir=state, fresh=True, workers=2)
    second = execute(state_dir=state, fresh=True, workers=2)
    assert second.report["cache"]["miss_count"] == 0
    assert second.report["cache"]["hit_count"] == 75
    assert second.report["incremental"]["affected_samples"] == []
    assert all(value["computed"] == 0 for value in second.report["cache"]["by_stage"].values())


def test_entity_removal_invalidates_only_referencing_samples(tmp_path, data_dir):
    state = tmp_path / "state"
    execute(state_dir=state, fresh=True, workers=2)
    altered = tmp_path / "resources"
    shutil.copytree(data_dir / "resources", altered)
    entity_path = altered / "entities.json"
    document = json.loads(entity_path.read_text(encoding="utf-8"))
    document["entities"]["品牌"] = [item for item in document["entities"]["品牌"] if item["term"] != "华为"]
    entity_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    second = execute(state_dir=state, resources_dir=altered, config_path=altered / "config.json", fresh=True, workers=2)
    assert second.report["cache"]["changed_entries"]["removed"] == ["entity:品牌:华为"]
    assert second.report["incremental"]["affected_samples"] == ["C001", "C009"]
    assert second.report["cache"]["by_stage"]["slots"]["computed"] == 2
    assert second.report["cache"]["by_stage"]["sequence"]["computed"] == 2
    assert second.report["cache"]["by_stage"]["merge"]["computed"] == 2
    assert second.report["cache"]["by_stage"]["intent"]["computed"] == 0
    by_id = {item["sample_id"]: item for item in second.results}
    assert all(slot["text"] != "华为" for slot in by_id["C001"]["slots"])
    assert all(slot["text"] != "华为" for slot in by_id["C009"]["slots"])


def test_interrupted_snapshot_resume_matches_uninterrupted(tmp_path):
    baseline = execute(state_dir=tmp_path / "baseline", fresh=True, workers=2)
    interrupted = execute(state_dir=tmp_path / "recover", fresh=True, workers=2, interrupt_after=4)
    assert interrupted.complete is False
    assert interrupted.interrupted is True
    resumed = execute(state_dir=tmp_path / "recover", fresh=False, workers=2)
    assert resumed.complete is True
    assert resumed.results == baseline.results
    assert resumed.failures == baseline.failures
    assert resumed.report["intent_distribution"] == baseline.report["intent_distribution"]
    assert resumed.report["confidence_stats"] == baseline.report["confidence_stats"]


def test_snapshot_fingerprint_mismatch_is_config_error(tmp_path, data_dir):
    state = tmp_path / "state"
    execute(state_dir=state, fresh=True, workers=1)
    config = json.loads((data_dir / "resources" / "config.json").read_text(encoding="utf-8"))
    config["confidence_threshold"] = 0.61
    changed = tmp_path / "config.json"
    changed.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ConfigMismatchError) as caught:
        execute(
            state_dir=state,
            config_path=changed,
            fresh=False,
            workers=1,
        )
    assert caught.value.code == "E_CONFIG_MISMATCH"
    assert caught.value.details["reason"] == "snapshot_fingerprint_mismatch"


def test_snapshot_has_required_fields_and_atomic_pointer(tmp_path):
    state = tmp_path / "state"
    result = execute(state_dir=state, fresh=True, workers=1)
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    for key in (
        "version",
        "stage",
        "stages",
        "config_hash",
        "resource_hash",
        "completed_sample_ids",
        "partial_result_summary",
    ):
        assert key in latest
    assert latest["version"] == result.snapshot_version
    assert latest["stage"] == "all"
    assert latest["stages"] == ["normalize", "intent", "slots", "sequence", "merge"]
    assert len(latest["completed_sample_ids"]) == 15
    snapshot_dir = state / latest["snapshot_directory"]
    assert (snapshot_dir / "manifest.json").is_file()
    for stage in latest["stages"]:
        assert (snapshot_dir / f"{stage}.json").is_file()

