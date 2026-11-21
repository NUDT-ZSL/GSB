from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .util import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    read_json,
    read_jsonl,
    sha256_json,
    write_jsonl,
)


FRAME_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = FRAME_RATE * FRAME_MS // 1000


@dataclass(frozen=True)
class CallTimeline:
    call_id: str
    duration_ms: int
    seed: int
    segments: list[dict[str, int | str]]


def _allocate(
    total: int,
    count: int,
    minimum: int,
    rng: np.random.Generator,
) -> list[int]:
    base = minimum * count
    if base > total:
        raise ValueError("duration is too short for requested timeline")
    weights = rng.uniform(0.8, 1.2, size=count)
    weights = weights / weights.sum()
    extra = total - base
    values = [minimum + int(math.floor(extra * weight)) for weight in weights]
    values[-1] += total - sum(values)
    return values


def build_timeline(
    call_id: str,
    frame_count: int,
    seed: int,
    silence_count: int,
    crosstalk_index: int,
    device_index: int,
) -> CallTimeline:
    rng = np.random.default_rng(seed ^ 0x5EED)
    silence_total = max(silence_count * 12, int(round(frame_count * 0.38)))
    speech_total = frame_count - silence_total
    silence_lengths = _allocate(silence_total, silence_count, 12, rng)
    speech_lengths = _allocate(speech_total, silence_count - 1, 20, rng)

    segments: list[dict[str, int | str]] = []
    cursor = 0
    for index in range(silence_count):
        silence_frames = silence_lengths[index]
        if index == crosstalk_index:
            scenario = "crosstalk_residue"
        elif index == device_index:
            scenario = "device_step"
        else:
            scenario = "stationary_floor"
        segments.append(
            {
                "start_frame": cursor,
                "end_frame": cursor + silence_frames,
                "kind": "silence",
                "source_scenario": scenario,
            }
        )
        cursor += silence_frames
        if index < silence_count - 1:
            speech_frames = speech_lengths[index]
            segments.append(
                {
                    "start_frame": cursor,
                    "end_frame": cursor + speech_frames,
                    "kind": "speech",
                }
            )
            cursor += speech_frames
    if cursor != frame_count:
        raise AssertionError(f"timeline mismatch: {cursor} != {frame_count}")
    return CallTimeline(
        call_id=call_id,
        duration_ms=frame_count * FRAME_MS,
        seed=seed,
        segments=segments,
    )


def _frame_energy_zcr(samples: np.ndarray) -> tuple[float, float]:
    energy = float(np.dot(samples, samples) / samples.size)
    crossings = int(np.count_nonzero((samples[:-1] >= 0.0) != (samples[1:] >= 0.0)))
    return round(energy, 12), round(crossings / samples.size, 12)


def _generate_call(
    timeline: CallTimeline,
    data_dir: Path,
) -> dict[str, Any]:
    frame_count = timeline.duration_ms // FRAME_MS
    sample_count = frame_count * FRAME_SAMPLES
    rng = np.random.default_rng(timeline.seed)
    noise_sigma = np.zeros(frame_count, dtype=np.float64)
    is_speech = np.zeros(frame_count, dtype=bool)
    scenarios: list[str | None] = [None] * frame_count
    speech_frames: list[int] = []
    silence_frames: list[int] = []

    device_range: tuple[int, int] | None = None
    for segment in timeline.segments:
        start = int(segment["start_frame"])
        end = int(segment["end_frame"])
        if segment["kind"] == "speech":
            is_speech[start:end] = True
            speech_frames.extend(range(start, end))
        else:
            silence_frames.extend(range(start, end))
            scenario = str(segment["source_scenario"])
            scenarios[start:end] = [scenario] * (end - start)
            if scenario == "device_step":
                device_range = (start, end)

    phase = float(rng.uniform(0.0, 2.0 * math.pi))
    frame_index = np.arange(frame_count, dtype=np.float64)
    drift = (
        1.0
        + 0.16 * np.sin(2.0 * math.pi * frame_index / 137.0 + phase)
        + 0.045 * np.sin(2.0 * math.pi * frame_index / 31.0 + 0.5 * phase)
    )
    base_sigma = 0.0032 * drift
    if device_range is not None:
        base_sigma[device_range[0] : device_range[1]] *= 2.8
    noise_sigma[:] = base_sigma

    samples = np.empty(sample_count, dtype=np.float64)
    for frame in range(frame_count):
        start = frame * FRAME_SAMPLES
        end = start + FRAME_SAMPLES
        noise = rng.normal(0.0, noise_sigma[frame], FRAME_SAMPLES)
        value = noise
        if is_speech[frame]:
            t = np.arange(FRAME_SAMPLES, dtype=np.float64)
            pitch = 145.0 + 55.0 * math.sin(2.0 * math.pi * frame / 37.0)
            envelope = 0.55 + 0.45 * math.sin(2.0 * math.pi * frame / 19.0) ** 2
            voice = (
                0.040 * envelope * np.sin(2.0 * math.pi * pitch * t / FRAME_RATE)
                + 0.018 * envelope * np.sin(2.0 * math.pi * 2.0 * pitch * t / FRAME_RATE)
                + 0.010 * envelope * np.sin(2.0 * math.pi * 3.0 * pitch * t / FRAME_RATE)
            )
            value = noise + voice
        elif scenarios[frame] == "crosstalk_residue":
            t = np.arange(FRAME_SAMPLES, dtype=np.float64)
            modulation = 0.22 + 0.78 * abs(
                math.sin(2.0 * math.pi * frame / 5.5 + phase)
            )
            residue = modulation * (
                0.016 * np.sin(2.0 * math.pi * 330.0 * t / FRAME_RATE)
                + 0.009 * np.sin(2.0 * math.pi * 710.0 * t / FRAME_RATE)
            )
            value = noise + residue
        samples[start:end] = value

    quantized = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype("<i2")
    pcm_path = data_dir / "calls" / f"{timeline.call_id}.pcm"
    atomic_write_bytes(pcm_path, quantized.tobytes())

    normalized = quantized.astype(np.float64) / 32768.0
    observation_records: list[dict[str, Any]] = []
    noise_profile_frames: list[dict[str, Any]] = []
    for frame in range(frame_count):
        start = frame * FRAME_SAMPLES
        end = start + FRAME_SAMPLES
        energy, zcr = _frame_energy_zcr(normalized[start:end])
        observation_records.append(
            {
                "frame_index": frame,
                "sample_start": start,
                "sample_end": end,
                "noise_floor_true": round(float(noise_sigma[frame] ** 2), 12),
                "is_speech": bool(is_speech[frame]),
                "energy": energy,
                "zero_crossing_rate": zcr,
            }
        )
        noise_profile_frames.append(
            {
                "frame_index": frame,
                "sample_start": start,
                "sample_end": end,
                "noise_floor_true": round(float(noise_sigma[frame] ** 2), 12),
            }
        )

    observation_path = data_dir / "observations" / f"{timeline.call_id}.jsonl"
    write_jsonl(observation_path, observation_records)
    profile_ref = f"profiles/{timeline.call_id}.json"
    atomic_write_json(
        data_dir / profile_ref,
        {
            "call_id": timeline.call_id,
            "frame_ms": FRAME_MS,
            "sample_rate": FRAME_RATE,
            "frames": noise_profile_frames,
        },
    )

    truth_segments = [
        {
            "start_frame": int(segment["start_frame"]),
            "end_frame": int(segment["end_frame"]),
            "kind": str(segment["kind"]),
            **(
                {"source_scenario": str(segment["source_scenario"])}
                if segment["kind"] == "silence"
                else {}
            ),
        }
        for segment in timeline.segments
    ]
    truth_body = {
        "schema_version": 1,
        "call_id": timeline.call_id,
        "frame_ms": FRAME_MS,
        "sample_rate": FRAME_RATE,
        "frame_count": frame_count,
        "segments": truth_segments,
    }
    truth_document = dict(truth_body)
    truth_document["revision"] = sha256_json(truth_body)
    atomic_write_json(data_dir / "truth" / f"{timeline.call_id}.json", truth_document)

    return {
        "call_id": timeline.call_id,
        "duration_ms": timeline.duration_ms,
        "seed": timeline.seed,
        "sample_rate": FRAME_RATE,
        "frame_ms": FRAME_MS,
        "noise_profile_ref": profile_ref,
    }


def generate_dataset(
    data_dir: Path,
    seed: int = 20261122,
    calls: int = 3,
) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    for subdir in ("calls", "observations", "truth", "profiles"):
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)

    specifications = [
        ("call_0001", 240, 5, 1, 3),
        ("call_0002", 300, 7, 1, 3),
        ("call_0003", 350, 8, 2, 5),
    ]
    if calls < 1:
        raise ValueError("calls must be positive")
    manifest_records: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    for index in range(calls):
        if index < len(specifications):
            call_id, frame_count, silence_count, crosstalk_index, device_index = (
                specifications[index]
            )
        else:
            call_id = f"call_{index + 1:04d}"
            frame_count = 260 + 20 * (index % 7)
            silence_count = 5 + (index % 4)
            crosstalk_index = 1
            device_index = max(2, silence_count // 2)
        call_seed = int(seed) + index * 1009
        timeline = build_timeline(
            call_id,
            frame_count,
            call_seed,
            silence_count,
            crosstalk_index,
            device_index,
        )
        manifest_records.append(_generate_call(timeline, data_dir))
        timelines.append(
            {
                "call_id": call_id,
                "frame_count": frame_count,
                "segments": timeline.segments,
            }
        )

    write_jsonl(data_dir / "manifest.jsonl", manifest_records)
    atomic_write_json(
        data_dir / "sample_metadata.json",
        {
            "schema_version": 1,
            "seed": seed,
            "calls": calls,
            "sample_rate": FRAME_RATE,
            "frame_ms": FRAME_MS,
            "timelines": timelines,
        },
    )
    return {
        "code": "OK",
        "data_dir": str(data_dir),
        "calls": len(manifest_records),
        "sample_rate": FRAME_RATE,
        "frame_ms": FRAME_MS,
        "seed": seed,
        "manifest": str(data_dir / "manifest.jsonl"),
    }


def load_truth(data_dir: Path, call_id: str) -> dict[str, Any]:
    return read_json(data_dir / "truth" / f"{call_id}.json")


def truth_frame_kinds(truth: dict[str, Any], frame_count: int) -> list[str]:
    kinds = ["unknown"] * frame_count
    for segment in truth.get("segments", []):
        start = int(segment["start_frame"])
        end = int(segment["end_frame"])
        if start < 0 or end > frame_count or start >= end:
            from .errors import fail

            fail(
                "E_SEGMENT_SPLIT",
                call_id=truth.get("call_id"),
                truth_frames=frame_count,
                start_frame=start,
                end_frame=end,
            )
        for frame in range(start, end):
            if kinds[frame] != "unknown":
                from .errors import fail

                fail(
                    "E_SEGMENT_SPLIT",
                    call_id=truth.get("call_id"),
                    overlapping_frame=frame,
                )
            kinds[frame] = str(segment["kind"])
    return kinds


def edit_truth(
    data_dir: Path,
    call_id: str,
    start_frame: int,
    end_frame: int,
    kind: str,
) -> dict[str, Any]:
    truth_path = data_dir / "truth" / f"{call_id}.json"
    truth = read_json(truth_path)
    frame_count = int(truth["frame_count"])
    if kind not in {"speech", "silence"}:
        raise ValueError("kind must be speech or silence")
    if start_frame < 0 or end_frame > frame_count or start_frame >= end_frame:
        raise ValueError("edited frame range is invalid")

    kinds = truth_frame_kinds(truth, frame_count)
    for frame in range(start_frame, end_frame):
        kinds[frame] = kind
    segments: list[dict[str, Any]] = []
    cursor = 0
    while cursor < frame_count:
        next_cursor = cursor + 1
        while next_cursor < frame_count and kinds[next_cursor] == kinds[cursor]:
            next_cursor += 1
        segments.append(
            {
                "start_frame": cursor,
                "end_frame": next_cursor,
                "kind": kinds[cursor],
            }
        )
        cursor = next_cursor
    body = {
        "schema_version": 1,
        "call_id": call_id,
        "frame_ms": FRAME_MS,
        "sample_rate": FRAME_RATE,
        "frame_count": frame_count,
        "segments": segments,
    }
    truth = dict(body)
    truth["revision"] = sha256_json(body)
    atomic_write_json(truth_path, truth)
    return {
        "code": "OK",
        "call_id": call_id,
        "revision": truth["revision"],
        "truth_file": str(truth_path),
        "edited_frames": [start_frame, end_frame],
        "kind": kind,
    }


def replace_pcm_gain(data_dir: Path, call_id: str, gain: float) -> dict[str, Any]:
    path = data_dir / "calls" / f"{call_id}.pcm"
    raw = path.read_bytes()
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    changed = np.clip(np.rint(samples * float(gain)), -32768, 32767).astype("<i2")
    atomic_write_bytes(path, changed.tobytes())
    normalized = changed.astype(np.float64) / 32768.0
    observation_path = data_dir / "observations" / f"{call_id}.jsonl"
    observations = read_jsonl(observation_path)
    for frame_index, observation in enumerate(observations):
        start = int(observation["sample_start"])
        end = int(observation["sample_end"])
        energy, zcr = _frame_energy_zcr(normalized[start:end])
        observation["energy"] = energy
        observation["zero_crossing_rate"] = zcr
        observation["noise_floor_true"] = round(
            float(observation["noise_floor_true"]) * float(gain) ** 2, 12
        )
    write_jsonl(observation_path, observations)
    truth = read_json(data_dir / "truth" / f"{call_id}.json")
    profile_ref = f"profiles/{call_id}.json"
    profile = read_json(data_dir / profile_ref)
    for frame in profile.get("frames", []):
        frame["noise_floor_true"] = round(
            float(frame["noise_floor_true"]) * float(gain) ** 2, 12
        )
    atomic_write_json(data_dir / profile_ref, profile)
    return {
        "code": "OK",
        "call_id": call_id,
        "pcm_file": str(path),
        "observation_file": str(observation_path),
        "gain": gain,
        "bytes": len(changed.tobytes()),
        "truth_revision_unchanged": truth.get("revision"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic call samples")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--seed", type=int, default=20261122)
    parser.add_argument("--calls", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = generate_dataset(Path(args.data_dir), seed=args.seed, calls=args.calls)
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

