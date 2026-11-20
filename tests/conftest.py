"""Shared pytest fixtures: tiny deterministic corpus in a temp directory."""

from __future__ import annotations

import json

import pytest

from seraph_core.config.config import EngineConfig
from seraph_core.data.synth import generate_corpus


TINY_SPEC = {
    "dialogues": [
        {
            "dialog_id": "tdlg_a",
            "seed": 11,
            "duration_ms": 1500.0,
            "scene_tag": "unit",
            "turns": [
                {"start_ms": 100.0, "end_ms": 600.0, "speaker_id": 1,
                 "emotion_hint": "calm"},
                {"start_ms": 750.0, "end_ms": 1400.0, "speaker_id": 2,
                 "emotion_hint": "excited"},
            ],
        },
        {
            "dialog_id": "tdlg_b",
            "seed": 22,
            "duration_ms": 2100.0,
            "scene_tag": "unit",
            "turns": [
                {"start_ms": 120.0, "end_ms": 900.0, "speaker_id": 3,
                 "emotion_hint": "low"},
                {"start_ms": 1050.0, "end_ms": 2000.0, "speaker_id": 1,
                 "emotion_hint": "calm"},
            ],
        },
    ]
}


@pytest.fixture()
def work_dir(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(TINY_SPEC), encoding="utf-8")
    data_dir = tmp_path / "corpus"
    generate_corpus(str(spec_path), str(data_dir))
    snapshot_dir = tmp_path / "snapshots"
    report_dir = tmp_path / "reports"
    return {"tmp": tmp_path, "data": str(data_dir),
            "snapshots": str(snapshot_dir), "reports": str(report_dir)}


@pytest.fixture()
def base_config():
    return EngineConfig(epochs=12, chunk_size=4, learning_rate=0.02)


@pytest.fixture()
def tiny_limits(base_config):
    base_config.resource_limits.max_gradient_bytes = 64 * 1024
    base_config.resource_limits.min_chunk_size = 2
    return base_config
