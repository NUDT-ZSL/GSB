from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo"


def copy_demo(destination: Path, *, with_commands: bool = True) -> Path:
    destination = destination.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(DEMO, destination)
    if not with_commands:
        (destination / "edit_commands.jsonl").unlink(missing_ok=True)
    return destination


def update_config(directory: Path, updates: dict) -> None:
    path = directory / "acoustic_config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def make_tiny_input(
    directory: Path,
    *,
    utterance_id: str = "utt_tiny",
    phones: list[str] | None = None,
    durations: list[int] | None = None,
    f0_hz: list[float] | None = None,
    energy: list[float] | None = None,
    noise_floor: list[float] | None = None,
    voicing_score: list[float] | None = None,
    config_updates: dict | None = None,
    commands: list[dict] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    phones = phones or ["AH"]
    durations = durations or [8]
    f0_hz = f0_hz or [130.0]
    energy = energy or [0.7]
    noise_floor = noise_floor or [0.01]
    voicing_score = voicing_score or [0.9]
    config = {
        "sample_rate": 16000,
        "frame_length_ms": 20.0,
        "frame_shift_ms": 10.0,
        "f0_min_hz": 70.0,
        "f0_max_hz": 400.0,
        "f0_quantization_hz": 5.0,
        "spectral_bands": 12,
        "voicing_threshold": 0.5,
        "energy_target_dbfs": -18.0,
        "max_batch_frames": 5,
        "concurrency": 2,
        "snapshot_interval_frames": 4,
    }
    if config_updates:
        config.update(config_updates)
    (directory / "acoustic_config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    total_frames = sum(durations)
    output_length = (total_frames - 1) * 160 + 320
    n = np.arange(output_length, dtype=np.float64)
    audio = np.rint(500.0 * np.sin(2.0 * np.pi * 130.0 * n / 16000.0)).astype(np.int16)
    wav_name = f"{utterance_id}.wav"
    sf.write(
        str(directory / wav_name),
        audio,
        16000,
        subtype="PCM_16",
        format="WAV",
    )
    label = {
        "utterance_id": utterance_id,
        "wav": wav_name,
        "phonemes": phones,
        "duration_frames": durations,
        "f0_hz": f0_hz,
        "energy": energy,
        "noise_floor": noise_floor,
        "voicing_score": voicing_score,
    }
    write_jsonl(directory / "labels.jsonl", [label])
    if commands is not None:
        write_jsonl(directory / "edit_commands.jsonl", commands)
    (directory / "generator_seed.json").write_text(
        json.dumps({"seed": 12345}, sort_keys=True) + "\n", encoding="utf-8"
    )
    return directory


@pytest.fixture
def demo_copy(tmp_path: Path) -> Path:
    return copy_demo(tmp_path / "sample")

