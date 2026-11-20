"""Read generated PCM / truth / manifest artefacts with strict validation."""

from __future__ import annotations

import csv
import json
import os

import numpy as np

from ..errors import (E_DUPLICATE_DIALOG, E_INPUT_INVALID, E_TRUTH_MISMATCH,
                      EngineError)

TRUTH_FIELDS = ["frame_index", "speaker_id", "valence", "arousal", "is_speech"]


def read_manifest(path: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            dialog_id = row["dialog_id"]
            if dialog_id in seen:
                raise EngineError(E_DUPLICATE_DIALOG,
                                  f"duplicate dialog_id in manifest: {dialog_id}",
                                  item=dialog_id)
            seen.add(dialog_id)
            rows.append(row)
    return sorted(rows, key=lambda r: r["dialog_id"])


def read_pcm(path: str) -> np.ndarray:
    if not os.path.exists(path):
        raise EngineError(E_INPUT_INVALID, f"PCM file missing: {path}", item=path)
    return np.fromfile(path, dtype=np.int16).astype(np.float64) / 32768.0


def read_truth(path: str) -> list[dict]:
    if not os.path.exists(path):
        raise EngineError(E_INPUT_INVALID, f"truth file missing: {path}", item=path)
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != TRUTH_FIELDS:
            raise EngineError(E_INPUT_INVALID,
                              f"truth header must be {TRUTH_FIELDS}, got "
                              f"{reader.fieldnames}", item=path)
        for row in reader:
            rows.append({
                "frame_index": int(row["frame_index"]),
                "speaker_id": int(row["speaker_id"]),
                "valence": float(row["valence"]),
                "arousal": float(row["arousal"]),
                "is_speech": int(row["is_speech"]),
            })
    return rows


def frame_count_for(n_samples: int, frame_length: int, hop_length: int) -> int:
    if n_samples < frame_length:
        return 0
    return 1 + (n_samples - frame_length) // hop_length


def load_dialogue(data_dir: str, entry: dict, frame_length: int,
                  hop_length: int) -> dict:
    dialog_id = entry["dialog_id"]
    pcm = read_pcm(os.path.join(data_dir, "pcm", f"{dialog_id}.pcm"))
    truth = read_truth(os.path.join(data_dir, "truth", f"{dialog_id}.csv"))
    expected = frame_count_for(pcm.shape[0], frame_length, hop_length)
    if len(truth) != expected:
        raise EngineError(
            E_TRUTH_MISMATCH,
            f"truth frame count {len(truth)} does not match framed audio count "
            f"{expected} for {dialog_id}", item=dialog_id)
    if [row["frame_index"] for row in truth] != list(range(expected)):
        raise EngineError(E_TRUTH_MISMATCH,
                          f"frame_index sequence not contiguous for {dialog_id}",
                          item=dialog_id)
    declared_samples = int(round(entry["sample_rate"]
                                 * float(entry["duration_ms"]) / 1000.0))
    if pcm.shape[0] != declared_samples:
        raise EngineError(E_TRUTH_MISMATCH,
                          f"PCM samples {pcm.shape[0]} != manifest duration "
                          f"{declared_samples} for {dialog_id}", item=dialog_id)
    if entry.get("frame_count") not in (None, expected):
        raise EngineError(E_TRUTH_MISMATCH,
                          f"manifest frame_count {entry['frame_count']} != "
                          f"computed {expected} for {dialog_id}", item=dialog_id)
    return {"dialog_id": dialog_id, "pcm": pcm, "truth": truth}
