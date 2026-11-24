"""Deterministic fixture generator for the delivery pipeline."""

from __future__ import annotations

import json
import sqlite3
import struct
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .util import canonical_json_bytes, sha256_file
from .wavio import encode_wav_bytes


SEED = 20261125


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, value: Any, *, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        path.write_text(text + "\n", encoding="utf-8")
    else:
        path.write_bytes(canonical_json_bytes(value) + b"\n")


def _tone_blocked(rng: np.random.Generator, sample_rate: int, seconds: float,
                  channels: int, amplitudes: list[float],
                  frequencies: list[float]) -> np.ndarray:
    frames = int(round(seconds * sample_rate))
    block = max(1, frames // len(amplitudes))
    result = np.zeros((frames, channels), dtype=np.float64)
    for channel in range(channels):
        for index, (amplitude, frequency) in enumerate(zip(amplitudes, frequencies)):
            start = index * block
            end = frames if index == len(amplitudes) - 1 else (index + 1) * block
            t = np.arange(end - start, dtype=np.float64) / sample_rate
            wave_signal = (
                np.sin(2.0 * np.pi * frequency * t)
                + 0.27 * np.sin(2.0 * np.pi * (frequency * 2.01) * t + 0.3 * channel)
            )
            result[start:end, channel] += amplitude * wave_signal
    noise = rng.normal(0.0, 0.0004, size=result.shape)
    result = np.clip(result * 0.72 + noise, -1.0, 1.0)
    return result.astype(np.float32)


def _write_pcm_wav(path: Path, samples: np.ndarray, sample_rate: int,
                   bits: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if bits in (16, 24):
        _write_bytes(path, encode_wav_bytes(samples, sample_rate, bits))
        return
    if bits == 8:
        unsigned = np.clip(
            np.rint((samples.astype(np.float64) * 0.5 + 0.5) * 255.0), 0, 255
        ).astype(np.uint8)
        channels = samples.shape[1]
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(1)
            handle.setframerate(sample_rate)
            handle.writeframes(unsigned.tobytes())
        return
    raise ValueError(f"unsupported fixture bit depth {bits}")


def _criterion(record_id: str, canonical_id: str, criterion: str,
               generation: int, effective_ms: int, *, tombstone: bool = False,
               **data: Any) -> dict[str, Any]:
    entry = {
        "record_id": record_id,
        "canonical_id": canonical_id,
        "criterion": criterion,
        "generation": generation,
        "effective_ms": effective_ms,
        "tombstone": tombstone,
    }
    entry.update(data)
    return entry


def _build_v1() -> dict[str, Any]:
    entries = [
        {
            "record_id": "v1-promo-window",
            "canonical_id": "program/promo",
            "criterion": "window",
            "generation": 1,
            "effective_seconds": 0.25,
            "window_ms": 120,
            "hop_ms": 60,
            "policy": "zero_pad",
        },
        {
            "record_id": "v1-promo-relative",
            "canonical_id": "program/promo",
            "criterion": "relative_gate",
            "generation": 1,
            "effective_seconds": 0.5,
            "threshold": -9.0,
        },
        {
            "record_id": "v1-promo-absolute",
            "canonical_id": "program/promo",
            "criterion": "absolute_gate",
            "generation": 1,
            "effective_seconds": 0.5,
            "threshold_lufs": -68.0,
        },
        {
            "record_id": "v1-promo-target",
            "canonical_id": "program/promo",
            "criterion": "integrated_loudness",
            "generation": 1,
            "effective_seconds": 0.5,
            "target_lufs": -23.0,
        },
        {
            "record_id": "v1-promo-dr",
            "canonical_id": "program/promo",
            "criterion": "dynamic_range",
            "generation": 1,
            "effective_seconds": 0.5,
            "min_db": 5.0,
            "max_db": 13.0,
        },
        {
            "record_id": "v1-promo-compression",
            "canonical_id": "program/promo",
            "criterion": "compression",
            "generation": 1,
            "effective_seconds": 0.5,
            "threshold_lufs": -17.0,
            "ratio": 2.0,
            "enabled": True,
        },
        {
            "record_id": "v1-voice-window",
            "canonical_id": "program/voice",
            "criterion": "window",
            "generation": 1,
            "effective_seconds": 0.25,
            "window_ms": 200,
            "hop_ms": 100,
            "policy": "zero_pad",
        },
        {
            "record_id": "v1-voice-dr",
            "canonical_id": "program/voice",
            "criterion": "dynamic_range",
            "generation": 1,
            "effective_seconds": 0.5,
            "min_db": 4.0,
            "max_db": 14.0,
        },
        {
            "record_id": "v1-voice-target",
            "canonical_id": "program/voice",
            "criterion": "integrated_loudness",
            "generation": 1,
            "effective_seconds": 0.5,
            "target_lufs": -24.0,
        },
        {
            "record_id": "v1-silent-absolute",
            "canonical_id": "program/silent",
            "criterion": "absolute_gate",
            "generation": 1,
            "effective_seconds": 0.5,
            "threshold_lufs": -70.0,
        },
    ]
    return {
        "schema": "criteria-v1/1",
        "snapshot_id": "delivery-criteria-v1",
        "logical_origin_ms": 0,
        "expected_windows": len(entries),
        "entries": entries,
    }


def _build_v2() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    core_entries = [
        _criterion("v2-promo-window", "program/promo", "window", 2, 1500,
                   window_ms=140, hop_ms=70, policy="zero_pad"),
        _criterion("v2-promo-relative", "program/promo", "relative_gate", 2, 1600,
                   threshold=-9.5),
        _criterion("v2-promo-dr", "program/promo", "dynamic_range", 2, 1700,
                   min_db=6.0, max_db=11.0),
        _criterion(
            "v2-promo-compression", "program/promo", "compression", 2, 1700,
            suggestion_rule={
                "threshold_lufs": -19.0,
                "ratio": 2.5,
                "enabled": True,
                "mode": "lookahead",
            },
        ),
        _criterion("v2-voice-window", "program/voice", "window", 2, 1500,
                   window_ms=180, hop_ms=90, policy="zero_pad"),
        _criterion("v2-silent-absolute", "program/silent", "absolute_gate", 2,
                   1500, threshold_lufs=-69.0),
        _criterion("v2-promo-legacy", "program/promo", "legacy_peak", 2, 1500,
                   threshold_db=-10.0),
    ]
    tombstone_entries = [
        _criterion("v2-promo-legacy-tombstone", "program/promo", "legacy_peak",
                   3, 4000, tombstone=True),
    ]
    aux_entries = [
        _criterion("v2-promo-aux", "program/promo", "audit_note", 2, 1500,
                   threshold_db=-12.0),
    ]
    core = {
        "schema": "criteria-v2-shard/1",
        "shard_id": "core",
        "expected_windows": len(core_entries),
        "entries": core_entries,
    }
    tombstones = {
        "schema": "criteria-v2-shard/1",
        "shard_id": "tombstones",
        "expected_windows": len(tombstone_entries),
        "entries": tombstone_entries,
    }
    aux = {
        "schema": "criteria-v2-shard/1",
        "shard_id": "aux",
        "expected_windows": len(aux_entries),
        "entries": aux_entries,
    }
    return core, tombstones, aux


def _write_v2_db(path: Path, shards: list[tuple[str, Path, dict[str, Any]]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE snapshot_shards (
                shard_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                expected_windows INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                logical_origin_ms INTEGER NOT NULL
            );
            CREATE TABLE criteria (
                record_id TEXT PRIMARY KEY,
                shard_id TEXT NOT NULL,
                canonical_input_id TEXT NOT NULL,
                criterion TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effective_ms INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                suggestion_rule TEXT,
                payload_json TEXT NOT NULL
            );
            """
        )
        connection.execute("INSERT INTO meta VALUES (?, ?)",
                           ("schema", "criteria-v2/1"))
        connection.execute("INSERT INTO meta VALUES (?, ?)",
                           ("logical_epoch_ms", "0"))
        for shard_id, shard_path, document in shards:
            digest = sha256_file(shard_path)
            entries = document["entries"]
            connection.execute(
                "INSERT INTO snapshot_shards VALUES (?, ?, ?, ?, ?, ?)",
                (shard_id, shard_path.name, digest, len(entries), 2, 0),
            )
            for entry in entries:
                suggestion = entry.get("suggestion_rule")
                connection.execute(
                    "INSERT INTO criteria VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["record_id"],
                        shard_id,
                        entry["canonical_id"],
                        entry["criterion"],
                        int(entry["generation"]),
                        int(entry["effective_ms"]),
                        int(bool(entry["tombstone"])),
                        json.dumps(suggestion, sort_keys=True) if suggestion else None,
                        canonical_json_bytes(entry).decode("utf-8"),
                    ),
                )
        connection.commit()
    finally:
        connection.close()


def generate_fixtures(workspace: Path, *, force: bool = False) -> dict[str, Any]:
    workspace = workspace.resolve()
    known = ["task.json", "anomalies.json", "media", "criteria", "output", "state"]
    if workspace.exists() and any((workspace / name).exists() for name in known) and not force:
        raise FileExistsError(
            f"{workspace} already contains generated fixtures; use --force to regenerate"
        )
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "media").mkdir(exist_ok=True)
    (workspace / "criteria").mkdir(exist_ok=True)
    (workspace / "criteria" / "v2").mkdir(exist_ok=True)

    rng = np.random.default_rng(SEED)
    promo = _tone_blocked(
        rng, 48000, 2.6, 2, [0.42, 0.08, 0.72], [220.0, 440.0, 880.0]
    )
    voice = _tone_blocked(
        rng, 44100, 2.0, 1, [0.08, 0.06, 0.10], [180.0, 250.0, 320.0]
    )
    silent = np.zeros((int(1.2 * 48000), 2), dtype=np.float32)
    bad_depth = _tone_blocked(
        rng, 44100, 1.0, 1, [0.22, 0.1], [300.0, 500.0]
    )
    wrong_rate = _tone_blocked(
        rng, 48000, 1.2, 2, [0.28, 0.12], [260.0, 520.0]
    )
    _write_pcm_wav(workspace / "media" / "promo.wav", promo, 48000, 24)
    _write_pcm_wav(workspace / "media" / "voice.wav", voice, 44100, 16)
    _write_pcm_wav(workspace / "media" / "silent.wav", silent, 48000, 24)
    _write_pcm_wav(workspace / "media" / "bad_depth.wav", bad_depth, 44100, 8)
    _write_pcm_wav(workspace / "media" / "wrong_rate.wav", wrong_rate, 48000, 16)

    v1_path = workspace / "criteria" / "v1.json"
    _write_json(v1_path, _build_v1())
    core, tombstones, aux = _build_v2()
    shard_dir = workspace / "criteria" / "v2"
    core_path = shard_dir / "core.json"
    tombstones_path = shard_dir / "tombstones.json"
    aux_path = shard_dir / "aux.json"
    _write_json(core_path, core)
    _write_json(tombstones_path, tombstones)
    _write_json(aux_path, aux)
    db_path = workspace / "criteria" / "v2.sqlite3"
    _write_v2_db(
        db_path,
        [("core", core_path, core), ("tombstones", tombstones_path, tombstones),
         ("aux", aux_path, aux)],
    )

    anomalies = {
        "schema": "anomaly-injections/1",
        "seed": SEED,
        "items": [
            {
                "injection_id": "inject-invalid-depth",
                "kind": "invalid_bit_depth",
                "target_input_id": "bad_depth",
                "injected_value": 8,
            },
            {
                "injection_id": "inject-wrong-rate-declaration",
                "kind": "wrong_sample_rate_declaration",
                "target_input_id": "wrong_rate",
                "actual_sample_rate": 48000,
                "declared_sample_rate": 44100,
            },
            {
                "injection_id": "inject-corrupt-shard",
                "kind": "corrupt_shard",
                "target": "aux.json",
                "shard_id": "aux",
                "note": "optional audit shard is corrupted after metadata digest capture",
            },
        ],
    }
    anomalies_path = workspace / "anomalies.json"
    _write_json(anomalies_path, anomalies)

    # Corrupt only after the SQLite shard digest was recorded.
    with aux_path.open("ab") as handle:
        handle.write(b'\n{"corrupt":')

    task = {
        "schema": "loudness-task/1",
        "task_id": "broadcast-delivery-demo",
        "allowed_sample_rates": [44100, 48000, 96000],
        "allowed_bit_depths": [16, 24],
        "measurement": {
            "window_ms": 100,
            "hop_ms": 50,
            "partial_window_policy": "zero_pad",
            "relative_gate_lu": -10.0,
            "absolute_gate_lufs": -70.0,
            "dynamic_range_min_db": 4.0,
            "dynamic_range_max_db": 12.0,
            "target_lufs": -23.0,
            "compression_threshold_lufs": -18.0,
            "compression_ratio": 2.0,
            "target_sample_rate": 48000,
            "output_bit_depth": 24,
            "max_workers": 3,
            "decode_buffer_bytes": 1048576,
            "sample_buffer_bytes": 16777216,
            "checkpoint_windows": 4,
            "decode_block_frames": 4096,
        },
        "inputs": [
            {
                "id": "promo",
                "canonical_id": "program/promo",
                "path": "media/promo.wav",
                "layout": "stereo",
                "channels": 2,
                "weights": [0.5, 0.5],
                "declared_sample_rate": 48000,
                "declared_bit_depth": 24,
            },
            {
                "id": "voice",
                "canonical_id": "program/voice",
                "path": "media/voice.wav",
                "layout": "mono",
                "channels": 1,
                "weights": [1.0],
                "declared_sample_rate": 44100,
                "declared_bit_depth": 16,
            },
            {
                "id": "bad_depth",
                "canonical_id": "program/bad-depth",
                "path": "media/bad_depth.wav",
                "layout": "mono",
                "channels": 1,
                "weights": [1.0],
                "declared_sample_rate": 44100,
                "declared_bit_depth": 8,
            },
            {
                "id": "silent",
                "canonical_id": "program/silent",
                "path": "media/silent.wav",
                "layout": "stereo",
                "channels": 2,
                "weights": [0.5, 0.5],
                "declared_sample_rate": 48000,
                "declared_bit_depth": 24,
            },
            {
                "id": "wrong_rate",
                "canonical_id": "program/wrong-rate",
                "path": "media/wrong_rate.wav",
                "layout": "stereo",
                "channels": 2,
                "weights": [0.5, 0.5],
                "declared_sample_rate": 44100,
                "declared_bit_depth": 16,
            },
        ],
        "snapshots": {
            "v1": {
                "path": "criteria/v1.json",
                "sha256": sha256_file(v1_path),
                "expected_entries": len(_build_v1()["entries"]),
                "logical_origin_ms": 500,
            },
            "v2": {
                "database": "criteria/v2.sqlite3",
                "sha256": sha256_file(db_path),
                "shard_root": "criteria/v2",
            },
        },
        "anomalies": "anomalies.json",
    }
    task_path = workspace / "task.json"
    _write_json(task_path, task)
    return {
        "workspace": str(workspace),
        "task": str(task_path),
        "seed": SEED,
        "inputs": [entry["id"] for entry in task["inputs"]],
        "anomalies": [entry["injection_id"] for entry in anomalies["items"]],
    }
