"""Deterministic PCM, frame truth and annotation sample generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import numpy as np

from .canonical import atomic_write_json, sha256_hex
from .config import EngineConfig


FloorSpan = Dict[str, Any]
VoiceSegment = Dict[str, Any]


def default_timeline(config: EngineConfig) -> Tuple[List[FloorSpan], List[VoiceSegment], List[Dict[str, Any]]]:
    """Return floor spans, voice synthesis spans and injected anomaly truth."""
    floor_spans: List[FloorSpan] = [
        {"start_ms": 0, "end_ms": 500, "speaker_id": 0, "turn_label": "pause_open", "kind": "silence"},
        {"start_ms": 500, "end_ms": 2300, "speaker_id": 1, "turn_label": "turn_001", "kind": "normal_turn"},
        {"start_ms": 2300, "end_ms": 2600, "speaker_id": 0, "turn_label": "pause_001", "kind": "normal_pause"},
        {"start_ms": 2600, "end_ms": 4200, "speaker_id": 2, "turn_label": "turn_002", "kind": "normal_turn"},
        {"start_ms": 4200, "end_ms": 4450, "speaker_id": 0, "turn_label": "pause_002", "kind": "normal_pause"},
        {"start_ms": 4450, "end_ms": 6000, "speaker_id": 3, "turn_label": "turn_003", "kind": "normal_turn"},
        {"start_ms": 6000, "end_ms": 6900, "speaker_id": 0, "turn_label": "pause_abnormal_003", "kind": "abnormal_pause"},
        {"start_ms": 6900, "end_ms": 8100, "speaker_id": 1, "turn_label": "turn_004", "kind": "normal_turn"},
        {"start_ms": 8100, "end_ms": 8400, "speaker_id": 0, "turn_label": "pause_004", "kind": "normal_pause"},
        {"start_ms": 8400, "end_ms": 9100, "speaker_id": 2, "turn_label": "turn_005", "kind": "normal_turn"},
        {"start_ms": 9100, "end_ms": 10500, "speaker_id": 3, "turn_label": "turn_006", "kind": "grab_floor"},
        {"start_ms": 10500, "end_ms": 10750, "speaker_id": 0, "turn_label": "pause_005", "kind": "normal_pause"},
        {"start_ms": 10750, "end_ms": 11600, "speaker_id": 1, "turn_label": "turn_007", "kind": "normal_turn"},
        {"start_ms": 11600, "end_ms": 11900, "speaker_id": 2, "turn_label": "turn_008_interrupt", "kind": "interruption"},
        {"start_ms": 11900, "end_ms": 12600, "speaker_id": 1, "turn_label": "turn_007", "kind": "interruption_resume"},
        {"start_ms": 12600, "end_ms": 14000, "speaker_id": 0, "turn_label": "pause_closing", "kind": "silence"},
    ]

    voice_segments: List[VoiceSegment] = [
        {"speaker_id": 1, "start_ms": 500, "end_ms": 2300, "amplitude_gain": 1.00},
        {"speaker_id": 2, "start_ms": 2600, "end_ms": 4200, "amplitude_gain": 0.96},
        {"speaker_id": 3, "start_ms": 4450, "end_ms": 6000, "amplitude_gain": 1.05},
        {"speaker_id": 1, "start_ms": 6900, "end_ms": 8100, "amplitude_gain": 1.00},
        {"speaker_id": 2, "start_ms": 8400, "end_ms": 9400, "amplitude_gain": 0.96},
        {"speaker_id": 3, "start_ms": 9100, "end_ms": 10500, "amplitude_gain": 1.08},
        {"speaker_id": 1, "start_ms": 10750, "end_ms": 12600, "amplitude_gain": 0.92},
        {"speaker_id": 2, "start_ms": 11600, "end_ms": 11900, "amplitude_gain": 1.12},
    ]

    injected_events = [
        {
            "type": "abnormal_pause",
            "start_ms": 6000,
            "end_ms": 6900,
            "start_frame": 600,
            "end_frame": 690,
            "between_turns": ["turn_003", "turn_004"],
        },
        {
            "type": "floor_grab",
            "start_ms": 9100,
            "end_ms": 9400,
            "start_frame": 910,
            "end_frame": 940,
            "previous_speaker_id": 2,
            "current_speaker_id": 3,
        },
        {
            "type": "interruption",
            "start_ms": 11600,
            "end_ms": 11900,
            "start_frame": 1160,
            "end_frame": 1190,
            "interrupted_speaker_id": 1,
            "interrupting_speaker_id": 2,
            "resume_frame": 1190,
        },
    ]
    return floor_spans, voice_segments, injected_events


def _speaker_profile(config: EngineConfig, speaker_id: int) -> Dict[str, Any]:
    for speaker in config["speakers"]:
        if int(speaker["speaker_id"]) == int(speaker_id):
            return speaker
    raise KeyError(f"missing speaker profile: {speaker_id}")


def _synthesize_voice(
    n_samples: int,
    speaker: Dict[str, Any],
    sample_rate: int,
    rng: np.random.Generator,
) -> np.ndarray:
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    speaker_id = int(speaker["speaker_id"])
    base_f0 = float(speaker["f0_hz"])
    vibrato = 1.0 + 0.012 * np.sin(
        2.0 * np.pi * (4.2 + 0.17 * speaker_id) * t + 0.7 * speaker_id
    )
    contour = base_f0 * vibrato
    phase = 2.0 * np.pi * np.cumsum(contour, dtype=np.float64) / sample_rate

    source = np.zeros(n_samples, dtype=np.float64)
    for harmonic in range(1, 13):
        source += (1.0 / harmonic) * np.sin(harmonic * phase + 0.11 * harmonic)
    source /= 2.35

    formants = [float(x) for x in speaker["formants_hz"]]
    formant_signal = np.zeros(n_samples, dtype=np.float64)
    for index, formant_hz in enumerate(formants):
        amplitude = 0.24 / (index + 1)
        formant_signal += amplitude * np.sin(
            2.0 * np.pi * formant_hz * t + 0.31 * index + speaker_id
        )

    breath = rng.normal(0.0, 0.018, n_samples)
    breath *= 0.35 + 0.65 * np.sin(2.0 * np.pi * 0.8 * t) ** 2

    signal = 0.68 * source + 0.28 * formant_signal + 0.04 * breath

    attack = max(1, int(0.025 * sample_rate))
    release = max(1, int(0.040 * sample_rate))
    envelope = np.ones(n_samples, dtype=np.float64)
    attack_len = min(attack, n_samples // 2)
    release_len = min(release, n_samples // 2)
    if attack_len > 0:
        envelope[:attack_len] = np.linspace(0.0, 1.0, attack_len)
    if release_len > 0:
        envelope[-release_len:] = np.linspace(1.0, 0.0, release_len)
    syllable = 0.76 + 0.24 * np.sin(
        2.0 * np.pi * (2.7 + 0.23 * speaker_id) * t + 0.9 * speaker_id
    )
    syllable = np.clip(syllable, 0.38, 1.0)
    level = float(speaker["energy_level"])
    gain = float(speaker.get("amplitude_gain", 1.0)) if "amplitude_gain" in speaker else 1.0
    return signal * envelope * syllable * level * gain


def synthesize_pcm(config: EngineConfig) -> Tuple[np.ndarray, bytes]:
    sample_rate = int(config["sample_rate"])
    total_samples = int(round(float(config["duration_s"]) * sample_rate))
    pcm = np.zeros(total_samples, dtype=np.float64)
    rng = np.random.default_rng(int(config["seed"]))
    _, voice_segments, _ = default_timeline(config)

    for segment in voice_segments:
        start = int(segment["start_ms"]) * sample_rate // 1000
        end = int(segment["end_ms"]) * sample_rate // 1000
        profile = dict(_speaker_profile(config, int(segment["speaker_id"])))
        profile["amplitude_gain"] = segment.get("amplitude_gain", 1.0)
        voice = _synthesize_voice(end - start, profile, sample_rate, rng)
        pcm[start:end] += voice

    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak > 0.0:
        pcm = pcm * (0.89 / peak)
    int16_pcm = np.clip(np.rint(pcm * 32767.0), -32768, 32767).astype("<i2")
    return int16_pcm, int16_pcm.tobytes()


def expected_frame_count(config: EngineConfig, pcm_samples: int) -> int:
    frame_samples = config.frame_samples
    hop_samples = config.hop_samples
    if pcm_samples < frame_samples:
        return 0
    return 1 + (pcm_samples - frame_samples) // hop_samples


def _span_at(spans: Sequence[Dict[str, Any]], time_ms: int) -> Dict[str, Any]:
    for span in spans:
        if int(span["start_ms"]) <= time_ms < int(span["end_ms"]):
            return span
    return spans[-1]


def build_frame_truth(config: EngineConfig, pcm_samples: int) -> List[Dict[str, Any]]:
    sample_rate = int(config["sample_rate"])
    hop_samples = config.hop_samples
    frame_samples = config.frame_samples
    floor_spans, voice_segments, _ = default_timeline(config)
    frame_count = expected_frame_count(config, pcm_samples)
    rows: List[Dict[str, Any]] = []

    for frame_index in range(frame_count):
        start_sample = frame_index * hop_samples
        end_sample = start_sample + frame_samples
        start_ms = start_sample * 1000 // sample_rate
        end_ms = end_sample * 1000 // sample_rate
        floor = _span_at(floor_spans, start_ms)
        active_ids = sorted(
            {
                int(segment["speaker_id"])
                for segment in voice_segments
                if int(segment["start_ms"]) <= start_ms < int(segment["end_ms"])
            }
        )
        speaker_id = int(floor["speaker_id"])
        rows.append(
            {
                "frame_index": frame_index,
                "start_sample": start_sample,
                "end_sample": end_sample,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_id": speaker_id,
                "is_speech": speaker_id > 0,
                "turn_label": floor["turn_label"],
                "floor_kind": floor["kind"],
                "active_speaker_ids": active_ids,
            }
        )
    return rows


def canonical_emotion_labels(truth: Sequence[Dict[str, Any]], config: EngineConfig) -> List[str]:
    """Create the shared semantic annotation labels used by v1 and v2."""
    labels: List[str] = []
    for row in truth:
        speaker_id = int(row["speaker_id"])
        frame_index = int(row["frame_index"])
        kind = str(row.get("floor_kind", ""))
        if not row["is_speech"]:
            if "abnormal" in str(row.get("turn_label", "")):
                label = "sad"
            else:
                label = "calm"
        elif kind == "grab_floor":
            label = "angry" if frame_index % 17 < 12 else "happy"
        elif kind == "interruption":
            label = "angry"
        elif kind == "interruption_resume":
            label = "neutral"
        elif speaker_id == 2:
            label = "happy" if frame_index % 23 < 17 else "neutral"
        elif speaker_id == 3:
            label = "sad" if frame_index % 19 < 10 else "calm"
        else:
            label = "neutral" if frame_index % 29 < 20 else "calm"
        labels.append(label)
    return labels


def write_jsonl(path: str | Path, rows: Iterable[Dict[str, Any]]) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in rows:
        import json

        lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    from .canonical import atomic_write_bytes

    atomic_write_bytes(path, data)
    return sha256_hex(data)


def generate_sample_bundle(
    output_dir: str | Path, config: EngineConfig | None = None
) -> Dict[str, Any]:
    from .ledger import write_v1_ledger, write_v2_ledger

    config = config or EngineConfig.load()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    int16_pcm, pcm_bytes = synthesize_pcm(config)
    pcm_path = output_dir / str(config["pcm_filename"])
    pcm_path.write_bytes(pcm_bytes)
    pcm_hash = sha256_hex(pcm_bytes)

    truth = build_frame_truth(config, int16_pcm.size)
    truth_path = output_dir / str(config["truth_filename"])
    truth_hash = write_jsonl(truth_path, truth)

    labels = canonical_emotion_labels(truth, config)
    v1_path = output_dir / "emotion_annotations_v1.json"
    v1_info = write_v1_ledger(v1_path, truth, labels, config)

    v2_dir = output_dir / "emotion_annotations_v2"
    v2_info = write_v2_ledger(v2_dir, truth, labels, config, generation=1, revision=0)

    _, _, injected_events = default_timeline(config)
    atomic_write_json(output_dir / "injected_events.json", {"events": injected_events})
    config_payload = config.to_dict()
    config_payload["generated"] = {
        "pcm_samples": int(int16_pcm.size),
        "frame_count": len(truth),
        "pcm_sha256": pcm_hash,
        "truth_sha256": truth_hash,
        "v1_sha256": v1_info["file_sha256"],
        "v2_manifest_sha256": v2_info["manifest_sha256"],
    }
    atomic_write_json(output_dir / "config.json", config_payload)
    manifest = {
        "dialogue_id": config["dialogue_id"],
        "seed": int(config["seed"]),
        "sample_rate": int(config["sample_rate"]),
        "channels": 1,
        "sample_format": "s16le",
        "pcm_path": pcm_path.name,
        "pcm_samples": int(int16_pcm.size),
        "pcm_sha256": pcm_hash,
        "frame_count": len(truth),
        "frame_params": {
            "frame_ms": int(config["frame_ms"]),
            "hop_ms": int(config["hop_ms"]),
            "frame_samples": config.frame_samples,
            "hop_samples": config.hop_samples,
        },
        "files": {
            "truth": truth_path.name,
            "v1": v1_path.name,
            "v2_dir": v2_dir.name,
            "injected_events": "injected_events.json",
            "config": "config.json",
        },
        "hashes": {
            "truth": truth_hash,
            "v1": v1_info["file_sha256"],
            "v2_manifest": v2_info["manifest_sha256"],
        },
    }
    atomic_write_json(output_dir / "manifest.json", manifest)
    return manifest
