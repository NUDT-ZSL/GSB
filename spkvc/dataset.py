"""Deterministic local dataset generation and loading."""

from __future__ import annotations

import io
import json
import math
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import PRECISION_PROFILES, PipelineConfig, decimals_for
from .errors import fail
from .models import ManifestEntry, canonical_bytes, sha256_bytes, write_json_atomic


PHONEMES: tuple[str, ...] = ("a", "i", "u", "e", "o", "m", "n", "s")


def _speaker_parameters(index: int) -> dict[str, Any]:
    rng = np.random.Generator(np.random.PCG64(10_000 + index))
    f0_base = 92.0 + 27.0 * index + float(rng.uniform(-3.0, 3.0))
    formants = [
        480.0 + 55.0 * index + float(rng.uniform(-15.0, 15.0)),
        1420.0 + 95.0 * index + float(rng.uniform(-25.0, 25.0)),
        2480.0 + 120.0 * index + float(rng.uniform(-35.0, 35.0)),
    ]
    energy = 0.16 + 0.018 * index + float(rng.uniform(-0.005, 0.005))
    return {
        "f0_base": round(f0_base, 6),
        "formants": [round(value, 6) for value in formants],
        "energy": round(energy, 6),
    }


def _revision_parameters(base: dict[str, Any], revision: int) -> dict[str, Any]:
    if revision <= 1:
        return {
            "f0_base": float(base["f0_base"]),
            "formants": [float(value) for value in base["formants"]],
            "energy": float(base["energy"]),
        }
    factor = 1.0 + 0.035 * (revision - 1)
    return {
        "f0_base": round(float(base["f0_base"]) * factor, 6),
        "formants": [
            round(float(value) * (1.0 + 0.022 * (revision - 1)), 6)
            for value in base["formants"]
        ],
        "energy": round(float(base["energy"]) * (1.0 + 0.07 * (revision - 1)), 6),
    }


def _phoneme_schedule(seed: int, frame_count: int) -> list[int]:
    rng = np.random.Generator(np.random.PCG64(seed ^ 0x5EED))
    schedule: list[int] = []
    current = int(rng.integers(0, len(PHONEMES)))
    remaining = 0
    for _ in range(frame_count):
        if remaining <= 0:
            current = int(rng.integers(0, len(PHONEMES)))
            remaining = int(rng.integers(3, 7))
        schedule.append(current)
        remaining -= 1
    return schedule


def _harmonic_weights(
    f0: float, formants: Iterable[float], harmonics: int = 8
) -> np.ndarray:
    weights = np.zeros(harmonics, dtype=np.float64)
    formant_values = list(formants)
    for index in range(harmonics):
        harmonic = index + 1
        value = 0.18 / harmonic
        for formant in formant_values:
            distance = harmonic * f0 - formant
            value += math.exp(-0.5 * (distance / 260.0) ** 2)
        weights[index] = value
    norm = float(np.sqrt(np.sum(weights * weights) * 0.5))
    if norm <= 1e-12:
        weights[0] = 1.0
        norm = float(np.sqrt(np.sum(weights * weights) * 0.5))
    return weights / norm


def _synthesize(
    *,
    seed: int,
    frame_count: int,
    speaker_params: dict[str, Any],
    frame_samples: int,
    sample_rate: int,
) -> tuple[np.ndarray, list[float], list[int]]:
    rng = np.random.Generator(np.random.PCG64(seed))
    schedule = _phoneme_schedule(seed, frame_count)
    f0_base = float(speaker_params["f0_base"])
    formants = [float(value) for value in speaker_params["formants"]]
    energy_base = float(speaker_params["energy"])

    pcm = np.zeros(frame_count * frame_samples, dtype=np.int16)
    f0_values: list[float] = []
    phase_cycles = 0.0
    for frame_index in range(frame_count):
        vowel_shift = 1.0 + 0.018 * math.sin(
            2.0 * math.pi * frame_index / max(5.0, frame_count / 3.0)
        )
        random_walk = float(rng.normal(0.0, 0.006))
        f0 = f0_base * (vowel_shift + random_walk)
        f0 = min(400.0, max(60.0, f0))
        envelope = 0.72 + 0.20 * math.sin(
            2.0 * math.pi * frame_index / 9.0 + 0.7
        )
        target_rms = max(0.006, energy_base * envelope)
        start = frame_index * frame_samples
        t = np.arange(frame_samples, dtype=np.float64) / sample_rate
        phase = 2.0 * math.pi * (phase_cycles + f0 * t)
        weights = _harmonic_weights(f0, formants)
        signal = np.zeros(frame_samples, dtype=np.float64)
        for harmonic, weight in enumerate(weights, start=1):
            signal += float(weight) * np.sin(harmonic * phase + 0.17 * harmonic)
        signal += 0.008 * rng.standard_normal(frame_samples)
        current_rms = float(np.sqrt(np.mean(signal * signal)))
        if current_rms > 1e-12:
            signal *= target_rms / current_rms
        phase_cycles += f0 * frame_samples / sample_rate
        pcm[start : start + frame_samples] = np.rint(
            np.clip(signal, -0.97, 0.97) * 32767.0
        ).astype(np.int16)
        f0_values.append(round(f0, 6))
    return pcm, f0_values, schedule


def write_wav(path: Path, pcm: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.astype("<i2", copy=False).tobytes())


def read_wav(path: str | Path, expected_sample_rate: int = 16000) -> tuple[np.ndarray, bytes]:
    source = Path(path)
    if not source.is_file():
        fail(
            "E_AUDIO_NOT_FOUND",
            "音频文件不存在",
            field="audio_path",
            details={"path": str(source)},
        )
    try:
        with wave.open(str(source), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        fail(
            "E_AUDIO_CORRUPT",
            "WAV 文件无法读取",
            field="audio_path",
            details={"path": str(source), "reason": str(exc)},
        )
    if sample_rate != expected_sample_rate:
        fail(
            "E_INVALID_SAMPLE_RATE",
            "音频采样率必须为 16000 Hz",
            field="sample_rate",
            details={
                "path": str(source),
                "actual": sample_rate,
                "expected": expected_sample_rate,
            },
        )
    if channels != 1 or width != 2:
        fail(
            "E_AUDIO_FORMAT",
            "音频必须为单声道 16-bit PCM",
            field="audio_path",
            details={
                "path": str(source),
                "channels": channels,
                "sample_width": width,
            },
        )
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True)
    return pcm, raw


def _truth_rows(
    pcm: np.ndarray,
    frame_f0: list[float],
    schedule: list[int],
    frame_samples: int,
    hop_samples: int,
    decimals: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if len(pcm) < frame_samples:
        return rows
    frame_count = 1 + (len(pcm) - frame_samples) // hop_samples
    for frame_index in range(frame_count):
        start = frame_index * hop_samples
        chunk_index = min(len(frame_f0) - 1, start // frame_samples)
        f0_true = frame_f0[chunk_index]
        frame = pcm[start : start + frame_samples].astype(np.float64) / 32768.0
        energy_true = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0
        rows.append(
            {
                "frame_index": frame_index,
                "f0_true": round(f0_true, decimals),
                "energy_true": round(energy_true, decimals),
                "phoneme_id": int(schedule[chunk_index]),
            }
        )
    return rows


@dataclass
class Dataset:
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.jsonl"

    @property
    def refs_path(self) -> Path:
        return self.root / "refs.json"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def truth_dir(self) -> Path:
        return self.root / "truth"

    @property
    def ref_dir(self) -> Path:
        return self.root / "refs"

    def utterance_audio_path(self, utterance_id: str) -> Path:
        return self.audio_dir / f"{utterance_id}.wav"

    def truth_path(self, utterance_id: str) -> Path:
        return self.truth_dir / f"{utterance_id}.jsonl"

    def ref_audio_path(self, ref_id: str, revision: int) -> Path:
        return self.ref_dir / ref_id / f"r{revision:04d}.wav"

    def load_entries(self) -> list[ManifestEntry]:
        if not self.manifest_path.is_file():
            fail(
                "E_MANIFEST_NOT_FOUND",
                "样本清单不存在",
                field="manifest_path",
                details={"path": str(self.manifest_path)},
            )
        entries: list[ManifestEntry] = []
        with open(self.manifest_path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    fail(
                        "E_MANIFEST_JSON",
                        "样本清单不是合法 JSONL",
                        field="manifest_path",
                        details={"line": line_number, "reason": str(exc)},
                    )
                entries.append(ManifestEntry.from_dict(value))
        return entries

    def load_refs(self) -> dict[str, dict[str, Any]]:
        if not self.refs_path.is_file():
            return {}
        value = json.loads(self.refs_path.read_text(encoding="utf-8"))
        refs = value.get("refs", {})
        return refs if isinstance(refs, dict) else {}

    def ref_speaker_id(self, ref_id: str) -> str:
        refs = self.load_refs()
        if ref_id in refs:
            return str(refs[ref_id].get("speaker_id", ref_id))
        return ref_id

    def load_config(self) -> PipelineConfig:
        if not self.config_path.is_file():
            return PipelineConfig()
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        return PipelineConfig.from_dict(value)

    def read_truth(self, utterance_id: str) -> list[dict[str, Any]]:
        path = self.truth_path(utterance_id)
        if not path.is_file():
            fail(
                "E_TRUTH_NOT_FOUND",
                "逐帧真值文件不存在",
                field="truth_path",
                details={"utterance_id": utterance_id, "path": str(path)},
            )
        rows: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    fail(
                        "E_TRUTH_CORRUPT",
                        "真值文件不是合法 JSONL",
                        field="truth_path",
                        details={
                            "utterance_id": utterance_id,
                            "line": line_number,
                            "reason": str(exc),
                        },
                    )
        return rows


def validate_entries(
    entries: list[ManifestEntry],
    *,
    dataset: Dataset | None = None,
    config: PipelineConfig | None = None,
) -> None:
    if config is not None:
        config.validate()
    seen: set[str] = set()
    for entry in entries:
        if entry.utterance_id in seen:
            fail(
                "E_DUPLICATE_UTTERANCE_ID",
                "utterance_id 重复",
                field="utterance_id",
                details={"utterance_id": entry.utterance_id},
            )
        seen.add(entry.utterance_id)
        if not entry.ref_id:
            fail(
                "E_MISSING_REF_ID",
                "ref_id 缺失",
                field="ref_id",
                details={"utterance_id": entry.utterance_id},
            )
        if entry.precision_profile not in PRECISION_PROFILES:
            fail(
                "E_UNKNOWN_PRECISION_PROFILE",
                "precision_profile 未知",
                field="precision_profile",
                details={
                    "utterance_id": entry.utterance_id,
                    "actual": entry.precision_profile,
                    "allowed": sorted(PRECISION_PROFILES),
                },
            )
        if entry.end_ms <= entry.start_ms:
            fail(
                "E_INVALID_TIMELINE",
                "end_ms 必须大于 start_ms",
                field="end_ms",
                details={"utterance_id": entry.utterance_id},
            )
        if entry.ref_revision < 1:
            fail(
                "E_INVALID_REF_REVISION",
                "ref_revision 必须为正整数",
                field="ref_revision",
                details={"utterance_id": entry.utterance_id},
            )
    if dataset is None:
        return
    for entry in entries:
        if not dataset.utterance_audio_path(entry.utterance_id).is_file():
            fail(
                "E_AUDIO_NOT_FOUND",
                "样本音频不存在",
                field="audio_path",
                details={
                    "utterance_id": entry.utterance_id,
                    "path": str(dataset.utterance_audio_path(entry.utterance_id)),
                },
            )
        if not dataset.truth_path(entry.utterance_id).is_file():
            fail(
                "E_TRUTH_NOT_FOUND",
                "逐帧真值文件不存在",
                field="truth_path",
                details={
                    "utterance_id": entry.utterance_id,
                    "path": str(dataset.truth_path(entry.utterance_id)),
                },
            )
        if not dataset.ref_audio_path(entry.ref_id, entry.ref_revision).is_file():
            fail(
                "E_REF_NOT_FOUND",
                "参考音色片段不存在",
                field="ref_id",
                details={
                    "utterance_id": entry.utterance_id,
                    "ref_id": entry.ref_id,
                    "ref_revision": entry.ref_revision,
                    "path": str(dataset.ref_audio_path(entry.ref_id, entry.ref_revision)),
                },
            )


def generate_dataset(
    root: str | Path,
    *,
    force: bool = False,
    speaker_count: int = 4,
    revisions: tuple[int, ...] = (1, 2),
    sample_rate: int = 16000,
    frame_ms: int = 20,
    hop_ms: int = 20,
) -> Dataset:
    target = Path(root)
    if target.exists() and force:
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    (target / "audio").mkdir(exist_ok=True)
    (target / "truth").mkdir(exist_ok=True)
    (target / "refs").mkdir(exist_ok=True)

    config = PipelineConfig(sample_rate=sample_rate, frame_ms=frame_ms, hop_ms=hop_ms)
    config.validate()
    revisions = tuple(sorted(set(int(revision) for revision in revisions)))
    if not revisions:
        fail(
            "E_INVALID_CONFIG",
            "至少需要一个参考 revision",
            field="revisions",
        )
    if revisions[0] < 1:
        fail(
            "E_INVALID_REF_REVISION",
            "参考 revision 必须为正整数",
            field="ref_revision",
            details={"actual": revisions[0]},
        )
    baseline_revision = 1 if 1 in revisions else min(revisions)
    frame_samples = config.frame_samples
    decimals = decimals_for("fp32")

    speakers = [f"spk-{index + 1:02d}" for index in range(speaker_count)]
    params = {speaker: _speaker_parameters(index) for index, speaker in enumerate(speakers)}
    refs_meta: dict[str, Any] = {}

    for index, speaker in enumerate(speakers):
        ref_id = f"ref-{speaker}"
        refs_meta[ref_id] = {
            "speaker_id": speaker,
            "revisions": list(revisions),
        }
        for revision in revisions:
            ref_params = _revision_parameters(params[speaker], revision)
            ref_pcm, _, _ = _synthesize(
                seed=90_000 + index * 100 + revision,
                frame_count=100,
                speaker_params=ref_params,
                frame_samples=frame_samples,
                sample_rate=sample_rate,
            )
            write_wav(
                target / "refs" / ref_id / f"r{revision:04d}.wav",
                ref_pcm,
                sample_rate,
            )

    durations = [640, 800, 560, 960, 720, 640, 880, 760]
    starts = [0, 1100, 2300, 3300, 4600, 5800, 6900, 8200]
    manifest_rows: list[dict[str, Any]] = []
    for index, duration_ms in enumerate(durations):
        source_speaker = speakers[index % speaker_count]
        target_speaker = speakers[(index + 1 + (index // speaker_count)) % speaker_count]
        if target_speaker == source_speaker:
            target_speaker = speakers[(index + 2) % speaker_count]
        ref_id = f"ref-{target_speaker}"
        utterance_id = f"utt-{index + 1:03d}"
        seed = 20_000 + index * 37
        frame_count = duration_ms // frame_ms
        pcm, f0_values, schedule = _synthesize(
            seed=seed,
            frame_count=frame_count,
            speaker_params=params[source_speaker],
            frame_samples=frame_samples,
            sample_rate=sample_rate,
        )
        write_wav(target / "audio" / f"{utterance_id}.wav", pcm, sample_rate)
        truth = _truth_rows(
            pcm,
            f0_values,
            schedule,
            frame_samples,
            config.hop_samples,
            decimals,
        )
        truth_path = target / "truth" / f"{utterance_id}.jsonl"
        with open(truth_path, "w", encoding="utf-8", newline="\n") as handle:
            for row in truth:
                handle.write(
                    json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8").decode(
                        "utf-8"
                    )
                    + "\n"
                )
        text = "-".join(PHONEMES[value] for value in schedule[:6])
        start_ms = starts[index % len(starts)]
        manifest_rows.append(
            {
                "utterance_id": utterance_id,
                "speaker_id": source_speaker,
                "ref_id": ref_id,
                "start_ms": start_ms,
                "end_ms": start_ms + duration_ms,
                "text": text,
                "seed": seed,
                "ref_revision": baseline_revision,
                "precision_profile": "fp32",
            }
        )

    with open(target / "manifest.jsonl", "w", encoding="utf-8", newline="\n") as handle:
        for row in manifest_rows:
            handle.write(
                json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                + "\n"
            )
    write_json_atomic(target / "refs.json", {"refs": refs_meta})
    write_json_atomic(target / "config.json", config.to_dict())
    write_json_atomic(
        target / "dataset_manifest.json",
        {
            "generator": "spkvc.dataset.generate_dataset",
            "sample_rate": sample_rate,
            "frame_ms": frame_ms,
            "hop_ms": hop_ms,
            "speaker_count": speaker_count,
            "revisions": list(revisions),
            "utterance_count": len(manifest_rows),
            "manifest_sha256": sha256_bytes(
                (target / "manifest.jsonl").read_bytes()
            ),
        },
    )
    return Dataset(target)


def pcm_hash(pcm: np.ndarray) -> str:
    return sha256_bytes(pcm.astype("<i2", copy=False).tobytes())


def pcm_to_wav_bytes(pcm: np.ndarray, sample_rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.astype("<i2", copy=False).tobytes())
    return buffer.getvalue()


def stable_entry_signature(entries: list[ManifestEntry]) -> str:
    return sha256_bytes(
        canonical_bytes([entry.to_dict() for entry in entries])
    )

