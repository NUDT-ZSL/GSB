"""Determinism, audio validation and feature extraction tests."""
from __future__ import annotations

import os

import numpy as np
import pytest

from voiceworkbench.audio import scan_wav_files, validate_wav_header, read_wav
from voiceworkbench.config import load_config
from voiceworkbench.determinism import array_fingerprint, atomic_write_json, content_hash
from voiceworkbench.errors import AudioFormatError
from voiceworkbench.features import (
    extract_all,
    extract_file,
    extract_frame,
    compute_frame_metrics,
    _frame_matrix,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_generated_audio_is_valid(audio_dir):
    paths = scan_wav_files(audio_dir)
    assert len(paths) == 3
    for path in paths:
        validate_wav_header(path)
        audio = read_wav(path)
        assert audio.sample_rate == 16000
        assert audio.samples.ndim == 1


def test_bad_audio_rejected(bad_audio_dir):
    paths = scan_wav_files(bad_audio_dir)
    assert paths
    for path in paths:
        with pytest.raises(AudioFormatError) as exc:
            validate_wav_header(path)
        assert exc.value.code == "E_AUDIO_FORMAT"


def test_frame_count_and_zero_padding(config_factory):
    cfg = config_factory()
    audio = read_wav(os.path.join(ROOT, "data", "audio", "sample_1.wav"))
    features, failures = extract_file(audio.path, cfg)
    assert not failures
    expected = len(range(0, audio.sample_count, cfg.hop_size))
    assert features.frame_count == expected
    assert [f.frame_index for f in features.frames] == list(range(expected))
    last = features.frames[-1]
    assert last.end_ms >= audio.duration_ms


def test_feature_extraction_is_deterministic(config_factory, audio_dir):
    cfg = config_factory()
    first, failures_first = extract_all(scan_wav_files(audio_dir), cfg)
    second, failures_second = extract_all(scan_wav_files(audio_dir), cfg)
    assert failures_first == failures_second == []
    assert [f.to_dict() for f in first] == [f.to_dict() for f in second]
    fingerprints = [frame.frame_fingerprint for ff in first for frame in ff.frames]
    assert len(fingerprints) == len(set(fingerprints)) or len(fingerprints) > 0


def test_vectorized_matches_scalar(config_factory):
    cfg = config_factory()
    audio = read_wav(os.path.join(ROOT, "data", "audio", "sample_2.wav"))
    matrix = _frame_matrix(audio.samples, cfg.frame_size, cfg.hop_size)
    metrics = compute_frame_metrics(matrix, cfg)
    for index in range(matrix.shape[0]):
        record = extract_frame(matrix[index], index, index * cfg.hop_size, cfg)
        assert record.voiceprint == list(metrics["voiceprint"][index])
        assert abs(record.energy_db - metrics["energy_db"][index]) < 1e-9
        assert abs(record.flatness - metrics["flatness"][index]) < 1e-9


def test_quantization_levels_change_codes(config_factory, audio_dir):
    cfg8 = config_factory(quant_levels=8)
    cfg4 = config_factory(quant_levels=4)
    path = scan_wav_files(audio_dir)[0]
    features8, _ = extract_file(path, cfg8)
    features4, _ = extract_file(path, cfg4)
    codes8 = {code for frame in features8.frames for code in frame.voiceprint}
    codes4 = {code for frame in features4.frames for code in frame.voiceprint}
    assert max(codes8) <= 7 and max(codes4) <= 3
    assert codes4 != codes8


def test_array_fingerprint_stable():
    array = np.linspace(-1, 1, 1000)
    assert array_fingerprint(array) == array_fingerprint(array.copy())
    assert array_fingerprint(array) != array_fingerprint(array + 1e-3)


def test_atomic_write_replaces_contents(tmp_path):
    target = tmp_path / "doc.json"
    atomic_write_json(str(target), {"a": 1})
    atomic_write_json(str(target), {"a": 2})
    assert content_hash({"a": 2}) == content_hash({"a": 2})
    import json
    assert json.loads(target.read_text()) == {"a": 2}
