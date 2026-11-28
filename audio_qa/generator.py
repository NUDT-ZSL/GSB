"""Deterministic synthetic archive-audio corpus and manifest generator."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .grading import (
    THRESHOLD_TABLE,
    grade_clipping,
    grade_dc_offset,
    grade_impulse,
    grade_noise,
    segment_overall_grade,
)
from .signal_utils import db_to_linear, true_runs
from .wav import write_mono_wav
from .workflow import map_workflow


SAMPLE_RATE = 16000
SEGMENT_SECONDS = 6.0
TRUTH_FRAME_LENGTH = 256
TRUTH_HOP = 64
BASE_NOISE_DBFS = -72.0


def _rng(seed: int, recording_index: int, segment_index: int) -> np.random.Generator:
    return np.random.default_rng(int(seed) * 1_000_003 + recording_index * 1009 + segment_index * 9176)


def _smooth_gate(n: int, start: int, end: int, sample_rate: int, ramp_ms: float = 20.0) -> np.ndarray:
    gate = np.zeros(n, dtype=np.float64)
    start = max(0, min(int(start), n))
    end = max(start, min(int(end), n))
    if end <= start:
        return gate
    gate[start:end] = 1.0
    ramp = max(1, int(round(ramp_ms * sample_rate / 1000.0)))
    ramp = min(ramp, (end - start) // 2) if (end - start) > 2 else 1
    if ramp >= 2:
        shape = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, ramp, endpoint=False))
        gate[start : start + ramp] *= shape
        gate[end - ramp : end] *= shape[::-1]
    return gate


def _synth_music(n: int, sample_rate: int, rng: np.random.Generator) -> np.ndarray:
    x = np.zeros(n, dtype=np.float64)
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    notes = [220.0, 246.94, 293.66, 329.63, 392.0, 440.0, 523.25, 587.33, 659.25, 783.99]
    cursor = int(0.12 * sample_rate)
    lead_in = cursor
    while cursor < n:
        note_len = int(round(rng.uniform(0.105, 0.155) * sample_rate))
        gap_len = int(round(rng.uniform(0.045, 0.075) * sample_rate))
        note_len = min(note_len, n - cursor)
        if note_len <= 8:
            break
        tt = np.arange(note_len, dtype=np.float64) / float(sample_rate)
        attack = rng.uniform(0.0025, 0.006)
        decay = rng.uniform(0.070, 0.130)
        release = min(note_len / sample_rate, rng.uniform(0.035, 0.070))
        envelope = (1.0 - np.exp(-tt / attack)) * np.exp(-tt / decay)
        if release > 0:
            release_start = max(0.0, note_len / sample_rate - release)
            release_mask = tt >= release_start
            envelope[release_mask] *= 0.5 + 0.5 * np.cos(
                np.pi * (tt[release_mask] - release_start) / release
            )
        base = notes[int(rng.integers(0, len(notes)))]
        vibrato = 1.0 + 0.0015 * np.sin(2.0 * np.pi * rng.uniform(4.5, 6.5) * tt)
        phase = rng.uniform(0.0, 2.0 * np.pi)
        partials = [
            (1.0, 1.0, 1.0),
            (2.0, 0.42, 1.3),
            (3.0, 0.22, 1.7),
            (4.0, 0.12, 2.1),
            (5.0, 0.07, 2.6),
        ]
        waveform = np.zeros(note_len, dtype=np.float64)
        for multiple, amplitude, phase_scale in partials:
            waveform += amplitude * np.sin(
                2.0 * np.pi * base * multiple * np.cumsum(vibrato) / sample_rate
                + phase * phase_scale
            )
        amplitude = rng.uniform(0.26, 0.38)
        x[cursor : cursor + note_len] += amplitude * envelope * waveform
        if rng.random() < 0.30 and cursor + note_len + gap_len < n:
            fifth = notes[min(len(notes) - 1, notes.index(base) + 3)]
            overlay_len = min(note_len, int(round(0.075 * sample_rate)))
            to = np.arange(overlay_len, dtype=np.float64) / float(sample_rate)
            env_o = (1.0 - np.exp(-to / 0.004)) * np.exp(-to / 0.080)
            x[cursor : cursor + overlay_len] += (
                0.14 * env_o * np.sin(2.0 * np.pi * fifth * to + phase * 0.7)
            )
        cursor += note_len + gap_len

    x *= 0.92 + 0.08 * np.sin(2.0 * np.pi * 0.13 * t + 0.4)
    peak = float(np.max(np.abs(x))) if n else 0.0
    if peak > 0:
        x *= 0.52 / peak
    fade = min(int(0.010 * sample_rate), n // 2)
    if fade > 1:
        shape = np.linspace(0.0, 1.0, fade)
        x[:fade] *= shape
        x[-fade:] *= shape[::-1]
    if lead_in:
        x[:lead_in] *= np.linspace(0.0, 1.0, lead_in)
    return x


def _noise_component(
    n: int,
    sample_rate: int,
    rng: np.random.Generator,
    noise_damage: dict | None,
) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    base_sigma = db_to_linear(BASE_NOISE_DBFS)
    modulation = 1.0 + 0.10 * np.sin(2.0 * np.pi * 0.19 * t + 0.7)
    base = rng.normal(0.0, base_sigma, n) * modulation
    if not noise_damage:
        return base
    start = int(round(float(noise_damage["start_seconds"]) * sample_rate))
    end = int(round(float(noise_damage["end_seconds"]) * sample_rate))
    target_sigma = db_to_linear(float(noise_damage["level_dbfs"]))
    extra_sigma = float(np.sqrt(max(target_sigma * target_sigma - base_sigma * base_sigma, 0.0)))
    gate = _smooth_gate(n, start, end, sample_rate, ramp_ms=25.0)
    extra = rng.normal(0.0, extra_sigma, n) * gate
    return base + extra


def _apply_dc(
    x: np.ndarray, sample_rate: int, dc_damage: dict | None
) -> np.ndarray:
    if not dc_damage:
        return x
    n = x.size
    start = int(round(float(dc_damage["start_seconds"]) * sample_rate))
    end = int(round(float(dc_damage["end_seconds"]) * sample_rate))
    gate = _smooth_gate(n, start, end, sample_rate, ramp_ms=20.0)
    return x + float(dc_damage["amplitude"]) * gate


def _apply_hard_clip(
    x: np.ndarray, sample_rate: int, clip_damage: dict
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    n = x.size
    start = int(round(float(clip_damage["start_seconds"]) * sample_rate))
    end = int(round(float(clip_damage["end_seconds"]) * sample_rate))
    gate = _smooth_gate(n, start, end, sample_rate, ramp_ms=8.0)
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    overlay = 2.65 * np.sin(2.0 * np.pi * 220.0 * t + 0.2) + 1.35 * np.sin(
        2.0 * np.pi * 330.0 * t + 1.1
    )
    pre_clip = x + overlay * gate
    clipped = np.clip(pre_clip, -0.999, 0.999)
    mask = np.abs(pre_clip) >= 0.999
    regions = true_runs(mask, max_gap=int(round(0.020 * sample_rate)), min_length=max(2, int(round(0.005 * sample_rate))))
    return clipped, regions


def _apply_soft_limit(
    x: np.ndarray, sample_rate: int, clip_damage: dict
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    n = x.size
    start = int(round(float(clip_damage["start_seconds"]) * sample_rate))
    end = int(round(float(clip_damage["end_seconds"]) * sample_rate))
    if end <= start:
        return x, []
    gate = _smooth_gate(n, start, end, sample_rate, ramp_ms=10.0)
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    overlay = 1.05 * np.sin(2.0 * np.pi * 247.0 * t + 0.5) + 0.55 * np.sin(
        2.0 * np.pi * 370.0 * t + 1.7
    )
    driven = x + overlay * gate
    limited = np.tanh(2.7 * driven)
    peak = float(np.max(np.abs(limited))) if n else 0.0
    if peak > 0:
        limited *= 0.96 / peak
    blended = x * (1.0 - gate) + limited * gate
    return blended, [(start, end)]


def _add_impulses(
    x: np.ndarray,
    sample_rate: int,
    rng: np.random.Generator,
    impulse_damage: dict,
    forbidden: list[tuple[int, int]],
) -> list[dict]:
    n = x.size
    count = int(impulse_damage["count"])
    target_peak = float(impulse_damage["peak"])
    low = int(0.62 * sample_rate)
    high = n - int(0.42 * sample_rate)
    guard = int(0.018 * sample_rate)
    min_sep = max(guard, int(0.050 * sample_rate))
    coarse = np.linspace(low, high, max(count, 1))
    jitter = rng.uniform(-0.018, 0.018, count) * sample_rate
    desired = np.sort(np.clip(coarse + jitter, low, high)).astype(int)
    positions: list[int] = []
    for wanted in desired:
        best_position = int(np.clip(wanted, low, high))
        best_score = float("inf")
        for _ in range(80):
            candidate = int(np.clip(wanted + rng.uniform(-0.09, 0.09) * sample_rate, low, high))
            if any(abs(candidate - prior) < min_sep for prior in positions):
                continue
            if any(candidate + guard >= s and candidate - guard <= e for s, e in forbidden):
                continue
            local = float(np.max(np.abs(x[max(0, candidate - guard) : candidate + guard])))
            if local < best_score:
                best_score = local
                best_position = candidate
            if local < 0.16:
                break
        positions.append(best_position)
    positions = sorted(set(positions))
    events: list[dict] = []
    for position in positions:
        if any(position + guard >= s and position - guard <= e for s, e in forbidden):
            continue
        duration = int(round(rng.uniform(0.0030, 0.0055) * sample_rate))
        duration = max(8, min(duration, n - position))
        if duration <= 2:
            continue
        tt = np.arange(duration, dtype=np.float64) / float(sample_rate)
        f = rng.uniform(1200.0, 3100.0)
        phase = rng.uniform(0.0, 2.0 * np.pi)
        polarity = -1.0 if rng.random() < 0.5 else 1.0
        tau = rng.uniform(0.00035, 0.00075)
        shape = np.exp(-tt / tau) * np.sin(2.0 * np.pi * f * tt + phase)
        local_peak = float(np.max(np.abs(x[position : position + duration])))
        headroom = max(0.0, 0.965 - local_peak)
        amplitude = min(target_peak, max(0.04, 0.92 * headroom))
        component = polarity * amplitude * shape
        x[position : position + duration] += component
        actual_peak = float(np.max(np.abs(component)))
        events.append(
            {
                "start_sample": int(position),
                "end_sample": int(position + duration),
                "component_peak": actual_peak,
            }
        )
    return events


def _truth_noise_floor(
    noise_component: np.ndarray,
    segment_start: int,
    sample_rate: int,
    frame_length: int = TRUTH_FRAME_LENGTH,
    hop: int = TRUTH_HOP,
) -> dict:
    starts: list[int] = []
    values: list[float] = []
    n = noise_component.size
    cursor = 0
    while cursor < n:
        end = min(cursor + frame_length, n)
        block = noise_component[cursor:end]
        rms = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        starts.append(int(segment_start + cursor))
        values.append(float(20.0 * np.log10(max(rms, 1.0e-12))))
        cursor += hop
    return {
        "frame_length": int(frame_length),
        "hop": int(hop),
        "frame_starts": starts,
        "dbfs": values,
    }


def _interval_union_length(intervals: list[dict]) -> int:
    if not intervals:
        return 0
    ordered = sorted((int(item["start_sample"]), int(item["end_sample"])) for item in intervals)
    total = 0
    s0, e0 = ordered[0]
    for s, e in ordered[1:]:
        if s <= e0:
            e0 = max(e0, e)
        else:
            total += e0 - s0
            s0, e0 = s, e
    total += e0 - s0
    return int(total)


def _make_ground_truth(
    segment_start: int,
    segment_end: int,
    sample_rate: int,
    damages: dict,
    noise_truth: dict,
) -> dict:
    duration = max(1, segment_end - segment_start)
    clip_items = damages.get("clipping", [])
    clip_coverage = _interval_union_length(clip_items) / float(duration)
    clip_grade = grade_clipping(clip_coverage) if clip_items else None
    dc_items = damages.get("dc_offset", [])
    dc_grades = [grade_dc_offset(abs(float(item["value"]))) for item in dc_items]
    dc_grade = max(dc_grades, key=lambda x: {"light": 1, "medium": 2, "heavy": 3}[x]) if dc_grades else None
    noise_items = damages.get("noise", [])
    noise_grades = [grade_noise(float(item["level_dbfs"])) for item in noise_items]
    noise_grade = max(noise_grades, key=lambda x: {"light": 1, "medium": 2, "heavy": 3}[x]) if noise_grades else None
    impulse_items = damages.get("impulse", [])
    density = len(impulse_items) / (duration / float(sample_rate))
    peak = max((float(item["peak_amplitude"]) for item in impulse_items), default=0.0)
    impulse_grade = grade_impulse(density, peak)[0] if impulse_items else None
    segment_grades = {
        "clipping": clip_grade,
        "dc_offset": dc_grade,
        "noise": noise_grade,
        "impulse": impulse_grade,
    }
    overall, rationale = segment_overall_grade(segment_grades)
    workflow = map_workflow(segment_grades, overall)
    for item in clip_items:
        item["grade"] = clip_grade
    for item in dc_items:
        item["grade"] = grade_dc_offset(abs(float(item["value"])))
    for item in noise_items:
        item["grade"] = grade_noise(float(item["level_dbfs"]))
    for items in damages.values():
        for item in items:
            item["start_sample"] = int(item["start_sample"]) + int(segment_start)
            item["end_sample"] = int(item["end_sample"]) + int(segment_start)
    return {
        "damages": {
            "clipping": clip_items,
            "dc_offset": dc_items,
            "noise": noise_items,
            "impulse": impulse_items,
        },
        "segment_grades": segment_grades,
        "overall_grade": overall,
        "overall_grade_rationale": rationale,
        "workflow": workflow,
        "noise_floor": noise_truth,
    }


def _segment_damage_specs() -> list[dict]:
    hard = lambda start, end: {"subtype": "hard", "start_seconds": start, "end_seconds": end}
    soft = lambda start, end: {"subtype": "soft", "start_seconds": start, "end_seconds": end}
    recordings = [
        {
            "recording_id": "tape_A_hard_clip",
            "title": "磁带 A：硬削波轻/中/重",
            "segments": [
                {"clipping": hard(2.300, 2.355)},
                {"clipping": hard(2.200, 2.680)},
                {"clipping": hard(1.800, 3.600)},
            ],
        },
        {
            "recording_id": "vinyl_B_soft_limit",
            "title": "黑胶 B：轻度限幅轻/中/重",
            "segments": [
                {"clipping": soft(2.300, 2.360)},
                {"clipping": soft(2.200, 2.680)},
                {"clipping": soft(1.800, 3.600)},
            ],
        },
        {
            "recording_id": "tape_C_dc_offset",
            "title": "磁带 C：直流偏移轻/中/重",
            "segments": [
                {"dc_offset": {"start_seconds": 1.2, "end_seconds": 4.8, "amplitude": 0.025}},
                {"dc_offset": {"start_seconds": 1.2, "end_seconds": 4.8, "amplitude": 0.070}},
                {"dc_offset": {"start_seconds": 1.2, "end_seconds": 4.8, "amplitude": 0.150}},
            ],
        },
        {
            "recording_id": "tape_D_noise_floor",
            "title": "磁带 D：底噪抬升轻/中/重",
            "segments": [
                {"noise": {"start_seconds": 1.2, "end_seconds": 4.8, "level_dbfs": -61.0}},
                {"noise": {"start_seconds": 1.2, "end_seconds": 4.8, "level_dbfs": -51.0}},
                {"noise": {"start_seconds": 1.2, "end_seconds": 4.8, "level_dbfs": -40.0}},
            ],
        },
        {
            "recording_id": "vinyl_E_impulse",
            "title": "黑胶 E：脉冲爆音轻/中/重",
            "segments": [
                {"impulse": {"count": 2, "peak": 0.18}},
                {"impulse": {"count": 10, "peak": 0.40}},
                {"impulse": {"count": 30, "peak": 0.52}},
            ],
        },
        {
            "recording_id": "archive_F_mixed",
            "title": "档案 F：混合劣化与真值缺失段",
            "segments": [
                {},
                {
                    "clipping": hard(2.000, 2.450),
                    "dc_offset": {"start_seconds": 1.0, "end_seconds": 5.0, "amplitude": 0.070},
                    "noise": {"start_seconds": 0.8, "end_seconds": 5.2, "level_dbfs": -52.0},
                    "impulse": {"count": 8, "peak": 0.35},
                },
                {
                    "clipping": hard(1.800, 3.200),
                    "dc_offset": {"start_seconds": 1.0, "end_seconds": 5.0, "amplitude": 0.090},
                    "noise": {"start_seconds": 1.0, "end_seconds": 5.0, "level_dbfs": -50.0},
                    "impulse": {"count": 12, "peak": 0.42},
                    "truth_available": False,
                },
            ],
        },
        {
            "recording_id": "preview_G_light",
            "title": "预览 G：清洁、轻度底噪、轻度直流",
            "segments": [
                {},
                {"noise": {"start_seconds": 1.2, "end_seconds": 4.8, "level_dbfs": -61.0}},
                {"dc_offset": {"start_seconds": 1.5, "end_seconds": 4.5, "amplitude": 0.022}},
            ],
        },
    ]
    return recordings


def _render_segment(
    n: int,
    sample_rate: int,
    rng: np.random.Generator,
    damage: dict,
) -> tuple[np.ndarray, np.ndarray, dict]:
    music = _synth_music(n, sample_rate, rng)
    noise = _noise_component(n, sample_rate, rng, damage.get("noise"))
    x = music + noise
    x = _apply_dc(x, sample_rate, damage.get("dc_offset"))

    clipping_truth: list[dict] = []
    clip_spec = damage.get("clipping")
    if clip_spec:
        if clip_spec.get("subtype") == "soft":
            x, regions = _apply_soft_limit(x, sample_rate, clip_spec)
            for start, end in regions:
                clipping_truth.append(
                    {
                        "start_sample": int(start),
                        "end_sample": int(end),
                        "subtype": "soft",
                    }
                )
        else:
            x, regions = _apply_hard_clip(x, sample_rate, clip_spec)
            for start, end in regions:
                clipping_truth.append(
                    {
                        "start_sample": int(start),
                        "end_sample": int(end),
                        "subtype": "hard",
                    }
                )

    forbidden: list[tuple[int, int]] = []
    if clip_spec:
        forbidden.append(
            (
                int(round(float(clip_spec["start_seconds"]) * sample_rate)),
                int(round(float(clip_spec["end_seconds"]) * sample_rate)),
            )
        )
    impulse_truth: list[dict] = []
    impulse_spec = damage.get("impulse")
    if impulse_spec:
        events = _add_impulses(x, sample_rate, rng, impulse_spec, forbidden)
        for event in events:
            start = int(event["start_sample"])
            end = int(event["end_sample"])
            peak = float(np.max(np.abs(x[start:end]))) if end > start else 0.0
            impulse_truth.append(
                {
                    "start_sample": start,
                    "end_sample": end,
                    "peak_amplitude": peak,
                }
            )

    x = np.clip(x, -0.999, 0.999)
    damages = {
        "clipping": clipping_truth,
        "dc_offset": [],
        "noise": [],
        "impulse": impulse_truth,
    }
    if damage.get("dc_offset"):
        spec = damage["dc_offset"]
        damages["dc_offset"].append(
            {
                "start_sample": int(round(float(spec["start_seconds"]) * sample_rate)),
                "end_sample": int(round(float(spec["end_seconds"]) * sample_rate)),
                "value": float(spec["amplitude"]),
            }
        )
    if damage.get("noise"):
        spec = damage["noise"]
        damages["noise"].append(
            {
                "start_sample": int(round(float(spec["start_seconds"]) * sample_rate)),
                "end_sample": int(round(float(spec["end_seconds"]) * sample_rate)),
                "level_dbfs": float(spec["level_dbfs"]),
            }
        )
    return x, noise, damages


def generate_dataset(out_dir: str | Path, seed: int = 20261128) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    segment_samples = int(round(SEGMENT_SECONDS * SAMPLE_RATE))
    recording_specs = _segment_damage_specs()
    manifest_recordings: list[dict] = []
    for rec_index, rec_spec in enumerate(recording_specs):
        recording_id = rec_spec["recording_id"]
        pieces: list[np.ndarray] = []
        segment_entries: list[dict] = []
        for seg_index, damage in enumerate(rec_spec["segments"]):
            rng = _rng(seed, rec_index, seg_index)
            audio, noise, damages = _render_segment(
                segment_samples, SAMPLE_RATE, rng, damage
            )
            segment_start = seg_index * segment_samples
            segment_end = segment_start + segment_samples
            truth_available = bool(damage.get("truth_available", True))
            noise_truth = _truth_noise_floor(noise, segment_start, SAMPLE_RATE)
            segment_entry = {
                "segment_id": f"{recording_id}/seg_{seg_index:02d}",
                "start_sample": segment_start,
                "end_sample": segment_end,
                "truth_available": truth_available,
            }
            if truth_available:
                segment_entry["ground_truth"] = _make_ground_truth(
                    segment_start, segment_end, SAMPLE_RATE, damages, noise_truth
                )
            else:
                segment_entry["ground_truth"] = None
            pieces.append(audio)
            segment_entries.append(segment_entry)
        recording_audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float64)
        wav_name = f"{recording_id}.wav"
        write_mono_wav(out_dir / wav_name, recording_audio, SAMPLE_RATE, sample_width=2)
        manifest_recordings.append(
            {
                "recording_id": recording_id,
                "title": rec_spec["title"],
                "path": wav_name,
                "sample_rate": SAMPLE_RATE,
                "duration_samples": int(recording_audio.size),
                "duration_seconds": recording_audio.size / float(SAMPLE_RATE),
                "segments": segment_entries,
            }
        )

    manifest = {
        "schema_version": 1,
        "generator": "generate_dataset.py",
        "seed": int(seed),
        "sample_rate": SAMPLE_RATE,
        "bit_depth": 16,
        "channels": 1,
        "segment_seconds": SEGMENT_SECONDS,
        "truth_frame_length": TRUTH_FRAME_LENGTH,
        "truth_hop": TRUTH_HOP,
        "default_frame_length": 512,
        "default_hop": 128,
        "alternate_frame_length": 1024,
        "alternate_hop": 256,
        "grade_thresholds": THRESHOLD_TABLE,
        "recordings": manifest_recordings,
        "total_duration_seconds": sum(
            item["duration_samples"] for item in manifest_recordings
        )
        / float(SAMPLE_RATE),
    }
    with (out_dir / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return manifest
