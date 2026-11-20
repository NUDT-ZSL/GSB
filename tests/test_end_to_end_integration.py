"""Acceptance-style integration test against the checked-in corpus/config."""

from __future__ import annotations

import json
import os

import numpy as np

from seraph_core.config.config import EngineConfig
from seraph_core.diag.app import DiagnosticApp


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_default_corpus_full_run_and_recovery(tmp_path):
    root = _project_root()
    with open(os.path.join(root, "config", "default.json"),
              encoding="utf-8") as handle:
        config = EngineConfig.from_dict(json.load(handle))
    data_dir = os.path.join(root, "data", "corpus")

    full_dir = str(tmp_path / "full")
    full_app = DiagnosticApp(config, data_dir, full_dir)
    full = full_app.run_full()
    assert full["diagnostics"]["self_check"]["passed"]
    assert len(full["summary"]["dialogue_scores"]) == 6

    recovery_dir = str(tmp_path / "recovery")
    recovery_app = DiagnosticApp(config, data_dir, recovery_dir)
    recovery_app.run_interrupted(kill_after_epoch=5)
    resumed = recovery_app.run_resumed()
    assert np.array_equal(full["training"]["weights"],
                          resumed["training"]["weights"])
    full_loss = [row["loss"] for row in full["training"]["history"]]
    resumed_loss = [row["loss"] for row in resumed["training"]["history"]]
    assert full_loss == resumed_loss

    spectrum = resumed["diagnostics"]["error_spectrum"]
    assert {entry["level"] for entry in spectrum["levels"]} == {
        "feature_scaling", "chunk_accumulation", "fixed_quantization",
        "rounding_mode", "lr_scaling"}
    assert spectrum["dominant_source"] in {
        entry["level"] for entry in spectrum["levels"]}
