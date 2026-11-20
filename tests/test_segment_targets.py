"""Segment targets must equal the mean of the framed truth channel."""

from __future__ import annotations

import numpy as np

from seraph_core.data.dataset import read_manifest
from seraph_core.engine import pipeline as pl


def test_segment_targets_match_frame_truth(base_config, work_dir):
    manifest = read_manifest(f"{work_dir['data']}/manifest.jsonl")
    for entry in manifest:
        dialogue = pl.load_dialogue(work_dir["data"], entry,
                                    base_config.frame_length,
                                    base_config.hop_length)
        result = pl.process_dialogue(dialogue, base_config)
        valence = np.array([row["valence"] for row in dialogue["truth"]])
        arousal = np.array([row["arousal"] for row in dialogue["truth"]])
        for meta, target in zip(result["segment_meta"], result["targets"]):
            start, end = meta["frame_start"], meta["frame_end"]
            assert np.isclose(target[0], valence[start:end].mean())
            assert np.isclose(target[1], arousal[start:end].mean())


def test_all_segments_are_speech(base_config, work_dir):
    manifest = read_manifest(f"{work_dir['data']}/manifest.jsonl")
    for entry in manifest:
        dialogue = pl.load_dialogue(work_dir["data"], entry,
                                    base_config.frame_length,
                                    base_config.hop_length)
        result = pl.process_dialogue(dialogue, base_config)
        for start, end in result["boundaries"]:
            assert result["is_speech"][start:end].all()
