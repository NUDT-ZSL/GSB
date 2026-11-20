"""Requirements 4 and 8: error attribution, traceability and self-checks."""

from __future__ import annotations

import pytest

from seraph_core.data.dataset import read_manifest
from seraph_core.diag.app import DiagnosticApp
from seraph_core.diag.consistency import run_self_checks
from seraph_core.diag.error_spectrum import (LEVEL_ORDER, error_spectrum,
                                             run_all_variants)
from seraph_core.engine import pipeline as pl
from seraph_core.errors import EngineError


def _dataset(config, work_dir):
    manifest = read_manifest(f"{work_dir['data']}/manifest.jsonl")
    outputs = [pl.process_dialogue(
        pl.load_dialogue(work_dir["data"], entry, config.frame_length,
                         config.hop_length), config) for entry in manifest]
    return pl.pool_from_framing(outputs, config)


def test_error_spectrum_covers_every_level(base_config, work_dir):
    dataset = _dataset(base_config, work_dir)
    variants = run_all_variants(dataset, base_config)
    spectrum = error_spectrum(variants)
    levels = [entry["level"] for entry in spectrum["levels"]]
    assert levels == list(LEVEL_ORDER)
    for entry in spectrum["levels"]:
        metrics = entry["absolute_deviation"]
        for key in ("max_abs", "mean_abs", "p50_abs", "p90_abs",
                    "p95_abs", "p99_abs"):
            assert key in metrics
        assert entry["relative_deviation"] >= 0.0
        assert entry["amplification_ratio"] >= 0.0
    assert spectrum["dominant_source"] in LEVEL_ORDER
    assert len(spectrum["traceability"]) == len(LEVEL_ORDER)


def test_dominant_attribution_points_to_real_entry(base_config, work_dir):
    dataset = _dataset(base_config, work_dir)
    spectrum = error_spectrum(run_all_variants(dataset, base_config))
    dominant = spectrum["dominant_source"]
    entry = next(item for item in spectrum["levels"]
                 if item["level"] == dominant)
    assert spectrum["dominant_evidence"]["entry_level"] == dominant
    assert spectrum["dominant_evidence"]["max_abs"] == \
        entry["absolute_deviation"]["max_abs"]


def test_full_run_self_checks_pass(base_config, work_dir):
    app = DiagnosticApp(base_config, work_dir["data"],
                        work_dir["snapshots"] + "/app")
    result = app.run_full()
    report = run_self_checks(result["dataset"], result["framing"],
                             result["training"], result["summary"],
                             base_config)
    assert report["passed"]
    check_names = {item["check"] for item in report["items"]}
    assert {"dialog_id_set", "loss_recompute", "protected_epoch",
            "chunk_plan", "chunk_spectrum_recompute"} <= check_names


def test_tampered_loss_triggers_inconsistent(base_config, work_dir):
    app = DiagnosticApp(base_config, work_dir["data"],
                        work_dir["snapshots"] + "/tamper")
    result = app.run_full()
    result["training"]["history"][0]["loss"] += 1.0
    with pytest.raises(EngineError) as exc:
        run_self_checks(result["dataset"], result["framing"],
                        result["training"], result["summary"], base_config)
    assert exc.value.code == "E_INCONSISTENT"
    assert "loss_recompute" in exc.value.item


def test_dialog_set_mismatch_detected(base_config, work_dir):
    app = DiagnosticApp(base_config, work_dir["data"],
                        work_dir["snapshots"] + "/mismatch")
    result = app.run_full()
    result["framing"].pop()
    with pytest.raises(EngineError) as exc:
        run_self_checks(result["dataset"], result["framing"],
                        result["training"], result["summary"], base_config)
    assert exc.value.code == "E_INCONSISTENT"
    assert "dialog_id_set" in exc.value.item
