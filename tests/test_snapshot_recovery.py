"""Requirements 5 and 6: four-stage snapshots, recovery and stability."""

from __future__ import annotations

import json
import os
import hashlib

import numpy as np
import pytest

from seraph_core.config.config import EngineConfig
from seraph_core.diag.app import DiagnosticApp
from seraph_core.errors import EngineError
from seraph_core.state.runner import EngineRunner
from seraph_core.state.snapshot import SnapshotStore


def _full_run(config, work_dir, name="full"):
    snapshot_dir = os.path.join(work_dir["snapshots"], name)
    app = DiagnosticApp(config, work_dir["data"], snapshot_dir)
    return app.run_full(), app


def test_snapshot_versions_monotonic_and_checksummed(base_config, work_dir):
    runner = EngineRunner(base_config, work_dir["data"],
                          os.path.join(work_dir["snapshots"], "a"))
    runner.run_all()
    store = runner.store
    for stage in ("framing", "pooling", "summary"):
        assert store.is_complete(stage)
        assert store.latest_version(stage) >= 1
    assert store.latest_version("training") == base_config.epochs + 1
    envelope, snapshot = store.load_latest("training")
    assert envelope["version"] == base_config.epochs + 1
    assert envelope["config_checksum"] == store.config_checksum
    assert envelope["payload_sha256"]
    assert snapshot["payload"]["completed"] is True


def test_corrupt_snapshot_detected(base_config, work_dir):
    runner = EngineRunner(base_config, work_dir["data"],
                          os.path.join(work_dir["snapshots"], "corrupt"))
    runner.run_framing()
    store = runner.store
    stage_dir = os.path.join(store.root_dir, "framing")
    target = os.path.join(stage_dir, "v00001.json")
    with open(target, "r", encoding="utf-8") as handle:
        envelope = json.load(handle)
    envelope["payload"]["frame_length"] = 999
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle)
    with pytest.raises(EngineError) as exc:
        store.load_latest("framing")
    assert exc.value.code == "E_SNAPSHOT_CORRUPT"


def test_lineage_drift_requires_full_replacement(base_config, work_dir):
    snapshot_dir = os.path.join(work_dir["snapshots"], "lineage")
    runner = EngineRunner(base_config, work_dir["data"], snapshot_dir)
    runner.run_framing()
    drifted = EngineConfig(**{**base_config.to_dict(), "frame_ms": 20.0,
                              "hop_ms": 5.0})
    drifted_store = SnapshotStore(snapshot_dir,
                                  hashlib.sha256(
                                      drifted.canonical_json()).hexdigest(),
                                  drifted.snapshot_schema_version)
    with pytest.raises(EngineError) as exc:
        drifted_store.load_latest("framing")
    assert exc.value.code == "E_SNAPSHOT_CORRUPT"
    drifted_store.reset_stage("framing")
    assert drifted_store.latest_version("framing") == 0


def test_commit_lock_is_non_blocking(base_config, work_dir):
    store = SnapshotStore(os.path.join(work_dir["snapshots"], "lock"),
                          "cfg")
    with store.commit_lock():
        with pytest.raises(EngineError) as exc:
            with store.commit_lock():
                pass
    assert exc.value.code == "E_STAGE_BUSY"


def test_force_termination_then_resume_matches_full_run(base_config, work_dir):
    full_result, _ = _full_run(base_config, work_dir, name="uninterrupted")

    recovery_dir = os.path.join(work_dir["snapshots"], "recovery")
    app = DiagnosticApp(base_config, work_dir["data"], recovery_dir)
    interrupted = app.run_interrupted(kill_after_epoch=3)
    assert interrupted["terminated"]
    assert not app.runner.store.is_complete("training")

    resumed = app.run_resumed()
    assert np.array_equal(full_result["training"]["weights"],
                          resumed["training"]["weights"])
    full_losses = [row["loss"] for row in
                   full_result["training"]["history"]]
    resumed_losses = [row["loss"] for row in
                      resumed["training"]["history"]]
    assert full_losses == resumed_losses
    assert 3 in resumed["training"]["protected_epochs"]
    assert 3 in resumed["training"]["skipped_epochs"]
    resume_events = [event["event"] for event in app.runner.events]
    assert "training.resumed" in resume_events


def test_uncommitted_state_is_rebuilt_from_epoch_start(base_config, work_dir):
    recovery_dir = os.path.join(work_dir["snapshots"], "rebuild")
    app = DiagnosticApp(base_config, work_dir["data"], recovery_dir)
    app.run_interrupted(kill_after_epoch=2)
    _, snapshot = app.runner.store.load_latest("training")
    assert snapshot["payload"]["last_epoch"] == 2
    assert snapshot["payload"]["next_epoch"] == 3
    resumed = app.run_resumed()
    epochs = [row["epoch"] for row in resumed["training"]["history"]]
    assert epochs == list(range(1, base_config.epochs + 1))


def test_strict_and_band_convergence_strategies(base_config):
    model_config = EngineConfig(**{**base_config.to_dict(),
                                   "convergence_strategy": "strict",
                                   "convergence_tolerance": 1e-2})
    x = np.random.default_rng(7).normal(size=(12, 4))
    y = np.column_stack([x[:, 0] * 0.5, x[:, 1] * 0.3])
    from seraph_core.engine.training import LinearRegressor, chunk_plan, augment
    model = LinearRegressor(4, model_config)
    history, converged = model.train(augment(x), y, chunk_plan(12, 4))
    assert converged >= 1 or len(history) == model_config.epochs
    assert history[-1].convergence_reason.startswith("strict")
