from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from meeting_vad.config import config_from_dict, load_config
from meeting_vad.errors import BusinessError
from meeting_vad.samples import load_annotations, load_clip_manifest
from meeting_vad.synth import generate_samples


ROOT = Path(__file__).resolve().parents[1]


def _raw_config() -> dict:
    return json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_config_validates_sample_rate(tmp_path: Path) -> None:
    raw = _raw_config()
    raw["sample_rate"] = 8000
    with pytest.raises(BusinessError) as exc:
        config_from_dict(raw, tmp_path)
    assert exc.value.code == "E_SAMPLE_RATE"


def test_config_validates_frame_ms(tmp_path: Path) -> None:
    raw = _raw_config()
    raw["frame_ms"] = 0
    with pytest.raises(BusinessError) as exc:
        config_from_dict(raw, tmp_path)
    assert exc.value.code == "E_FRAME_MS"


def test_config_validates_frame_shift(tmp_path: Path) -> None:
    raw = _raw_config()
    raw["frame_hop_ms"] = 25
    with pytest.raises(BusinessError) as exc:
        config_from_dict(raw, tmp_path)
    assert exc.value.code == "E_FRAME_SHIFT"


def test_duplicate_clip_id_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "samples.jsonl"
    row = {
        "clip_id": "dup",
        "sample_rate": 16000,
        "duration_ms": 100,
        "seed": 1,
        "frame_ms": 20,
        "channels": 1,
        "sample_width": 2,
        "audio_pcm": "missing.pcm",
    }
    manifest.write_text(
        json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8"
    )
    with pytest.raises(BusinessError) as exc:
        load_clip_manifest(manifest, 16000)
    assert exc.value.code == "E_DUPLICATE_CLIP"


def test_missing_segment_id_rejected(sample_dir: Path) -> None:
    clips = load_clip_manifest(sample_dir / "samples.jsonl", 16000)
    broken = sample_dir / "broken_annotations.jsonl"
    row = {
        "clip_id": clips[0].clip_id,
        "start_ms": 10,
        "end_ms": 20,
        "speaker_label": "S01",
        "label_revision": 1,
        "confidence": 0.8,
    }
    broken.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(BusinessError) as exc:
        load_annotations(broken, clips)
    assert exc.value.code == "E_SEGMENT_ID_MISSING"


def test_sample_generation_is_byte_deterministic(tmp_path: Path) -> None:
    output_a = tmp_path / "samples-a"
    output_b = tmp_path / "samples-b"
    first = generate_samples(ROOT / "config" / "synthesis.json", output_a, clean=True)
    first_bytes = {
        path.relative_to(output_a): path.read_bytes()
        for path in output_a.rglob("*")
        if path.is_file()
    }
    second = generate_samples(ROOT / "config" / "synthesis.json", output_b, clean=True)
    second_bytes = {
        path.relative_to(output_b): path.read_bytes()
        for path in output_b.rglob("*")
        if path.is_file()
    }
    assert first_bytes == second_bytes
    assert first["samples_sha256"] == second["samples_sha256"]
    assert first["annotations_sha256"] == second["annotations_sha256"]


def test_load_config_accepts_valid_file(tmp_path: Path) -> None:
    raw = _raw_config()
    config_file = _write_json(tmp_path / "config.json", raw)
    config = load_config(config_file, base_dir=tmp_path)
    assert config.sample_rate == 16000
    assert config.frame_hop_ms <= config.frame_ms
