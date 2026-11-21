from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import section_revision
from .errors import BusinessError, fail
from .util import (
    canonical_json,
    read_jsonl,
    seal_record,
    sha256_file,
    sha256_json,
)


@dataclass(frozen=True)
class CallMeta:
    call_id: str
    duration_ms: int
    seed: int
    sample_rate: int
    frame_ms: int
    noise_profile_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "duration_ms": self.duration_ms,
            "seed": self.seed,
            "sample_rate": self.sample_rate,
            "frame_ms": self.frame_ms,
            "noise_profile_ref": self.noise_profile_ref,
        }


def load_manifest(data_dir: Path) -> list[CallMeta]:
    path = data_dir / "manifest.jsonl"
    if not path.exists():
        fail("E_INPUT_INVALID", detail="manifest is missing", path=str(path))
    try:
        rows = read_jsonl(path)
    except (OSError, ValueError) as exc:
        fail("E_INPUT_INVALID", detail=f"cannot read manifest: {exc}", path=str(path))
    calls: list[CallMeta] = []
    seen: set[str] = set()
    for row in rows:
        try:
            call = CallMeta(
                call_id=str(row["call_id"]),
                duration_ms=int(row["duration_ms"]),
                seed=int(row["seed"]),
                sample_rate=int(row["sample_rate"]),
                frame_ms=int(row["frame_ms"]),
                noise_profile_ref=str(row["noise_profile_ref"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            fail("E_INPUT_INVALID", detail=f"invalid manifest row: {exc}")
        if call.call_id in seen:
            fail("E_INPUT_INVALID", detail="duplicate call_id", call_id=call.call_id)
        if call.sample_rate != 16000:
            fail(
                "E_CONFIG_INVALID",
                call_id=call.call_id,
                detail="manifest sample_rate must be 16000",
                sample_rate=call.sample_rate,
            )
        if call.frame_ms != 20:
            fail(
                "E_CONFIG_INVALID",
                call_id=call.call_id,
                detail="manifest frame_ms must be 20",
                frame_ms=call.frame_ms,
            )
        seen.add(call.call_id)
        calls.append(call)
    if not calls:
        fail("E_INPUT_INVALID", detail="manifest contains no calls")
    return sorted(calls, key=lambda call: call.call_id)


def pcm_path(data_dir: Path, call_id: str) -> Path:
    return data_dir / "calls" / f"{call_id}.pcm"


def observation_path(data_dir: Path, call_id: str) -> Path:
    return data_dir / "observations" / f"{call_id}.jsonl"


def feature_input_fingerprint(data_dir: Path, call: CallMeta) -> dict[str, Any]:
    pcm = pcm_path(data_dir, call.call_id)
    observations = observation_path(data_dir, call.call_id)
    if not pcm.exists():
        fail("E_INPUT_INVALID", call_id=call.call_id, detail="PCM file is missing")
    if not observations.exists():
        fail(
            "E_INPUT_INVALID",
            call_id=call.call_id,
            detail="observation file is missing",
        )
    return {
        "manifest": sha256_json(call.to_dict()),
        "pcm_sha256": sha256_file(pcm),
        "observation_sha256": sha256_file(observations),
        "pcm_bytes": pcm.stat().st_size,
    }


def frame_energy_zcr(samples: np.ndarray) -> tuple[float, float]:
    energy = float(np.dot(samples, samples) / samples.size)
    crossings = int(np.count_nonzero((samples[:-1] >= 0.0) != (samples[1:] >= 0.0)))
    return round(energy, 12), round(crossings / samples.size, 12)


def compute_feature_record(
    call: CallMeta,
    data_dir: Path,
    config: dict[str, Any],
    version: int,
    input_fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frame_samples = int(call.sample_rate * call.frame_ms / 1000)
    raw = pcm_path(data_dir, call.call_id).read_bytes()
    if len(raw) % 2:
        fail(
            "E_FRAME_MISMATCH",
            call_id=call.call_id,
            detail="PCM byte count is not a multiple of int16 sample size",
        )
    pcm = np.frombuffer(raw, dtype="<i2")
    normalized = pcm.astype(np.float64) / 32768.0
    computed_frame_count = int(normalized.size // frame_samples)

    observations = read_jsonl(observation_path(data_dir, call.call_id))
    observed_frame_count = len(observations)
    if computed_frame_count != observed_frame_count:
        fail(
            "E_FRAME_MISMATCH",
            call_id=call.call_id,
            computed_frame_count=computed_frame_count,
            observed_frame_count=observed_frame_count,
            mismatch_frame=min(computed_frame_count, observed_frame_count),
        )

    frames: list[dict[str, Any]] = []
    for frame_index in range(computed_frame_count):
        sample_start = frame_index * frame_samples
        sample_end = sample_start + frame_samples
        energy, zcr = frame_energy_zcr(normalized[sample_start:sample_end])
        observation = observations[frame_index]
        if (
            int(observation.get("frame_index", -1)) != frame_index
            or int(observation.get("sample_start", -1)) != sample_start
            or int(observation.get("sample_end", -1)) != sample_end
            or "energy" not in observation
            or "zero_crossing_rate" not in observation
            or abs(float(observation["energy"]) - energy) > 1e-12
            or abs(float(observation["zero_crossing_rate"]) - zcr) > 1e-12
        ):
            fail(
                "E_FRAME_MISMATCH",
                call_id=call.call_id,
                mismatch_frame=frame_index,
                computed_frame_count=computed_frame_count,
                observed_frame_count=observed_frame_count,
                computed_energy=energy,
                observed_energy=observation.get("energy"),
                computed_zero_crossing_rate=zcr,
                observed_zero_crossing_rate=observation.get("zero_crossing_rate"),
            )
        frames.append(
            {
                "frame_index": frame_index,
                "sample_start": sample_start,
                "sample_end": sample_end,
                "energy": energy,
                "zero_crossing_rate": zcr,
                "is_speech": bool(observation.get("is_speech", False)),
                "noise_floor_true": round(
                    float(observation.get("noise_floor_true", 0.0)), 12
                ),
            }
        )

    feature_config_revision = section_revision(config, "features")
    record: dict[str, Any] = {
        "call_id": call.call_id,
        "feature_version": int(version),
        "feature_config_revision": feature_config_revision,
        "sample_rate": call.sample_rate,
        "frame_ms": call.frame_ms,
        "frame_samples": frame_samples,
        "duration_ms": call.duration_ms,
        "frame_count": computed_frame_count,
        "observed_frame_count": observed_frame_count,
        "frames_checksum": sha256_json(frames),
        "frames": frames,
        "input_fingerprint": input_fingerprint
        or feature_input_fingerprint(data_dir, call),
    }
    record["dependencies"] = {
        "feature_config_revision": feature_config_revision,
        "input_fingerprint": record["input_fingerprint"],
    }
    return seal_record(record)


def frame_record(
    feature_record: dict[str, Any], frame_index: int
) -> dict[str, Any]:
    frames = feature_record.get("frames", [])
    if frame_index < 0 or frame_index >= len(frames):
        fail(
            "E_INCONSISTENT",
            stage="features",
            call_id=feature_record.get("call_id"),
            frame_index=frame_index,
        )
    return frames[frame_index]


def validate_feature_record(record: dict[str, Any]) -> None:
    call_id = record.get("call_id")
    frames = record.get("frames")
    if not isinstance(frames, list) or len(frames) != record.get("frame_count"):
        fail("E_INCONSISTENT", stage="features", call_id=call_id)
    for index, frame in enumerate(frames):
        if frame.get("frame_index") != index:
            fail(
                "E_FRAME_MISMATCH",
                call_id=call_id,
                mismatch_frame=index,
                detail="feature frame index sequence is not contiguous",
            )
        if not math.isfinite(float(frame.get("energy", math.nan))):
            fail("E_FRAME_MISMATCH", call_id=call_id, mismatch_frame=index)
    if record.get("frames_checksum") != sha256_json(frames):
        fail("E_INCONSISTENT", stage="features", call_id=call_id, detail="frames checksum")


def describe_feature_record(record: dict[str, Any]) -> str:
    return canonical_json(
        {
            "call_id": record["call_id"],
            "frame_count": record["frame_count"],
            "frames_checksum": record["frames_checksum"],
        }
    )

