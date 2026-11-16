"""Shared pytest fixtures and path setup."""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voiceworkbench.config import build_config  # noqa: E402

BASE_CONFIG = {
    "sample_rate": 16000,
    "channels": 1,
    "subtype": "PCM_16",
    "frame_ms": 25,
    "hop_ms": 10,
    "voiceprint_dims": 16,
    "quant_levels": 8,
    "energy_db_floor": -45.0,
    "flatness_high": 0.55,
    "flatness_low": 0.35,
    "overlap_similarity": 0.85,
    "overlap_distance_gate": 6.0,
    "cluster_distance_threshold": 1.2,
    "min_speech_ms": 120,
    "min_silence_ms": 120,
    "max_workers": 4,
    "interact_budget_ms": 50,
    "snapshot_interval": 1,
}


@pytest.fixture
def config_factory():
    def factory(**overrides):
        raw = dict(BASE_CONFIG)
        raw.update(overrides)
        return build_config(raw)
    return factory


@pytest.fixture
def audio_dir():
    return os.path.join(ROOT, "data", "audio")


@pytest.fixture
def bad_audio_dir():
    return os.path.join(ROOT, "data", "bad_audio")


@pytest.fixture
def commands_path():
    return os.path.join(ROOT, "commands", "edits.jsonl")
