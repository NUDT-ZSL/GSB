"""Deterministic three-version museum narration fixture generator."""

from __future__ import annotations

import hashlib
import math
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .catalog import PHONEME_SPECS, phoneme_reference, source_label
from .errors import fail
from .io_utils import clean_nested, prepare_staging, replace_directory, write_json


MILLISECOND_US = 1_000
FRAME_UNIT_US = 40_000
FRAME_UNIT_FPS = 25.0
SAMPLE_RATE = 48_000


@dataclass(frozen=True)
class SentenceSeed:
    text: str
    phonemes: tuple[str, ...]
    units: tuple[int, ...]
    emotion_label: str
    emotion_intensity: float
    emotion_anchor_index: int
    stress: tuple[tuple[int, float], ...]


SENTENCE_SEEDS: tuple[SentenceSeed, ...] = (
    SentenceSeed(
        "青铜礼器铭文记录着祭祀与册命。",
        ("SIL", "B", "AA", "M", "IY", "P", "UW", "T", "AA", "N", "SIL"),
        (3, 6, 1, 6, 1, 6, 1, 1, 1, 1, 3),
        "neutral",
        0.0,
        8,
        ((8, 0.45),),
    ),
    SentenceSeed(
        "纹饰的细节在灯下渐渐清晰。",
        ("SIL", "F", "AH", "SH", "IY", "N", "IH", "L", "IY", "T", "AA", "SIL"),
        (3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 3),
        "joy",
        0.72,
        7,
        ((7, 0.75),),
    ),
    SentenceSeed(
        "这一段语速骤然放慢，留给观众观察。",
        (
            "SIL",
            "K",
            "AH",
            "T",
            "IH",
            "P",
            "AH",
            "M",
            "IY",
            "P",
            "UW",
            "B",
            "AA",
            "SIL",
        ),
        (2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 7, 3),
        "surprise",
        0.55,
        12,
        ((4, 0.5), (12, 0.85)),
    ),
    SentenceSeed(
        "展签上的年代标记仍有争议。",
        ("SIL", "D", "IH", "F", "AH", "N", "EY", "B", "AA", "P", "UW", "SIL"),
        (2, 1, 1, 1, 1, 1, 2, 4, 2, 4, 3, 3),
        "joy",
        0.9,
        2,
        ((10, 0.95),),
    ),
    SentenceSeed(
        "请沿着展线慢慢观看。",
        ("SIL", "L", "AA", "N", "G", "ER", "SH", "AH", "N", "SIL"),
        (2, 2, 1, 1, 1, 1, 3, 1, 1, 2),
        "sadness",
        0.62,
        6,
        ((6, 0.8),),
    ),
    SentenceSeed(
        "闭唇之后立即回到元音。",
        ("SIL", "M", "AA", "M", "IY", "B", "OW", "P", "AA", "M", "UW", "SIL"),
        (2, 6, 1, 6, 1, 6, 1, 6, 2, 6, 2, 3),
        "neutral",
        0.0,
        9,
        ((3, 0.35), (9, 0.65)),
    ),
    SentenceSeed(
        "本次讲解到此结束。",
        ("SIL", "HH", "UW", "T", "AH", "NG", "SIL"),
        (3, 1, 3, 1, 1, 2, 4),
        "concern",
        0.45,
        4,
        ((4, 0.72),),
    ),
)


def _build_master() -> dict[str, Any]:
    sentences: list[dict[str, Any]] = []
    cursor_us = 0
    for sentence_index, seed in enumerate(SENTENCE_SEEDS, start=1):
        if len(seed.phonemes) != len(seed.units):
            raise RuntimeError("fixture phoneme/duration length mismatch")
        sentence_id = f"S{sentence_index:02d}"
        phonemes: list[dict[str, Any]] = []
        sentence_start = cursor_us
        for phoneme_index, (canonical, units) in enumerate(
            zip(seed.phonemes, seed.units), start=1
        ):
            if canonical not in PHONEME_SPECS:
                raise RuntimeError(f"unknown fixture phoneme {canonical}")
            duration_us = units * FRAME_UNIT_US
            reference = phoneme_reference(canonical)
            phonemes.append(
                {
                    "id": f"{sentence_id}P{phoneme_index:02d}",
                    "canonical": canonical,
                    "category": reference["category"],
                    "viseme": reference["viseme"],
                    "start_us": cursor_us,
                    "end_us": cursor_us + duration_us,
                    "reference_targets": reference["targets"],
                }
            )
            cursor_us += duration_us
        anchor = phonemes[seed.emotion_anchor_index]
        stress = [
            {
                "phoneme_id": phonemes[index]["id"],
                "level": level,
            }
            for index, level in seed.stress
        ]
        sentences.append(
            {
                "id": sentence_id,
                "text": seed.text,
                "start_us": sentence_start,
                "end_us": cursor_us,
                "emotion": {
                    "label": seed.emotion_label,
                    "intensity": seed.emotion_intensity,
                    "anchor_phoneme_id": anchor["id"],
                },
                "stress": stress,
                "phonemes": phonemes,
            }
        )
    return {
        "format": "museum-lipsync-ground-truth/v1",
        "time_base": "microseconds",
        "sample_rate_hz": SAMPLE_RATE,
        "duration_us": cursor_us,
        "sentences": sentences,
    }


def _frequency_for(canonical: str) -> float:
    spec = PHONEME_SPECS[canonical]
    if spec.viseme == "AA":
        return 155.0
    if spec.viseme == "E":
        return 205.0
    if spec.viseme == "I":
        return 235.0
    if spec.viseme == "O":
        return 150.0
    if spec.viseme == "U":
        return 130.0
    return 115.0


def _synthesize_audio(master: dict[str, Any], seed: int) -> np.ndarray:
    duration_us = int(master["duration_us"])
    sample_count = int(round(duration_us * SAMPLE_RATE / 1_000_000))
    signal = np.zeros(sample_count, dtype=np.float64)
    rng = np.random.default_rng(seed)

    for sentence in master["sentences"]:
        for phoneme in sentence["phonemes"]:
            start = int(round(phoneme["start_us"] * SAMPLE_RATE / 1_000_000))
            end = int(round(phoneme["end_us"] * SAMPLE_RATE / 1_000_000))
            if end <= start:
                continue
            count = end - start
            t = np.arange(count, dtype=np.float64) / SAMPLE_RATE
            canonical = phoneme["canonical"]
            spec = PHONEME_SPECS[canonical]
            if canonical == "SIL":
                local = rng.normal(0.0, 0.0008, count)
            elif spec.viseme == "PP" and canonical in {"B", "P"}:
                local = rng.normal(0.0, 0.006, count)
            elif spec.category.endswith("fricative") or spec.category.endswith("affricate"):
                local = rng.normal(0.0, 0.018, count)
                local *= 0.7 + 0.3 * np.sin(2.0 * math.pi * 9.0 * t)
            else:
                frequency = _frequency_for(canonical)
                harmonic = np.sin(2.0 * math.pi * frequency * t)
                harmonic += 0.35 * np.sin(2.0 * math.pi * 2.0 * frequency * t + 0.2)
                harmonic += 0.15 * np.sin(2.0 * math.pi * 3.0 * frequency * t + 0.5)
                local = 0.035 * harmonic
            fade_count = min(count, max(1, int(0.008 * SAMPLE_RATE)))
            fade = np.linspace(0.0, 1.0, fade_count, endpoint=False)
            local[:fade_count] *= fade
            local[-fade_count:] *= fade[::-1]
            signal[start:end] += local

    signal = np.tanh(signal * 8.0) * 0.18
    return np.asarray(np.clip(signal, -1.0, 1.0) * 32767.0, dtype="<i2")


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def _to_ms(value_us: int) -> int:
    if value_us % MILLISECOND_US:
        # Generated fixtures always use exact 40 ms frame units.
        raise RuntimeError("fixture time is not an integer millisecond")
    return value_us // MILLISECOND_US


def _to_frame(value_us: int) -> int:
    numerator = value_us * int(FRAME_UNIT_FPS)
    if numerator % 1_000_000:
        raise RuntimeError("fixture time is not an exact 25 fps frame boundary")
    return numerator // 1_000_000


VERSION_SPECS: tuple[dict[str, Any], ...] = (
    {
        "version": "A",
        "directory": "version_a",
        "style": "arpabet",
        "time_base": "milliseconds",
        "fps": 24.0,
        "label_key": "arpabet",
        "channel_bindings": (
            ("Jaw_Open", "jaw_open"),
            ("Lip_Width", "lip_width"),
            ("Mouth_Corner", "mouth_corner"),
            ("Brow_Raise", "brow_raise"),
            ("Brow_Inner_Up", "brow_inner_up"),
        ),
    },
    {
        "version": "B",
        "directory": "version_b",
        "style": "ipa",
        "time_base": "frames",
        "fps": 25.0,
        "label_key": "ipa",
        "channel_bindings": (
            ("jawOpen", "jaw_open"),
            ("mouthWidth", "lip_width"),
            ("mouthCorner", "mouth_corner"),
            ("browRaise", "brow_raise"),
            ("browInnerUp", "brow_inner_up"),
        ),
    },
    {
        "version": "C",
        "directory": "version_c",
        "style": "legacy",
        "time_base": "milliseconds",
        "fps": 30.0,
        "label_key": "legacy",
        "channel_bindings": (
            ("CTRL_JAW", "jaw_open"),
            ("CTRL_LIP_W", "lip_width"),
            ("CTRL_MOUTH_L", "mouth_corner"),
            ("CTRL_BROW", "brow_raise"),
            ("CTRL_BROW_IN", "brow_inner_up"),
        ),
    },
)


def _version_annotation(
    master: dict[str, Any], version_spec: dict[str, Any]
) -> dict[str, Any]:
    style = version_spec["label_key"]
    time_base = version_spec["time_base"]
    sentences_payload: list[dict[str, Any]] = []
    for sentence in master["sentences"]:
        sentence_payload: dict[str, Any] = {
            "id": sentence["id"],
            "text": sentence["text"],
            "emotion": dict(sentence["emotion"]),
            "stress": [dict(item) for item in sentence["stress"]],
            "phonemes": [],
        }
        if time_base == "frames":
            sentence_payload["start_frame"] = _to_frame(sentence["start_us"])
            sentence_payload["end_frame"] = _to_frame(sentence["end_us"])
        else:
            sentence_payload["start_ms"] = _to_ms(sentence["start_us"])
            sentence_payload["end_ms"] = _to_ms(sentence["end_us"])
        for phoneme in sentence["phonemes"]:
            source = {
                "id": phoneme["id"],
                "label": source_label(phoneme["canonical"], style),
            }
            if time_base == "frames":
                source["start_frame"] = _to_frame(phoneme["start_us"])
                source["end_frame"] = _to_frame(phoneme["end_us"])
            else:
                source["start_ms"] = _to_ms(phoneme["start_us"])
                source["end_ms"] = _to_ms(phoneme["end_us"])
            sentence_payload["phonemes"].append(source)
        sentences_payload.append(sentence_payload)
    return {
        "format": "museum-lipsync-annotation/v1",
        "project": {
            "version": version_spec["version"],
            "label_style": version_spec["style"],
            "time_base": time_base,
            "fps": version_spec["fps"],
            "audio_file": "narration.wav",
            "audio_sample_rate": SAMPLE_RATE,
            "duration_ms": _to_ms(master["duration_us"]),
            "channel_map_file": "bindings.json",
        },
        "sentences": sentences_payload,
    }


def _version_bindings(version_spec: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for source, canonical in version_spec["channel_bindings"]:
        rows.append(
            {
                "source": source,
                "canonical": canonical,
                "scale": 1.0,
                "offset": 0.0,
                "neutral": 0.0,
            }
        )
    return {
        "format": "museum-lipsync-bindings/v1",
        "version": version_spec["version"],
        "fps": version_spec["fps"],
        "conflict_policy": "stress_over_emotion",
        "channel_bindings": rows,
    }


def _ground_truth_payload(master: dict[str, Any], seed: int) -> dict[str, Any]:
    sentences = []
    for sentence in master["sentences"]:
        sentence_out = {
            "id": sentence["id"],
            "text": sentence["text"],
            "start_ms": _to_ms(sentence["start_us"]),
            "end_ms": _to_ms(sentence["end_us"]),
            "emotion": dict(sentence["emotion"]),
            "stress": [dict(item) for item in sentence["stress"]],
            "phonemes": [],
        }
        for phoneme in sentence["phonemes"]:
            sentence_out["phonemes"].append(
                {
                    "id": phoneme["id"],
                    "canonical": phoneme["canonical"],
                    "category": phoneme["category"],
                    "viseme": phoneme["viseme"],
                    "start_ms": _to_ms(phoneme["start_us"]),
                    "end_ms": _to_ms(phoneme["end_us"]),
                    "reference_targets": dict(phoneme["reference_targets"]),
                }
            )
        sentences.append(sentence_out)
    return {
        "format": "museum-lipsync-ground-truth/v1",
        "seed": seed,
        "time_base": "milliseconds",
        "canonical_time_base": "microseconds",
        "sample_rate_hz": SAMPLE_RATE,
        "duration_ms": _to_ms(master["duration_us"]),
        "conflict_policy": "stress_over_emotion",
        "label_styles": {"A": "arpabet", "B": "ipa", "C": "legacy"},
        "sentences": sentences,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_samples(
    output_dir: str | Path,
    *,
    seed: int = 20261128,
) -> dict[str, Any]:
    """Generate the fixed three-version fixture and return its manifest."""
    destination = Path(output_dir).expanduser().resolve()
    master = _build_master()
    audio = _synthesize_audio(master, seed)
    truth = clean_nested(_ground_truth_payload(master, seed))
    staging = prepare_staging(destination)
    try:
        version_dirs: dict[str, str] = {}
        for version_spec in VERSION_SPECS:
            directory = staging / version_spec["directory"]
            directory.mkdir(parents=True, exist_ok=True)
            annotation = clean_nested(
                _version_annotation(master, version_spec)
            )
            bindings = clean_nested(_version_bindings(version_spec))
            write_json(directory / "annotation.json", annotation)
            write_json(directory / "bindings.json", bindings)
            _write_wav(directory / "narration.wav", audio)
            version_dirs[version_spec["version"]] = version_spec["directory"]

        write_json(staging / "ground_truth.json", truth)
        manifest = {
            "format": "museum-lipsync-samples/v1",
            "seed": seed,
            "sample_rate_hz": SAMPLE_RATE,
            "duration_ms": _to_ms(master["duration_us"]),
            "versions": version_dirs,
            "ground_truth": "ground_truth.json",
            "audio_sha256": _file_sha256(staging / "version_a" / "narration.wav"),
            "contains_cases": [
                "bilabial_closure_next_to_vowel",
                "abrupt_speech_rate_change",
                "emotion_stress_conflict",
            ],
        }
        write_json(staging / "manifest.json", manifest)
        replace_directory(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    manifest_path = destination / "manifest.json"
    if not manifest_path.exists():
        fail("E_OUTPUT", "sample manifest was not produced", path=str(manifest_path))
    return {
        **manifest,
        "output_dir": str(destination),
        "version_paths": {
            key: str(destination / value) for key, value in manifest["versions"].items()
        },
    }
