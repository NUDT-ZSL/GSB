"""Deterministic local synthesis of multi-speaker dialog and event audio."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np

from .catalog import ACTIVE_EVENT_TYPES, EVENT_TYPE_IDS
from .codes import BusinessCode
from .config import FEATURE_NAMES_FULL, FeatureConfig, ms_to_samples, validate_feature_config
from .errors import AedError
from .features import frame_count_for_samples
from .io import sha256_file, write_json, write_jsonl, write_wav_pcm16_mono
from .validation import validate_dialog_timeline

GENERATOR_VERSION = "deterministic-aed-synth-v1"

EVENT_NOMINAL_DURATION_S = {
    "knock": 0.38,
    "cough": 0.46,
    "keyboard": 0.58,
    "laughter": 0.66,
}


def _keyed_rng(*parts: Any) -> np.random.Generator:
    material = "|".join(str(part) for part in parts).encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(material).digest()[:8], "little")
    return np.random.default_rng(seed)


def _band_noise(rng: np.random.Generator, n_samples: int, sample_rate: int, low_hz: float, high_hz: float) -> np.ndarray:
    noise = rng.standard_normal(n_samples)
    if n_samples < 4:
        return noise
    spectrum = np.fft.rfft(noise)
    frequencies = np.fft.rfftfreq(n_samples, d=1.0 / sample_rate)
    mask = (frequencies >= low_hz) & (frequencies <= high_hz)
    spectrum[~mask] = 0.0
    filtered = np.fft.irfft(spectrum, n=n_samples)
    rms = float(np.sqrt(np.mean(filtered * filtered)))
    if rms <= 1e-12:
        return filtered
    return filtered / rms


def _normalize_peak(signal: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(signal))) if signal.size else 0.0
    if peak <= 1e-12:
        return signal
    return signal / peak


def make_event_waveform(
    event_type: str,
    duration_s: float,
    *,
    sample_rate: int = 16_000,
    seed: int = 0,
    variant: str = "canonical",
) -> np.ndarray:
    """Build one deterministic event waveform from a class-specific template."""

    if event_type not in ACTIVE_EVENT_TYPES:
        raise AedError(
            BusinessCode.E_EVENT_TYPE,
            f"unknown active event type {event_type!r}",
            field="event_type",
            details={"event_type": event_type},
        )
    if (
        isinstance(duration_s, bool)
        or not isinstance(duration_s, (int, float))
        or not math.isfinite(float(duration_s))
        or duration_s <= 0
    ):
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "event duration must be positive",
            field="event_duration",
        )

    n_samples = max(2, int(round(float(duration_s) * sample_rate)))
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    rng = _keyed_rng("event-template", GENERATOR_VERSION, event_type, seed, variant)
    signal = np.zeros(n_samples, dtype=np.float64)

    if event_type == "knock":
        for strike_index, (offset, weight) in enumerate(
            ((0.0, 1.0), (0.105, 0.78), (0.205, 0.62))
        ):
            start = int(round(offset * sample_rate))
            if start >= n_samples:
                break
            length = min(int(round(0.085 * sample_rate)), n_samples - start)
            local_t = np.arange(length, dtype=np.float64) / sample_rate
            envelope = np.exp(-42.0 * local_t)
            body = (
                0.85 * np.sin(2.0 * math.pi * 108.0 * local_t)
                + 0.48 * np.sin(2.0 * math.pi * 224.0 * local_t)
                + 0.20 * np.sin(2.0 * math.pi * 476.0 * local_t)
            )
            click = _band_noise(rng, length, sample_rate, 900.0, 6500.0)
            click *= np.exp(-700.0 * local_t)
            signal[start : start + length] += weight * (body + 0.32 * click) * envelope
        sustained = _band_noise(rng, n_samples, sample_rate, 45.0, 260.0)
        signal += 0.25 * sustained * (0.75 + 0.25 * np.sin(2.0 * math.pi * 4.0 * t))

    elif event_type == "cough":
        for burst_index, offset in enumerate((0.0, 0.20, 0.34)):
            start = int(round(offset * sample_rate))
            if start >= n_samples:
                break
            length = min(int(round(0.16 * sample_rate)), n_samples - start)
            local_t = np.arange(length, dtype=np.float64) / sample_rate
            attack = np.minimum(1.0, local_t / 0.008)
            envelope = attack * np.exp(-13.5 * local_t)
            broadband = _band_noise(rng, length, sample_rate, 350.0, 5600.0)
            resonance = (
                np.sin(2.0 * math.pi * 155.0 * local_t)
                + 0.55 * np.sin(2.0 * math.pi * 330.0 * local_t)
            )
            weight = 1.0 if burst_index == 0 else (0.55 if burst_index == 1 else 0.35)
            signal[start : start + length] += weight * envelope * (0.72 * broadband + 0.56 * resonance)
        sustained = _band_noise(rng, n_samples, sample_rate, 350.0, 5200.0)
        signal += 0.24 * sustained * (0.72 + 0.28 * np.sin(2.0 * math.pi * 2.7 * t))

    elif event_type == "keyboard":
        cursor = 0.0
        click_index = 0
        while cursor < duration_s - 0.01:
            start = int(round(cursor * sample_rate))
            length = min(int(round(0.012 * sample_rate)), n_samples - start)
            if length <= 1:
                break
            local_t = np.arange(length, dtype=np.float64) / sample_rate
            envelope = np.exp(-320.0 * local_t)
            click = _band_noise(rng, length, sample_rate, 2200.0, 7600.0)
            tone = np.sin(2.0 * math.pi * (3100.0 + 65.0 * click_index) * local_t)
            signal[start : start + length] += (0.78 * click + 0.34 * tone) * envelope
            cursor += float(rng.uniform(0.047, 0.078))
            click_index += 1
        sustained = _band_noise(rng, n_samples, sample_rate, 2600.0, 7600.0)
        signal += 0.22 * sustained

    elif event_type == "laughter":
        offsets = (0.0, 0.145, 0.285, 0.425, 0.560)
        for burst_index, offset in enumerate(offsets):
            start = int(round(offset * sample_rate))
            if start >= n_samples:
                break
            length = min(int(round(0.105 * sample_rate)), n_samples - start)
            local_t = np.arange(length, dtype=np.float64) / sample_rate
            f0 = 158.0 + 14.0 * (burst_index % 3) + 5.0 * math.sin(burst_index)
            envelope = np.sin(math.pi * np.clip(local_t / (length / sample_rate), 0.0, 1.0)) ** 1.5
            phase = 2.0 * math.pi * f0 * local_t
            voiced = np.zeros(length, dtype=np.float64)
            for harmonic in range(1, 9):
                harmonic_frequency = harmonic * f0
                formant_weight = (
                    0.20
                    + math.exp(-0.5 * ((harmonic_frequency - 780.0) / 190.0) ** 2)
                    + 0.82 * math.exp(-0.5 * ((harmonic_frequency - 2050.0) / 300.0) ** 2)
                    + 0.38 * math.exp(-0.5 * ((harmonic_frequency - 3150.0) / 380.0) ** 2)
                )
                voiced += formant_weight * np.sin(harmonic * phase) / (harmonic ** 0.8)
            breath = 0.035 * _band_noise(rng, length, sample_rate, 300.0, 3600.0)
            signal[start : start + length] += (0.55 * voiced + breath) * envelope
        sustained = _band_noise(rng, n_samples, sample_rate, 260.0, 3300.0)
        signal += 0.18 * sustained

    return _normalize_peak(signal)


def _speaker_parameters(rng: np.random.Generator, speaker_index: int) -> dict[str, Any]:
    f1 = float(rng.uniform(330.0, 820.0))
    f2 = float(rng.uniform(980.0, 2350.0))
    f3 = float(rng.uniform(2250.0, 3400.0))
    return {
        "speaker_id": f"spk_{speaker_index:02d}",
        "f0": float(rng.uniform(92.0, 205.0)),
        "formants": (f1, f2, f3),
        "bandwidths": (85.0, 150.0, 230.0),
        "syllable_rate": float(rng.uniform(2.9, 4.7)),
        "phase": float(rng.uniform(0.0, 2.0 * math.pi)),
        "rms": float(rng.uniform(0.085, 0.115)),
    }


def _synthesize_voice(
    params: dict[str, Any],
    n_samples: int,
    sample_rate: int,
    rng: np.random.Generator,
) -> np.ndarray:
    if n_samples <= 1:
        return np.zeros(max(0, n_samples), dtype=np.float64)
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    f0 = params["f0"] * (1.0 + 0.035 * np.sin(2.0 * math.pi * 0.73 * t + params["phase"]))
    f0 *= 1.0 + 0.012 * np.sin(2.0 * math.pi * 3.1 * t)
    phase = 2.0 * math.pi * np.cumsum(f0) / sample_rate

    voice = np.zeros(n_samples, dtype=np.float64)
    max_harmonic = min(14, int((sample_rate / 2.0) / max(60.0, params["f0"])))
    for harmonic in range(1, max_harmonic + 1):
        harmonic_frequency = harmonic * params["f0"]
        weight = 0.13
        for formant, bandwidth in zip(params["formants"], params["bandwidths"]):
            weight += math.exp(-0.5 * ((harmonic_frequency - formant) / bandwidth) ** 2)
        voice += weight * np.sin(harmonic * phase + 0.07 * harmonic) / (harmonic ** 0.72)

    syllable = (
        0.25
        + 0.75
        * (
            0.5
            + 0.5
            * np.sin(2.0 * math.pi * params["syllable_rate"] * t + params["phase"])
        )
        ** 1.6
    )
    voice *= syllable
    voice += 0.012 * _band_noise(rng, n_samples, sample_rate, 180.0, 1700.0)

    fade_samples = min(int(round(0.018 * sample_rate)), n_samples // 2)
    if fade_samples > 1:
        fade = np.linspace(0.0, 1.0, fade_samples, endpoint=False)
        voice[:fade_samples] *= fade
        voice[-fade_samples:] *= fade[::-1]

    rms = float(np.sqrt(np.mean(voice * voice)))
    if rms > 1e-12:
        voice *= float(params["rms"]) / rms
    return voice


def make_speaker_calibration_waveform(
    variant_index: int,
    *,
    duration_s: float = 1.0,
    sample_rate: int = 16_000,
) -> np.ndarray:
    """Deterministic speech-like waveform for background model calibration."""

    rng = _keyed_rng("speaker-calibration", GENERATOR_VERSION, variant_index)
    params = _speaker_parameters(rng, variant_index)
    n_samples = max(2, int(round(duration_s * sample_rate)))
    waveform = _synthesize_voice(
        params,
        n_samples,
        sample_rate,
        _keyed_rng("speaker-calibration-voice", GENERATOR_VERSION, variant_index),
    )
    return waveform


def _build_turns(
    rng: np.random.Generator,
    n_samples: int,
    sample_rate: int,
    speaker_ids: list[str],
    dialog_index: int,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    minimum_tail = int(round(0.10 * sample_rate))
    cursor = int(round(float(rng.uniform(0.08, 0.18)) * sample_rate))
    speaker_cursor = dialog_index % len(speaker_ids)
    while cursor < n_samples - minimum_tail:
        remaining_s = (n_samples - cursor) / sample_rate
        if remaining_s < 0.22:
            break
        turn_s = min(float(rng.uniform(0.34, 0.72)), remaining_s - 0.06)
        if turn_s < 0.20:
            break
        start = cursor
        end = min(n_samples, start + int(round(turn_s * sample_rate)))
        turns.append(
            {
                "turn_id": f"turn_{len(turns):02d}",
                "speaker_id": speaker_ids[speaker_cursor % len(speaker_ids)],
                "start_sample": int(start),
                "end_sample": int(end),
            }
        )
        speaker_cursor += 1
        gap_s = float(rng.uniform(0.07, 0.18))
        cursor = end + int(round(gap_s * sample_rate))
    if not turns:
        start = min(int(round(0.10 * sample_rate)), max(0, n_samples - 1))
        end = min(n_samples, start + max(2, int(round(0.35 * sample_rate))))
        turns.append(
            {
                "turn_id": "turn_00",
                "speaker_id": speaker_ids[0],
                "start_sample": start,
                "end_sample": end,
            }
        )
    return turns


def _place_events(
    rng: np.random.Generator,
    n_samples: int,
    sample_rate: int,
    dialog_index: int,
    dialog_id: str,
) -> list[dict[str, Any]]:
    duration_s = n_samples / sample_rate
    if duration_s >= 1.2:
        event_count = len(ACTIVE_EVENT_TYPES)
    else:
        event_count = min(
            len(ACTIVE_EVENT_TYPES),
            max(1, int(round(duration_s / 0.5))),
        )
    rotated = list(ACTIVE_EVENT_TYPES[dialog_index % len(ACTIVE_EVENT_TYPES) :]) + list(
        ACTIVE_EVENT_TYPES[: dialog_index % len(ACTIVE_EVENT_TYPES)]
    )
    selected_types = rotated[:event_count]
    events: list[dict[str, Any]] = []
    for event_index, event_type in enumerate(selected_types):
        slot_start = int(round(event_index * n_samples / event_count))
        slot_end = int(round((event_index + 1) * n_samples / event_count))
        margin = int(round(0.035 * sample_rate))
        usable_samples = max(0, slot_end - slot_start - 2 * margin)
        nominal_samples = int(round(EVENT_NOMINAL_DURATION_S[event_type] * sample_rate))
        duration_samples = min(nominal_samples, usable_samples)
        if duration_samples < int(round(0.18 * sample_rate)):
            continue
        extra = usable_samples - duration_samples
        start = slot_start + margin + int(round(float(rng.uniform(0.0, max(0, extra)))))
        end = start + duration_samples
        gain = float(rng.uniform(0.90, 1.10))
        waveform = make_event_waveform(
            event_type,
            duration_samples / sample_rate,
            sample_rate=sample_rate,
            seed=0x5EED,
            variant=f"{dialog_id}:{event_index}",
        )
        events.append(
            {
                "event_id": f"{dialog_id}_evt_{event_index:02d}",
                "event_type": event_type,
                "start_sample": int(start),
                "end_sample": int(end),
                "gain": round(gain, 6),
                "waveform": waveform * gain,
            }
        )
    return events


def _frame_truth(
    n_samples: int,
    sample_rate: int,
    hop_samples: int,
    turns: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    frame_count = frame_count_for_samples(n_samples, hop_samples)
    rows: list[dict[str, Any]] = []
    for frame_index in range(frame_count):
        cell_start = frame_index * hop_samples
        cell_end = min(n_samples, cell_start + hop_samples)
        center = (cell_start + cell_end) // 2

        active_speaker: str | None = None
        for turn in turns:
            if turn["start_sample"] <= center < turn["end_sample"]:
                active_speaker = turn["speaker_id"]
                break

        best_event: dict[str, Any] | None = None
        best_overlap = 0
        for event in events:
            overlap = max(
                0,
                min(cell_end, event["end_sample"]) - max(cell_start, event["start_sample"]),
            )
            if overlap <= 0:
                continue
            if (
                best_event is None
                or overlap > best_overlap
                or (
                    overlap == best_overlap
                    and (
                        EVENT_TYPE_IDS[event["event_type"]],
                        event["event_id"],
                    )
                    < (
                        EVENT_TYPE_IDS[best_event["event_type"]],
                        best_event["event_id"],
                    )
                )
            ):
                best_event = event
                best_overlap = overlap

        rows.append(
            {
                "frame_index": frame_index,
                "event_id": best_event["event_id"] if best_event else None,
                "event_type": best_event["event_type"] if best_event else "background",
                "speaker_id": active_speaker,
                "is_speech": active_speaker is not None,
            }
        )
    return rows


def generate_dataset(
    output_dir: str | Path,
    *,
    seed: int = 20261121,
    num_dialogs: int = 2,
    duration_ms: int = 2_200,
    speaker_count: int = 3,
    frame_ms: float = 20.0,
    hop_ms: float = 10.0,
) -> dict[str, Any]:
    """Generate WAV, truth JSONL, dialog manifest and reproducibility metadata."""

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "seed must be an integer",
            field="seed",
        )
    if isinstance(num_dialogs, bool) or not isinstance(num_dialogs, int) or num_dialogs < 1:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "num_dialogs must be a positive integer",
            field="num_dialogs",
        )
    if (
        isinstance(speaker_count, bool)
        or not isinstance(speaker_count, int)
        or not 1 <= speaker_count <= 8
    ):
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "speaker_count must be an integer in [1, 8]",
            field="speaker_count",
        )
    config = FeatureConfig(frame_ms=frame_ms, hop_ms=hop_ms, feature_mode="full")
    validate_feature_config(config)
    n_samples = ms_to_samples(duration_ms, config.sample_rate, field="duration_ms")
    if n_samples < config.frame_samples:
        raise AedError(
            BusinessCode.E_TIMELINE_INVALID,
            "dialog duration is shorter than one frame",
            field="duration_ms",
            details={"duration_ms": duration_ms, "frame_ms": frame_ms},
        )

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    dialog_root = root / "dialogs"
    dialog_root.mkdir(parents=True, exist_ok=True)

    master_rng = np.random.default_rng(seed)
    speaker_params = [_speaker_parameters(master_rng, index) for index in range(speaker_count)]
    speaker_ids = [params["speaker_id"] for params in speaker_params]
    manifest_rows: list[dict[str, Any]] = []
    dialog_metadata: list[dict[str, Any]] = []

    for dialog_index in range(num_dialogs):
        dialog_id = f"dialog_{dialog_index:03d}"
        rng = _keyed_rng("dialog", GENERATOR_VERSION, seed, dialog_id)
        turns = _build_turns(rng, n_samples, config.sample_rate, speaker_ids, dialog_index)
        events = _place_events(rng, n_samples, config.sample_rate, dialog_index, dialog_id)
        validate_dialog_timeline(
            {"turns": turns, "events": events},
            dialog_id=dialog_id,
            duration_ms=duration_ms,
            sample_rate=config.sample_rate,
        )

        pcm = np.zeros(n_samples, dtype=np.float64)
        for turn in turns:
            start = turn["start_sample"]
            end = turn["end_sample"]
            speaker_index = speaker_ids.index(turn["speaker_id"])
            voice_rng = _keyed_rng(
                "voice", GENERATOR_VERSION, seed, dialog_id, turn["turn_id"], turn["speaker_id"]
            )
            voice = _synthesize_voice(
                speaker_params[speaker_index],
                end - start,
                config.sample_rate,
                voice_rng,
            )
            pcm[start:end] += voice

        ambient_rng = _keyed_rng("ambient", GENERATOR_VERSION, seed, dialog_id)
        pcm += 0.0012 * _band_noise(ambient_rng, n_samples, config.sample_rate, 60.0, 1800.0)

        for event in events:
            pcm[event["start_sample"] : event["end_sample"]] += event.pop("waveform")

        peak = float(np.max(np.abs(pcm)))
        if peak > 0.98:
            pcm *= 0.98 / peak

        dialog_dir = dialog_root / dialog_id
        dialog_dir.mkdir(parents=True, exist_ok=True)
        wav_path = dialog_dir / "audio.wav"
        truth_path = dialog_dir / "frames.jsonl"
        write_wav_pcm16_mono(wav_path, pcm, config.sample_rate)
        truth_rows = _frame_truth(
            n_samples,
            config.sample_rate,
            config.hop_samples,
            turns,
            events,
        )
        write_jsonl(truth_path, truth_rows)

        manifest_rows.append(
            {
                "dialog_id": dialog_id,
                "sample_rate": config.sample_rate,
                "duration_ms": duration_ms,
                "modality_mask": ["audio"],
                "wav_path": str(wav_path.relative_to(root)),
                "truth_path": str(truth_path.relative_to(root)),
                "speaker_count": speaker_count,
                "seed": seed,
                "frame_ms": frame_ms,
                "hop_ms": hop_ms,
            }
        )
        dialog_metadata.append(
            {
                "dialog_id": dialog_id,
                "speakers": speaker_params,
                "turns": turns,
                "events": [
                    {
                        "event_id": event["event_id"],
                        "event_type": event["event_type"],
                        "start_sample": event["start_sample"],
                        "end_sample": event["end_sample"],
                        "gain": event["gain"],
                    }
                    for event in events
                ],
                "frame_count": len(truth_rows),
                "wav_sha256": sha256_file(wav_path),
                "truth_sha256": sha256_file(truth_path),
            }
        )

    manifest_path = root / "manifest.jsonl"
    write_jsonl(manifest_path, manifest_rows)
    metadata = {
        "generator": GENERATOR_VERSION,
        "seed": seed,
        "num_dialogs": num_dialogs,
        "duration_ms": duration_ms,
        "speaker_count": speaker_count,
        "sample_rate": config.sample_rate,
        "frame_ms": frame_ms,
        "hop_ms": hop_ms,
        "feature_names": list(FEATURE_NAMES_FULL),
        "frame_count_rule": "ceil(pcm_samples / hop_samples); final window zero-padded",
        "dialogs": dialog_metadata,
    }
    write_json(root / "metadata.json", metadata)
    return {
        "dataset_dir": str(root),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "metadata_path": str(root / "metadata.json"),
        "num_dialogs": num_dialogs,
        "speaker_count": speaker_count,
        "frame_count": sum(item["frame_count"] for item in dialog_metadata),
        "dialog_ids": [row["dialog_id"] for row in manifest_rows],
    }
