"""Requirement 7 input codes and requirement 8 application surface."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from seraph_core.data.dataset import read_manifest, read_pcm
from seraph_core.data.synth import generate_corpus, generate_dialog, SpeechTurn
from seraph_core.diag.app import DiagnosticApp, write_report
from seraph_core.errors import EngineError


def test_duplicate_dialog_id_rejected(tmp_path):
    spec = {"dialogues": [
        {"dialog_id": "x", "seed": 1, "duration_ms": 1000.0,
         "scene_tag": "s",
         "turns": [{"start_ms": 0.0, "end_ms": 100.0, "speaker_id": 1,
                    "emotion_hint": "calm"}]},
        {"dialog_id": "x", "seed": 2, "duration_ms": 1000.0,
         "scene_tag": "s",
         "turns": [{"start_ms": 0.0, "end_ms": 100.0, "speaker_id": 1,
                    "emotion_hint": "calm"}]},
    ]}
    spec_path = tmp_path / "dup.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(EngineError) as exc:
        generate_corpus(str(spec_path), str(tmp_path / "out"))
    assert exc.value.code == "E_DUPLICATE_DIALOG"


def test_synthesis_is_deterministic():
    turns = [SpeechTurn(100.0, 800.0, 1, "calm"),
             SpeechTurn(900.0, 1400.0, 2, "excited")]
    pcm_a, truth_a = generate_dialog("d", 42, 1500.0, turns)
    pcm_b, truth_b = generate_dialog("d", 42, 1500.0, turns)
    assert pcm_a.tobytes() == pcm_b.tobytes()
    assert truth_a == truth_b


def test_manifest_duplicate_rejected(work_dir):
    manifest_path = os.path.join(work_dir["data"], "manifest.jsonl")
    with open(manifest_path, "a", encoding="utf-8") as handle:
        handle.write(open(manifest_path, encoding="utf-8").readline())
    with pytest.raises(EngineError) as exc:
        read_manifest(manifest_path)
    assert exc.value.code == "E_DUPLICATE_DIALOG"


def test_truth_frame_mismatch_detected(work_dir, base_config):
    from seraph_core.engine import pipeline as pl
    manifest = read_manifest(f"{work_dir['data']}/manifest.jsonl")
    entry = manifest[0]
    truth_path = os.path.join(work_dir["data"], "truth",
                              f"{entry['dialog_id']}.csv")
    lines = open(truth_path, encoding="utf-8").readlines()
    with open(truth_path, "w", encoding="utf-8") as handle:
        handle.writelines(lines[:-1])
    with pytest.raises(EngineError) as exc:
        pl.load_dialogue(work_dir["data"], entry,
                         base_config.frame_length, base_config.hop_length)
    assert exc.value.code == "E_TRUTH_MISMATCH"


def test_missing_pcm_is_input_error(work_dir):
    manifest = read_manifest(f"{work_dir['data']}/manifest.jsonl")
    os.remove(os.path.join(work_dir["data"], "pcm",
                           f"{manifest[0]['dialog_id']}.pcm"))
    with pytest.raises(EngineError) as exc:
        read_pcm(os.path.join(work_dir["data"], "pcm",
                               f"{manifest[0]['dialog_id']}.pcm"))
    assert exc.value.code == "E_INPUT_INVALID"


def test_application_status_and_report(base_config, work_dir):
    snapshot_dir = os.path.join(work_dir["snapshots"], "ui")
    app = DiagnosticApp(base_config, work_dir["data"], snapshot_dir)
    result = app.run_full()
    status_text = app.render_status_text()
    assert "framing" in status_text
    assert "dominant error source" in status_text
    report_path = os.path.join(work_dir["reports"], "ui_report.json")
    write_report(report_path, app._public_report(result))
    loaded = json.load(open(report_path, encoding="utf-8"))
    assert loaded["training"]["loss_history"]
