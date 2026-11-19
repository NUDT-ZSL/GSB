"""Local sample loading and input validation chain."""

from __future__ import annotations

import json
import os

import numpy as np

from .config import SAMPLE_RATE
from .errors import BusinessError, ErrorCode
from .framing import frame_count, ms_to_samples


def load_manifest(manifest_path: str) -> list:
    if not os.path.isfile(manifest_path):
        raise BusinessError(
            ErrorCode.E_IO,
            f"manifest not found: {manifest_path}",
            field="manifest_path",
        )
    rows = []
    seen: set = set()
    with open(manifest_path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BusinessError(
                    ErrorCode.E_CONFIG_INVALID,
                    f"malformed JSONL on line {line_no}: {exc}",
                    field="manifest",
                ) from exc
            dialog_id = row.get("dialog_id")
            if dialog_id in seen:
                raise BusinessError(
                    ErrorCode.E_DUPLICATE_DIALOG,
                    f"duplicate dialog_id {dialog_id!r}",
                    dialog_id=str(dialog_id),
                    field="dialog_id",
                )
            seen.add(dialog_id)
            rows.append(row)
    if not rows:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID, "manifest contains no dialogs",
            field="manifest",
        )
    return rows


def validate_manifest_row(row: dict, config) -> None:
    dialog_id = str(row.get("dialog_id", ""))
    if row.get("sample_rate") != SAMPLE_RATE:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            f"sample_rate must be {SAMPLE_RATE}, got {row.get('sample_rate')}",
            dialog_id=dialog_id,
            field="sample_rate",
        )
    frame_ms = row.get("frame_ms")
    hop_ms = row.get("hop_ms", frame_ms)
    if not isinstance(frame_ms, (int, float)) or float(frame_ms) <= 0:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            "illegal frame_ms in manifest",
            dialog_id=dialog_id,
            field="frame_ms",
        )
    if float(hop_ms) > float(frame_ms):
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            "manifest hop_ms exceeds frame_ms",
            dialog_id=dialog_id,
            field="hop_ms",
        )
    if abs(float(frame_ms) - float(config.frame_ms)) > 1e-9 or abs(
        float(hop_ms) - float(config.hop_ms)
    ) > 1e-9:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            "manifest framing does not match component configuration",
            dialog_id=dialog_id,
            field="frame_ms",
        )
    speakers = row.get("speaker_ids")
    if not isinstance(speakers, list) or not speakers:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            "speaker_ids must be a non-empty list",
            dialog_id=dialog_id,
            field="speaker_ids",
        )
    if len(speakers) > config.max_speakers:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            f"speaker count {len(speakers)} exceeds max_speakers "
            f"{config.max_speakers}",
            dialog_id=dialog_id,
            field="max_speakers",
        )
    cursor = 0.0
    for idx, turn in enumerate(row.get("turns", [])):
        if turn["end_ms"] <= turn["start_ms"]:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} has illegal timeline",
                dialog_id=dialog_id,
                field=f"turns[{idx}]",
            )
        if turn["start_ms"] < cursor - 1e-9:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} overlaps previous turn",
                dialog_id=dialog_id,
                field=f"turns[{idx}].start_ms",
            )
        if turn["end_ms"] > float(row["duration_ms"]) + 1e-9:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} exceeds dialogue duration",
                dialog_id=dialog_id,
                field=f"turns[{idx}].end_ms",
            )
        gain = turn.get("gain", 1.0)
        if gain < 0.0 or gain > 2.0:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} gain out of [0,2]",
                dialog_id=dialog_id,
                field=f"turns[{idx}].gain",
            )
        cursor = turn["end_ms"]


def load_pcm(path: str, dialog_id: str) -> np.ndarray:
    if not os.path.isfile(path):
        raise BusinessError(
            ErrorCode.E_IO, f"PCM file missing: {path}", dialog_id=dialog_id,
            field="pcm_path",
        )
    raw = np.fromfile(path, dtype=np.int16)
    if raw.size == 0:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID, "empty PCM buffer",
            dialog_id=dialog_id, field="pcm_path",
        )
    return raw


def load_ground_truth(path: str, dialog_id: str) -> np.ndarray:
    if not os.path.isfile(path):
        raise BusinessError(
            ErrorCode.E_IO, f"ground truth missing: {path}",
            dialog_id=dialog_id, field="gt_path",
        )
    try:
        gt = np.genfromtxt(path, delimiter=",", names=True, dtype=None,
                           encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID, f"unreadable ground truth: {exc}",
            dialog_id=dialog_id, field="gt_path",
        ) from exc
    required = {"frame_index", "speaker_id", "valence", "arousal", "is_speech"}
    if not required.issubset(set(gt.dtype.names or ())):
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            f"ground truth missing required columns, got {gt.dtype.names}",
            dialog_id=dialog_id,
            field="gt_path",
        )
    if gt.ndim == 0:
        gt = gt.reshape(1)
    return gt


def verify_audio_frames(row: dict, pcm: np.ndarray, gt: np.ndarray, config):
    """Cross-check audio length, ground truth rows and frame geometry."""
    dialog_id = row["dialog_id"]
    frame_size = ms_to_samples(config.frame_ms, config.sample_rate)
    hop_size = ms_to_samples(config.hop_ms, config.sample_rate)
    audio_frames = frame_count(pcm.shape[0], frame_size, hop_size)
    gt_frames = int(gt.shape[0])
    if audio_frames != gt_frames or gt_frames != int(row["num_frames"]):
        raise BusinessError(
            ErrorCode.E_FRAME_MISMATCH,
            f"audio frames {audio_frames}, gt frames {gt_frames}, manifest "
            f"frames {row['num_frames']} disagree",
            dialog_id=dialog_id,
            field="num_frames",
            details={
                "audio_frames": audio_frames,
                "gt_frames": gt_frames,
                "manifest_frames": int(row["num_frames"]),
            },
        )
    expected_index = np.arange(gt_frames, dtype=np.int64)
    if not np.array_equal(gt["frame_index"].astype(np.int64), expected_index):
        raise BusinessError(
            ErrorCode.E_INCONSISTENT,
            "ground-truth frame_index is not a contiguous 0..N-1 sequence",
            dialog_id=dialog_id,
            details={"category": "frame_index_set"},
        )
