import json

from injection_workbench.checkpoints import CheckpointStore
from injection_workbench.config import Config
from injection_workbench.runner import run_batch
from injection_workbench.samples import generate_all


def test_resume_skips_completed_manifest(tmp_dirs, config):
    samples, reports, checkpoints = tmp_dirs
    generate_all(samples)
    store = CheckpointStore(checkpoints, config)
    manifest = samples / "manifest_small.json"

    first = run_batch([manifest], config, checkpoint_store=store,
                      report_path=reports / "r1.json")
    assert first["checkpoint"]["resumed_files"] == 0

    second = run_batch([manifest], config, checkpoint_store=store,
                       report_path=reports / "r2.json")
    assert second["checkpoint"]["resumed_files"] == 1
    assert second["manifests"][0]["from_checkpoint"] is True


def test_struct_version_change_forces_full_rebuild(tmp_dirs, config):
    samples, reports, checkpoints = tmp_dirs
    generate_all(samples)
    store = CheckpointStore(checkpoints, config)
    manifest = samples / "manifest_small.json"
    run_batch([manifest], config, checkpoint_store=store)

    upgraded = Config(
        thresholds=config.thresholds,
        budget=config.budget,
    )
    upgraded.checkpoint_struct_version = config.checkpoint_struct_version + 1
    new_store = CheckpointStore(checkpoints, upgraded)
    report = run_batch([manifest], upgraded, checkpoint_store=new_store,
                       report_path=reports / "r3.json")
    assert report["checkpoint"]["rebuilt"] is True
    assert "结构版本" in report["checkpoint"]["rebuild_reason"]
    assert report["checkpoint"]["resumed_files"] == 0


def test_dialect_whitelist_version_change_forces_rebuild(tmp_dirs, config):
    samples, _, checkpoints = tmp_dirs
    generate_all(samples)
    store = CheckpointStore(checkpoints, config)
    run_batch([samples / "manifest_small.json"], config, checkpoint_store=store)

    changed = Config(thresholds=config.thresholds, budget=config.budget)
    changed.dialect_whitelist_version = config.dialect_whitelist_version + 1
    state = CheckpointStore(checkpoints, changed).load()
    assert state.rebuilt is True
    assert "方言白名单" in state.rebuild_reason


def test_checkpoint_is_atomic_and_versioned(tmp_dirs, config):
    _, _, checkpoints = tmp_dirs
    store = CheckpointStore(checkpoints, config)
    store.save({"a.json": {"file_id": "a", "sink_verdicts": []}})
    data = json.loads((checkpoints / "checkpoint.json").read_text(encoding="utf-8"))
    assert data["complete"] is True
    assert data["struct_version"] == config.checkpoint_struct_version
    # 临时文件不应残留
    leftovers = [p.name for p in checkpoints.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
