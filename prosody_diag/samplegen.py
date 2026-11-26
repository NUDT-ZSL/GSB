from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import canonical_hash, write_json_atomic, write_jsonl_atomic
from .config import Config


SPEAKERS = [
    {"speaker_id": "SPK01", "f0_mean": 116.0, "amplitude": 0.205, "timbre_seed": 11},
    {"speaker_id": "SPK02", "f0_mean": 208.0, "amplitude": 0.175, "timbre_seed": 29},
    {"speaker_id": "SPK03", "f0_mean": 151.0, "amplitude": 0.190, "timbre_seed": 47},
]

TEXTS = [
    "清晨的雨声，掠过安静的站台，远方传来钟声。",
    "模型迁移之后，旧规则与新分片需要逐项核对。",
    "韵律边界决定停顿，也影响多说话人合成的自然度。",
    "他们把基频、能量和停顿时长，写入逐帧真值。",
    "决策树记录阈值、置信度和规则编号，便于追溯。",
    "缓存键包含哈希、精度档与规则代数，支持定向重算。",
]

UTTERANCE_MS = 2400
LEAD_MS = 160
GAP_MS = 520


def _pause_plan(utterance_index: int) -> list[dict[str, Any]]:
    # Durations are ten-millisecond grid values. The first short pause is the
    # designed v1/v2 threshold conflict; all plans remain numerically stable.
    short_duration = 100 if utterance_index % 2 == 0 else 120
    return [
        {"start_ms": 520, "duration_ms": short_duration, "gold_label": "B1"},
        {"start_ms": 1120, "duration_ms": 220, "gold_label": "B2"},
        {"start_ms": 1760, "duration_ms": 320, "gold_label": "B3"},
    ]


def _is_in_pause(center_ms: float, pauses: list[dict[str, Any]]) -> dict[str, Any] | None:
    for pause in pauses:
        if pause["start_ms"] <= center_ms < pause["start_ms"] + pause["duration_ms"]:
            return pause
    return None


def build_utterance_sample(
    utterance_index: int,
    speaker: dict[str, Any],
    text: str,
    cfg: Config,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], bytes]:
    duration_ms = UTTERANCE_MS
    sample_count = cfg.sample_rate * duration_ms // 1000
    frame_len = cfg.frame_length_samples
    hop = cfg.frame_shift_samples
    frame_count = 1 + (sample_count - frame_len) // hop
    pauses = _pause_plan(utterance_index)
    rng = np.random.default_rng(cfg.seed + 1009 * utterance_index + int(speaker["timbre_seed"]))

    centers_ms = np.array(
        [(i * hop + frame_len // 2) * 1000.0 / cfg.sample_rate for i in range(frame_count)],
        dtype=np.float64,
    )
    f0_true = np.zeros(frame_count, dtype=np.float64)
    energy_true = np.zeros(frame_count, dtype=np.float64)
    pause_labels = np.zeros(frame_count, dtype=np.int16)
    pause_duration = np.zeros(frame_count, dtype=np.int16)

    for i, center_ms in enumerate(centers_ms):
        pause = _is_in_pause(float(center_ms), pauses)
        if pause is not None:
            pause_labels[i] = 1
            pause_duration[i] = pause["duration_ms"]
            continue
        declination = 1.0 - 0.10 * i / max(1, frame_count - 1)
        syllable_phase = 2.0 * np.pi * center_ms / 245.0 + utterance_index * 0.37
        wobble = 1.0 + 0.045 * np.sin(2.0 * np.pi * center_ms / 173.0 + speaker["timbre_seed"])
        f0_true[i] = float(speaker["f0_mean"] * declination * wobble)
        envelope = 0.58 + 0.42 * (0.5 + 0.5 * np.sin(syllable_phase)) ** 1.7
        clause_boost = 1.0 + 0.05 * np.sin(2.0 * np.pi * center_ms / 760.0)
        energy_true[i] = float(speaker["amplitude"] * envelope * clause_boost)

    center_samples = np.array(
        [i * hop + frame_len // 2 for i in range(frame_count)], dtype=np.float64
    )
    all_samples = np.arange(sample_count, dtype=np.float64)
    f0_sample = np.interp(all_samples, center_samples, f0_true)
    energy_sample = np.interp(all_samples, center_samples, energy_true)
    # Avoid interpolation creating audible energy inside explicit zero pauses.
    pause_sample = np.interp(all_samples, center_samples, pause_labels.astype(np.float64))
    energy_sample = energy_sample * (pause_sample < 0.5)
    f0_sample = f0_sample * (pause_sample < 0.5)

    phase = 2.0 * np.pi * np.cumsum(f0_sample, dtype=np.float64) / cfg.sample_rate
    voice = (
        0.62 * np.sin(phase)
        + 0.25 * np.sin(2.0 * phase + 0.4)
        + 0.13 * np.sin(3.0 * phase + 0.9)
    )
    voice_rms = float(np.sqrt(np.mean(voice * voice))) or 1.0
    voice = voice / voice_rms
    breath = rng.normal(0.0, 0.006, sample_count)
    breath_envelope = np.clip(energy_sample / max(cfg.energy_scale, 1e-9), 0.0, 1.0)
    pcm_f64 = energy_sample * voice + breath * breath_envelope * 0.35
    pcm_f64 = np.clip(pcm_f64, -0.98, 0.98)
    pcm_i16 = np.round(pcm_f64 * 32767.0).astype("<i2")
    pcm_bytes = pcm_i16.tobytes(order="C")

    utterance_id = f"U{utterance_index + 1:03d}"
    task = {
        "utterance_id": utterance_id,
        "speaker_id": speaker["speaker_id"],
        "text": text,
        "start_ms": LEAD_MS,
        "end_ms": LEAD_MS + duration_ms,
        "precision_profile": ["fp64", "fp32", "q15"][utterance_index % 3],
        "decision_tree_rev": 2,
        "logical_clock_ms": 1_760_000_000_000 + utterance_index * 60_000,
        "revision": 1,
        "seed": cfg.seed + utterance_index,
    }

    truth_rows: list[dict[str, Any]] = []
    for i in range(frame_count):
        truth_rows.append(
            {
                "utterance_id": utterance_id,
                "frame_index": i,
                "speaker_id": speaker["speaker_id"],
                "f0_true": round(float(f0_true[i]), 6),
                "energy_true": round(float(energy_true[i]), 9),
                "pause_label": int(pause_labels[i]),
                "pause_duration_ms": int(pause_duration[i]),
                "frame_start_sample": i * hop,
                "frame_end_sample": i * hop + frame_len,
            }
        )

    boundary_truth: list[dict[str, Any]] = []
    for pause in pauses:
        frame_index = (pause["start_ms"] - cfg.frame_length_ms // 2) // cfg.frame_shift_ms
        boundary_truth.append(
            {
                "utterance_id": utterance_id,
                "frame_index": int(frame_index),
                "boundary_type": "pause_onset",
                "gold_label": pause["gold_label"],
                "pause_duration_ms": pause["duration_ms"],
            }
        )

    return task, truth_rows, boundary_truth, pcm_bytes


def generate_sample_workspace(sample_dir: str | Path, cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or Config()
    cfg.validate()
    root = Path(sample_dir)
    audio_dir = root / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    tasks: list[dict[str, Any]] = []
    truth: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    speaker_pcm: dict[str, bytearray] = {s["speaker_id"]: bytearray() for s in SPEAKERS}

    for index, text in enumerate(TEXTS):
        speaker = SPEAKERS[index % len(SPEAKERS)]
        task, frames, gold, segment = build_utterance_sample(index, speaker, text, cfg)
        # Lay utterances on each speaker's pronunciation timeline.
        timeline_start_ms = index // len(SPEAKERS) * (UTTERANCE_MS + LEAD_MS + GAP_MS)
        task["start_ms"] = timeline_start_ms + LEAD_MS
        task["end_ms"] = task["start_ms"] + UTTERANCE_MS
        start_sample = task["start_ms"] * cfg.sample_rate // 1000
        pcm_buffer = speaker_pcm[speaker["speaker_id"]]
        current_samples = len(pcm_buffer) // 2
        if current_samples < start_sample:
            pcm_buffer.extend(b"\x00\x00" * (start_sample - current_samples))
        pcm_buffer.extend(segment)
        tasks.append(task)
        for row in frames:
            row["start_ms"] = task["start_ms"]
            row["end_ms"] = task["end_ms"]
            row["absolute_frame_start_sample"] = task["start_ms"] * 16 + row["frame_start_sample"]
        truth.extend(frames)
        boundaries.extend(gold)

    for speaker in SPEAKERS:
        speaker_id = speaker["speaker_id"]
        (audio_dir / f"{speaker_id}.pcm").write_bytes(bytes(speaker_pcm[speaker_id]))

    write_jsonl_atomic(root / "tasks.jsonl", tasks)
    write_jsonl_atomic(root / "frame_truth.jsonl", truth)
    write_jsonl_atomic(root / "boundary_truth.jsonl", boundaries)
    manifest = {
        "seed": cfg.seed,
        "sample_rate": cfg.sample_rate,
        "frame_length_ms": cfg.frame_length_ms,
        "frame_shift_ms": cfg.frame_shift_ms,
        "utterance_count": len(tasks),
        "speaker_ids": [s["speaker_id"] for s in SPEAKERS],
        "task_fields": [
            "utterance_id",
            "speaker_id",
            "text",
            "start_ms",
            "end_ms",
            "precision_profile",
            "decision_tree_rev",
            "logical_clock_ms",
        ],
        "tasks_sha256": canonical_hash(tasks),
        "frame_truth_sha256": canonical_hash(truth),
        "boundary_truth_sha256": canonical_hash(boundaries),
    }
    write_json_atomic(root / "manifest.json", manifest)
    return manifest
