"""Requirement 1: byte-identical framing, segments and pooled vectors."""

from __future__ import annotations

import hashlib

import numpy as np

from seraph_core.data.dataset import read_manifest
from seraph_core.engine import pipeline as pl
from seraph_core.engine.features import (frame_signal, segment_boundaries,
                                         segment_width)
from seraph_core.state.runner import EngineRunner


def _run_pipeline(config, work_dir):
    manifest = read_manifest(f"{work_dir['data']}/manifest.jsonl")
    outputs = []
    for entry in manifest:
        dialogue = pl.load_dialogue(work_dir["data"], entry,
                                    config.frame_length, config.hop_length)
        outputs.append(pl.process_dialogue(dialogue, config))
    dataset = pl.pool_from_framing(outputs, config)
    return outputs, dataset


def _fingerprint(outputs, dataset) -> str:
    digest = hashlib.sha256()
    for item in outputs:
        digest.update(item["frame_features"].tobytes())
        digest.update(str(item["boundaries"]).encode())
        digest.update(item["pooled"].tobytes())
    digest.update(dataset["x_aug"].tobytes())
    digest.update(dataset["mean"].tobytes())
    digest.update(dataset["std"].tobytes())
    return digest.hexdigest()


def test_repeated_runs_are_byte_identical(base_config, work_dir):
    outputs_a, dataset_a = _run_pipeline(base_config, work_dir)
    outputs_b, dataset_b = _run_pipeline(base_config, work_dir)
    assert _fingerprint(outputs_a, dataset_a) == _fingerprint(
        outputs_b, dataset_b)


def test_frame_boundaries_are_exact_sample_indices(base_config):
    pcm = np.arange(1000, dtype=np.float64)
    frames = frame_signal(pcm, frame_length=320, hop_length=160)
    assert frames.shape[1] == 320
    expected = 1 + (1000 - 320) // 160
    assert frames.shape[0] == expected
    assert np.array_equal(frames[0], pcm[0:320])
    assert np.array_equal(frames[1], pcm[160:480])


def test_segmentation_honours_min_silence():
    is_speech = np.array([1, 1, 0, 0, 1, 1, 0, 0, 1, 1], dtype=bool)
    split = segment_boundaries(is_speech, min_silence_frames=2)
    assert split == [(0, 2), (4, 6), (8, 10)]
    short_silence = np.array([1, 1, 0, 1, 1], dtype=bool)
    assert segment_boundaries(short_silence, min_silence_frames=2) == [(0, 5)]
    merged = segment_boundaries(is_speech, min_silence_frames=3)
    assert merged == [(0, 10)]


def test_pooled_width_matches_contract(base_config, work_dir):
    outputs, dataset = _run_pipeline(base_config, work_dir)
    expected_width = segment_width(outputs[0]["frame_features"].shape[1])
    assert outputs[0]["pooled"].shape[1] == expected_width
    assert dataset["x_aug"].shape[1] == expected_width + 1


def test_snapshot_framing_roundtrip(base_config, work_dir):
    runner_a = EngineRunner(base_config, work_dir["data"],
                            work_dir["snapshots"])
    framing_a = runner_a.run_framing()
    runner_b = EngineRunner(base_config, work_dir["data"],
                            work_dir["snapshots"])
    framing_b = runner_b.run_framing()
    for item_a, item_b in zip(framing_a, framing_b):
        assert item_a["dialog_id"] == item_b["dialog_id"]
        assert np.array_equal(item_a["frame_features"],
                              item_b["frame_features"])
        assert item_a["boundaries"] == item_b["boundaries"]
