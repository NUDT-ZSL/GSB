"""Requirements 3-5: atomic progress, resume, corruption and rollback."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
import pytest

from qpipe import (
    E_CACHE_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    QuantPipeline,
    ResourceBudget,
    RunContext,
    samplegen,
)
from qpipe.storage import ProgressStore, WorkDir


def test_progress_points_are_monotonic_and_complete(generated, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run_all()
    history = pipeline.store.history()
    versions = [record["progress_version"] for record in history]
    assert versions == [1, 2, 3, 4]
    head = pipeline.store.head()
    assert head["stage"] == "infer"
    assert head["completed_layers"] == [0, 1, 2, 3, 4]
    # A point always covers every layer (partial stages never commit).
    for record in history:
        point = pipeline.store._load_verified_point(record["progress_version"])
        assert len(point["completed_layers"]) == 5


def test_kill_discards_uncommitted_and_resume_matches_full(generated):
    seed = generated["seed"]
    baseline = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"] + "_base",
        context=RunContext(seed=seed),
    ).run_all().to_dict()

    # The forced termination must happen in a child process so the test runner
    # itself survives.  No exit report is written; only committed points remain.
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "qpipe.cli",
            "--data-dir",
            generated["data_dir"],
            "--work-dir",
            generated["work_dir"],
            "--seed",
            str(seed),
            "run",
            "--kill-after-layer",
            "1",
            "--kill-stage",
            "infer",
        ],
        capture_output=True,
        text=True,
    )
    assert child.returncode == 137

    head = ProgressStore(WorkDir(generated["work_dir"])).head()
    assert head["stage"] == "calibrate"  # infer never committed

    resumed = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"],
        context=RunContext(seed=seed),
    ).resume().to_dict()
    assert resumed["prediction_checksum"] == baseline["prediction_checksum"]
    assert resumed["activation_checksums"] == baseline["activation_checksums"]


def test_corrupt_progress_point_detected(generated, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run_all()
    head = pipeline.store.head()
    point_path = pipeline.work.point_path(head["progress_version"])
    with open(point_path, "r", encoding="utf-8") as handle:
        point = json.load(handle)
    point["completed_layers"] = [0, 1]
    with open(point_path, "w", encoding="utf-8") as handle:
        json.dump(point, handle)
    with pytest.raises(Exception) as exc:
        ProgressStore(pipeline.work).head()
    assert exc.value.code == E_CACHE_CORRUPT


def test_corrupt_cache_entry_detected(generated, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run_all()
    entry_path = pipeline.work.cache_entry_path("quantize", 2)
    with open(entry_path, "r", encoding="utf-8") as handle:
        record = json.load(handle)
    record["payload"]["scale"] = 999.0
    with open(entry_path, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    with pytest.raises(Exception) as exc:
        pipeline.cache.read_entry("quantize", 2)
    assert exc.value.code == E_CACHE_CORRUPT
    assert exc.value.layer_index == 2


def test_rollback_revision_rejected(generated, pipeline_factory):
    from dataclasses import replace

    pipeline = pipeline_factory()
    package = samplegen.load_manifest(pipeline._manifest_path)
    pipeline.run_all()

    older_specs = []
    for spec in package.layers:
        if spec.layer_index == 2:
            older_specs.append(
                replace(spec, weight_revision=spec.weight_revision - 1)
            )
        else:
            older_specs.append(spec)
    old_package = type(package)(
        model_tag=package.model_tag, layers=tuple(older_specs)
    )
    with pytest.raises(Exception) as exc:
        pipeline.apply_revision(
            old_package,
            changed_layer=2,
            new_weights={
                2: samplegen.generate_weight(older_specs[2], seed=generated["seed"])
            },
        )
    assert exc.value.code == E_SNAPSHOT_ROLLBACK


def test_revision_wholesale_replaces_old_cache(generated, pipeline_factory):
    from dataclasses import replace

    pipeline = pipeline_factory()
    package = samplegen.load_manifest(pipeline._manifest_path)
    pipeline.run_all()
    before = pipeline.cache.read_entry("quantize", 2)["payload"]

    specs, weights = [], {}
    for spec in package.layers:
        if spec.layer_index == 2:
            spec = replace(spec, weight_revision=spec.weight_revision + 1)
            weights[2] = samplegen.generate_weight(
                spec, seed=generated["seed"]
            )
        specs.append(spec)
    new_package = type(package)(
        model_tag=package.model_tag, layers=tuple(specs)
    )
    pipeline.apply_revision(
        new_package, changed_layer=2, new_weights=weights
    )
    after = pipeline.cache.read_entry("quantize", 2)["payload"]
    assert after["weight_revision"] == before["weight_revision"] + 1
    assert after["dequant_checksum"] != before["dequant_checksum"]
    # Downstream layer 3 was also replaced; upstream layer 1 kept its checksum.
    unchanged = pipeline.cache.read_entry("quantize", 1)["payload"]
    assert unchanged["weight_revision"] == package.layers[1].weight_revision


def test_calibration_update_invalidates_all_layers(generated, pipeline_factory):
    pipeline = pipeline_factory()
    before = pipeline.run_all().to_dict()
    package = samplegen.load_manifest(pipeline._manifest_path)
    original = samplegen.load_calibration(pipeline._calibration_path)
    updated = (original * np.float32(1.5)).astype(np.float32)
    after = pipeline.apply_calibration_update(
        package,
        new_calibration=updated,
        calibration_revision=pipeline.calibration_revision + 1,
    ).to_dict()
    assert after["recomputed_layers"] == package.indices()
    assert after["prediction_checksum"] != before["prediction_checksum"]

    with pytest.raises(Exception) as exc:
        pipeline.apply_calibration_update(
            package, new_calibration=updated, calibration_revision=1
        )
    assert exc.value.code == E_SNAPSHOT_ROLLBACK
