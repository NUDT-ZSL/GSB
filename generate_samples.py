#!/usr/bin/env python3
"""Generate deterministic 16 kHz meeting PCM, truth frames, and VAD rules."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from vad_engine.util import atomic_write_bytes, atomic_write_json, quantize


@dataclass(frozen=True)
class UtteranceSpec:
    utterance_id: str
    speaker_id: str
    start_ms: int
    end_ms: int
    amplitude: float


@dataclass(frozen=True)
class GainJumpSpec:
    jump_id: str
    speaker_id: str
    utterance_id: str
    time_ms: int
    gain: float


@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    duration_ms: int
    noise_floor: float
    speakers: tuple[str, ...]
    utterances: tuple[UtteranceSpec, ...]
    gain_jumps: tuple[GainJumpSpec, ...]


SESSIONS: tuple[SessionSpec, ...] = (
    SessionSpec(
        session_id="session_001",
        duration_ms=6600,
        noise_floor=0.0018,
        speakers=("spk_a", "spk_b", "spk_c"),
        utterances=(
            UtteranceSpec("session_001:utt_001", "spk_a", 350, 1250, 0.085),
            UtteranceSpec("session_001:utt_002", "spk_b", 1500, 2450, 0.115),
            UtteranceSpec("session_001:utt_003", "spk_a", 2700, 3500, 0.105),
            UtteranceSpec("session_001:utt_004", "spk_c", 3750, 4500, 0.095),
            UtteranceSpec("session_001:utt_005", "spk_b", 4750, 5700, 0.100),
            UtteranceSpec("session_001:utt_006", "spk_c", 5900, 6400, 0.090),
        ),
        gain_jumps=(
            GainJumpSpec("session_001:gain_001", "spk_b", "session_001:utt_002", 1950, 1.90),
            GainJumpSpec("session_001:gain_002", "spk_a", "session_001:utt_003", 3100, 0.55),
            GainJumpSpec("session_001:gain_003", "spk_b", "session_001:utt_005", 5200, 2.20),
        ),
    ),
    SessionSpec(
        session_id="session_002",
        duration_ms=7000,
        noise_floor=0.0024,
        speakers=("spk_a", "spk_b", "spk_c"),
        utterances=(
            UtteranceSpec("session_002:utt_001", "spk_b", 300, 1000, 0.100),
            UtteranceSpec("session_002:utt_002", "spk_c", 1150, 2200, 0.090),
            UtteranceSpec("session_002:utt_003", "spk_a", 2400, 3300, 0.110),
            UtteranceSpec("session_002:utt_004", "spk_b", 3500, 4400, 0.105),
            UtteranceSpec("session_002:utt_005", "spk_a", 4700, 5900, 0.095),
            UtteranceSpec("session_002:utt_006", "spk_c", 6100, 6800, 0.100),
        ),
        gain_jumps=(
            GainJumpSpec("session_002:gain_001", "spk_c", "session_002:utt_002", 1700, 1.70),
            GainJumpSpec("session_002:gain_002", "spk_b", "session_002:utt_004", 4000, 0.50),
            GainJumpSpec("session_002:gain_003", "spk_a", "session_002:utt_005", 5300, 2.40),
        ),
    ),
    SessionSpec(
        session_id="session_003",
        duration_ms=6200,
        noise_floor=0.0012,
        speakers=("spk_a", "spk_b", "spk_c"),
        utterances=(
            UtteranceSpec("session_003:utt_001", "spk_c", 250, 950, 0.090),
            UtteranceSpec("session_003:utt_002", "spk_a", 1200, 2000, 0.105),
            UtteranceSpec("session_003:utt_003", "spk_b", 2200, 3100, 0.095),
            UtteranceSpec("session_003:utt_004", "spk_a", 3400, 4100, 0.100),
            UtteranceSpec("session_003:utt_005", "spk_c", 4300, 5000, 0.092),
            UtteranceSpec("session_003:utt_006", "spk_b", 5300, 6000, 0.106),
        ),
        gain_jumps=(
            GainJumpSpec("session_003:gain_001", "spk_c", "session_003:utt_001", 600, 1.60),
            GainJumpSpec("session_003:gain_002", "spk_a", "session_003:utt_004", 3800, 2.00),
        ),
    ),
)


SPEAKER_TONES = {
    "spk_a": (148.0, 0.55, 0.24, 0.11),
    "spk_b": (191.0, 0.48, 0.28, 0.13),
    "spk_c": (232.0, 0.52, 0.21, 0.10),
}


def _smooth_fade(length: int, fade_samples: int) -> np.ndarray:
    envelope = np.ones(length, dtype=np.float64)
    fade = min(fade_samples, length // 2)
    if fade <= 0:
        return envelope
    ramp = np.linspace(0.0, 1.0, fade, endpoint=False, dtype=np.float64)
    envelope[:fade] = ramp
    envelope[-fade:] = ramp[::-1]
    return envelope


def _synthesize(spec: SessionSpec, seed: int) -> tuple[np.ndarray, dict[str, Any]]:
    sample_rate = 16_000
    total_samples = spec.duration_ms * sample_rate // 1000
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, spec.noise_floor, total_samples).astype(np.float64)
    speech = np.zeros(total_samples, dtype=np.float64)

    for utterance_index, utterance in enumerate(spec.utterances):
        start = utterance.start_ms * sample_rate // 1000
        end = utterance.end_ms * sample_rate // 1000
        length = end - start
        t = np.arange(length, dtype=np.float64) / float(sample_rate)
        f0, h2, h3, h4 = SPEAKER_TONES[utterance.speaker_id]
        phase = rng.uniform(0.0, 2.0 * math.pi)
        harmonic = (
            np.sin(2.0 * math.pi * f0 * t + phase)
            + h2 * np.sin(2.0 * math.pi * 2.0 * f0 * t + 0.31 * phase)
            + h3 * np.sin(2.0 * math.pi * 3.0 * f0 * t + 0.67 * phase)
            + h4 * np.sin(2.0 * math.pi * 4.0 * f0 * t + 1.13 * phase)
        )
        harmonic /= 1.0 + h2 + h3 + h4
        syllabic = (
            0.86
            + 0.10 * np.sin(2.0 * math.pi * 4.1 * t + 0.23 * utterance_index)
            + 0.04 * np.sin(2.0 * math.pi * 7.3 * t + 0.71 * utterance_index)
        )
        fade = _smooth_fade(length, int(0.020 * sample_rate))
        signal = utterance.amplitude * harmonic * syllabic * fade

        for jump in spec.gain_jumps:
            if jump.utterance_id != utterance.utterance_id:
                continue
            jump_sample = jump.time_ms * sample_rate // 1000 - start
            if 0 <= jump_sample < length:
                signal[jump_sample:] *= jump.gain
        speech[start:end] += signal

    mixture = np.clip(speech + noise, -0.98, 0.98)
    pcm = np.rint(mixture * 32767.0).astype("<i2")
    meta = {
        "session_id": spec.session_id,
        "seed": seed,
        "sample_rate": sample_rate,
        "duration_ms": spec.duration_ms,
        "noise_floor": spec.noise_floor,
        "speakers": list(spec.speakers),
        "utterances": [
            {
                "utterance_id": utterance.utterance_id,
                "speaker_id": utterance.speaker_id,
                "start_ms": utterance.start_ms,
                "end_ms": utterance.end_ms,
                "amplitude": utterance.amplitude,
            }
            for utterance in spec.utterances
        ],
        "gain_jumps": [
            {
                "jump_id": jump.jump_id,
                "speaker_id": jump.speaker_id,
                "utterance_id": jump.utterance_id,
                "time_ms": jump.time_ms,
                "gain": jump.gain,
            }
            for jump in spec.gain_jumps
        ],
    }
    return pcm, meta


def _truth_rows(pcm: np.ndarray, meta: dict[str, Any]) -> list[dict[str, Any]]:
    sample_rate = int(meta["sample_rate"])
    frame_samples = sample_rate * 20 // 1000
    frame_count = len(pcm) // frame_samples
    rows: list[dict[str, Any]] = []
    for frame_index in range(frame_count):
        sample_start = frame_index * frame_samples
        sample_end = sample_start + frame_samples
        midpoint_ms = ((sample_start + sample_end) // 2) * 1000 // sample_rate
        active = None
        for utterance in meta["utterances"]:
            if utterance["start_ms"] <= midpoint_ms < utterance["end_ms"]:
                active = utterance
                break
        rows.append(
            {
                "frame_index": frame_index,
                "sample_start": sample_start,
                "sample_end": sample_end,
                "is_speech": active is not None,
                "speaker_id_true": active["speaker_id"] if active else None,
                "noise_floor_true": meta["noise_floor"],
                "utterance_id": active["utterance_id"] if active else None,
                "segment_start_ms": active["start_ms"] if active else None,
                "segment_end_ms": active["end_ms"] if active else None,
            }
        )
    return rows


def _median_speech_rms(
    pcm: np.ndarray, sample_rate: int, utterance: dict[str, Any], frame_samples: int
) -> float:
    start = int(utterance["start_ms"]) * sample_rate // 1000
    end = int(utterance["end_ms"]) * sample_rate // 1000
    values: list[float] = []
    for offset in range(start, max(start, end - frame_samples + 1), frame_samples):
        block = pcm[offset : offset + frame_samples].astype(np.float64) / 32768.0
        if len(block) >= frame_samples:
            values.append(float(np.sqrt(np.dot(block, block) / frame_samples)))
    if not values:
        return 0.05
    return float(np.median(np.asarray(values, dtype=np.float64)))


def _v1_payload(pcm: np.ndarray, meta: dict[str, Any]) -> dict[str, Any]:
    sample_rate = int(meta["sample_rate"])
    frame_samples = sample_rate * 20 // 1000
    rules: list[dict[str, Any]] = []
    for utterance in meta["utterances"]:
        rms = _median_speech_rms(pcm, sample_rate, utterance, frame_samples)
        rules.append(
            {
                "speaker_id": utterance["speaker_id"],
                "start_sec": round(utterance["start_ms"] / 1000.0, 3),
                "end_sec": round(utterance["end_ms"] / 1000.0, 3),
                "energy_threshold": quantize(max(0.008, rms * 0.96)),
                "min_silence_ms": 180 + (sum(map(ord, utterance["speaker_id"])) % 3) * 20,
            }
        )
    rules.sort(key=lambda item: (item["start_sec"], item["speaker_id"]))
    return {
        "session_id": meta["session_id"],
        "sample_rate": sample_rate,
        "frame_ms": 20,
        "hop_ms": 10,
        "noise_gate": quantize(max(meta["noise_floor"] * 2.4, 0.003)),
        "speakers": meta["speakers"],
        "rules": rules,
    }


def _v2_rules(pcm: np.ndarray, meta: dict[str, Any]) -> list[dict[str, Any]]:
    sample_rate = int(meta["sample_rate"])
    frame_samples = sample_rate * 20 // 1000
    base_by_utterance: dict[str, dict[str, Any]] = {}
    rules: list[dict[str, Any]] = []
    for index, utterance in enumerate(meta["utterances"], start=1):
        rms = _median_speech_rms(pcm, sample_rate, utterance, frame_samples)
        threshold = quantize(max(0.008, rms * 0.96))
        base = {
            "rule_id": f"{meta['session_id']}:g1:{index:03d}",
            "speaker_id": utterance["speaker_id"],
            "effective_ms": int(utterance["start_ms"]),
            "end_ms": int(utterance["end_ms"]),
            "energy_threshold": threshold,
            "min_silence_ms": 180 + (sum(map(ord, utterance["speaker_id"])) % 3) * 20,
            "rule_generation": 1,
            "scope": "speaker",
            "tombstone": False,
            "shard_index": 0,
        }
        base_by_utterance[utterance["utterance_id"]] = base
        rules.append(base)

    for index, jump in enumerate(meta["gain_jumps"], start=1):
        base = base_by_utterance[jump["utterance_id"]]
        rules.append(
            {
                "rule_id": f"{meta['session_id']}:g2:gain:{index:03d}",
                "speaker_id": jump["speaker_id"],
                "effective_ms": int(jump["time_ms"]),
                "end_ms": int(base["end_ms"]),
                "energy_threshold": quantize(
                    max(0.008, float(base["energy_threshold"]) * float(jump["gain"]))
                ),
                "min_silence_ms": int(base["min_silence_ms"]),
                "rule_generation": 2,
                "scope": "speaker",
                "tombstone": False,
                "shard_index": 1,
            }
        )

    last = meta["utterances"][-1]
    tombstone_start = max(0, int(meta["duration_ms"]) - 180)
    rules.append(
        {
            "rule_id": f"{meta['session_id']}:g2:tombstone:001",
            "speaker_id": last["speaker_id"],
            "effective_ms": tombstone_start,
            "end_ms": tombstone_start + 60,
            "energy_threshold": 0.0,
            "min_silence_ms": 0,
            "rule_generation": 2,
            "scope": f"speaker:{last['speaker_id']}",
            "tombstone": True,
            "shard_index": 1,
        }
    )
    rules.sort(key=lambda item: (item["shard_index"], item["effective_ms"], item["rule_id"]))
    return rules


def _write_v2(root: Path, meta: dict[str, Any], pcm: np.ndarray) -> None:
    session_dir = root / "rules" / "v2" / meta["session_id"]
    if session_dir.exists():
        shutil.rmtree(session_dir)
    shard_dir = session_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    rules = _v2_rules(pcm, meta)
    shards: list[dict[str, Any]] = []
    for shard_index in (0, 1):
        shard_rules = [rule for rule in rules if rule["shard_index"] == shard_index]
        filename = f"shard_{shard_index:03d}.json"
        shard_payload = {
            "format": "vad-rule-shard/v2",
            "shard_index": shard_index,
            "rules": shard_rules,
        }
        atomic_write_json(shard_dir / filename, shard_payload)
        shards.append(
            {
                "shard_index": shard_index,
                "path": f"shards/{filename}",
                "rule_count": len(shard_rules),
            }
        )

    database_path = session_dir / "metadata.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            """
            CREATE TABLE rule_metadata (
                rule_id TEXT PRIMARY KEY,
                shard_index INTEGER NOT NULL,
                speaker_id TEXT NOT NULL,
                effective_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                rule_generation INTEGER NOT NULL,
                scope TEXT NOT NULL,
                tombstone INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("format", "vad-rule-metadata/v2"),
                ("session_id", meta["session_id"]),
                ("rule_generation", "2"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO rule_metadata(
                rule_id, shard_index, speaker_id, effective_ms, end_ms,
                rule_generation, scope, tombstone
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    rule["rule_id"],
                    rule["shard_index"],
                    rule["speaker_id"],
                    rule["effective_ms"],
                    rule["end_ms"],
                    rule["rule_generation"],
                    rule["scope"],
                    1 if rule["tombstone"] else 0,
                )
                for rule in rules
            ],
        )
        connection.commit()
    finally:
        connection.close()

    manifest = {
        "format": "vad-rules/v2",
        "version": 2,
        "session_id": meta["session_id"],
        "sample_rate": meta["sample_rate"],
        "frame_ms": 20,
        "hop_ms": 10,
        "noise_gate": quantize(max(meta["noise_floor"] * 2.4, 0.003)),
        "metadata_db": "metadata.sqlite3",
        "rule_generation": 2,
        "shards": shards,
        "speaker_labels": meta["speakers"],
    }
    atomic_write_json(session_dir / "manifest.json", manifest)


def generate_all(output_dir: str | Path, seed: int = 20261126) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    for subdirectory in ("pcm", "truth", "meta", "rules"):
        path = root / subdirectory
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

    manifest_sessions: list[dict[str, Any]] = []
    for index, spec in enumerate(SESSIONS):
        session_seed = int(seed) + index * 100_003
        pcm, meta = _synthesize(spec, session_seed)
        truth = _truth_rows(pcm, meta)
        v1 = _v1_payload(pcm, meta)

        pcm_name = f"{spec.session_id}.pcm"
        atomic_write_bytes(root / "pcm" / pcm_name, pcm.tobytes(order="C"))
        atomic_write_json(root / "meta" / f"{spec.session_id}.json", meta)
        truth_text = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in truth
        )
        atomic_write_bytes(root / "truth" / f"{spec.session_id}.jsonl", truth_text.encode("utf-8"))
        atomic_write_json(root / "rules" / "v1" / f"{spec.session_id}.json", v1)
        _write_v2(root, meta, pcm)

        manifest_sessions.append(
            {
                "session_id": spec.session_id,
                "seed": session_seed,
                "duration_ms": spec.duration_ms,
                "sample_count": int(len(pcm)),
                "pcm": f"pcm/{pcm_name}",
                "truth": f"truth/{spec.session_id}.jsonl",
                "meta": f"meta/{spec.session_id}.json",
                "rules_v1": f"rules/v1/{spec.session_id}.json",
                "rules_v2": f"rules/v2/{spec.session_id}",
                "utterance_ids": [utterance.utterance_id for utterance in spec.utterances],
            }
        )

    manifest = {
        "format": "vad-sample-set/v1",
        "seed": int(seed),
        "sample_rate": 16_000,
        "channels": 1,
        "sample_width_bytes": 2,
        "encoding": "pcm_s16le",
        "truth_frame_ms": 20,
        "sessions": manifest_sessions,
    }
    atomic_write_json(root / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data", help="output directory (default: data)")
    parser.add_argument("--seed", type=int, default=20261126, help="fixed root seed")
    args = parser.parse_args(argv)
    manifest = generate_all(args.out, args.seed)
    print(
        "generated "
        f"{len(manifest['sessions'])} sessions, seed={manifest['seed']}, "
        f"sample_rate={manifest['sample_rate']}, output={args.out}"
    )
    for session in manifest["sessions"]:
        print(
            f"{session['session_id']}: samples={session['sample_count']} "
            f"utterances={len(session['utterance_ids'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
