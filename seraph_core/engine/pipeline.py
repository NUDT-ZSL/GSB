"""End-to-end deterministic framing -> segmentation -> pooling pipeline."""

from __future__ import annotations

import numpy as np

from ..data.dataset import load_dialogue
from .features import (detect_silence, frame_features, frame_signal,
                       pool_segment, segment_boundaries, segment_width,
                       standardize_apply, standardize_fit)
from .training import augment

N_FRAME_FEATURES = 5


def process_dialogue(dialogue: dict, config) -> dict:
    """Frame one dialogue, extract acoustics, cut segments and pool them."""
    frame_length = config.frame_length
    hop_length = config.hop_length
    frames = frame_signal(dialogue["pcm"], frame_length, hop_length)
    features = frame_features(frames, config.sample_rate)

    silence = detect_silence(features, config)
    truth = dialogue["truth"]
    # Speech mask from the truth file is the observed ground-truth channel;
    # the acoustic silence mask drives segmentation and is stored too.
    is_speech = np.array([row["is_speech"] == 1 for row in truth], dtype=bool)
    boundaries = segment_boundaries(is_speech, config.min_silence_frames)

    width = segment_width(features.shape[1])
    pooled = np.zeros((len(boundaries), width), dtype=np.float64)
    targets = np.zeros((len(boundaries), 2), dtype=np.float64)
    segment_meta: list[dict] = []
    valence = np.array([row["valence"] for row in truth], dtype=np.float64)
    arousal = np.array([row["arousal"] for row in truth], dtype=np.float64)
    speaker = np.array([row["speaker_id"] for row in truth], dtype=np.int64)

    for seg_idx, (start, end) in enumerate(boundaries):
        pooled[seg_idx] = pool_segment(features, start, end)
        targets[seg_idx, 0] = float(np.mean(valence[start:end]))
        targets[seg_idx, 1] = float(np.mean(arousal[start:end]))
        speakers = sorted(set(int(s) for s in speaker[start:end]))
        segment_meta.append({
            "dialog_id": dialogue["dialog_id"],
            "segment_index": seg_idx,
            "frame_start": int(start),
            "frame_end": int(end),
            "speaker_ids": speakers,
        })
    return {
        "dialog_id": dialogue["dialog_id"],
        "frame_features": features,
        "silence_mask": silence,
        "is_speech": is_speech,
        "boundaries": boundaries,
        "pooled": pooled,
        "targets": targets,
        "segment_meta": segment_meta,
    }


def build_dataset(data_dir: str, manifest: list[dict], config) -> dict:
    """Run every dialogue and assemble the standardised segment matrix."""
    per_dialog: list[dict] = []
    dialog_ids: list[str] = []
    for entry in manifest:
        dialogue = load_dialogue(data_dir, entry, config.frame_length,
                                 config.hop_length)
        result = process_dialogue(dialogue, config)
        per_dialog.append(result)
        dialog_ids.append(result["dialog_id"])

    raw_x = np.concatenate([item["pooled"] for item in per_dialog], axis=0)
    raw_y = np.concatenate([item["targets"] for item in per_dialog], axis=0)
    mean, std = standardize_fit(raw_x, config.subnormal_protection)
    x_scaled = standardize_apply(raw_x, mean, std, config.feature_scale)
    x_aug = augment(x_scaled)
    segment_meta: list[dict] = []
    for item in per_dialog:
        segment_meta.extend(item["segment_meta"])
    return {
        "dialog_ids": dialog_ids,
        "per_dialog": per_dialog,
        "raw_x": raw_x,
        "x": x_scaled,
        "x_aug": x_aug,
        "y": raw_y,
        "mean": mean,
        "std": std,
        "segment_meta": segment_meta,
    }


def pool_from_framing(framing_outputs: list[dict], config) -> dict:
    """Rebuild the pooled, standardised segment dataset from framing output.

    This is the stage-2 entry point and performs no PCM access, proving that
    the pooling stage is a pure deterministic function of the framing snapshot.
    """
    per_dialog = sorted(framing_outputs, key=lambda item: item["dialog_id"])
    raw_x = np.concatenate([item["pooled"] for item in per_dialog], axis=0)
    raw_y = np.concatenate([item["targets"] for item in per_dialog], axis=0)
    mean, std = standardize_fit(raw_x, config.subnormal_protection)
    x_scaled = standardize_apply(raw_x, mean, std, config.feature_scale)
    x_aug = augment(x_scaled)
    segment_meta: list[dict] = []
    for item in per_dialog:
        segment_meta.extend(item["segment_meta"])
    return {
        "dialog_ids": [item["dialog_id"] for item in per_dialog],
        "per_dialog": per_dialog,
        "raw_x": raw_x,
        "x": x_scaled,
        "x_aug": x_aug,
        "y": raw_y,
        "mean": mean,
        "std": std,
        "segment_meta": segment_meta,
    }


def dialogue_scores(weights: np.ndarray, dataset: dict) -> dict:
    """Roll segment predictions up to per-dialogue valence/arousal means."""
    predictions = dataset["x_aug"] @ weights
    scores: dict[str, dict] = {}
    for dialog_id in dataset["dialog_ids"]:
        indices = [idx for idx, meta in enumerate(dataset["segment_meta"])
                   if meta["dialog_id"] == dialog_id]
        block = predictions[indices]
        truth_block = dataset["y"][indices]
        scores[dialog_id] = {
            "pred_valence": float(np.mean(block[:, 0])),
            "pred_arousal": float(np.mean(block[:, 1])),
            "truth_valence": float(np.mean(truth_block[:, 0])),
            "truth_arousal": float(np.mean(truth_block[:, 1])),
            "segment_count": len(indices),
        }
    return scores
