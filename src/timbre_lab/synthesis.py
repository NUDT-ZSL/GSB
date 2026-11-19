"""Deterministic local PCM sample generation and loading."""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import FrameConfig
from .deterministic import (
    SplitMix64,
    atomic_write_bytes,
    canonical_json_text,
    derive_seed,
    read_jsonl,
    sha256_bytes,
    write_jsonl,
)
from .errors import DomainError, E_CONFIG_INVALID, E_SAMPLE_RATE_MISMATCH


@dataclass(frozen=True)
class PhonemeProfile:
    phoneme_id: int
    label: str
    f0_factor: float
    formant_shift: float
    energy_factor: float
    noise_level: float
    voiced: bool


PHONEME_PROFILES: tuple[PhonemeProfile, ...] = (
    PhonemeProfile(0, "a", 1.00, 0.91, 1.00, 0.020, True),
    PhonemeProfile(1, "e", 1.06, 1.04, 0.92, 0.018, True),
    PhonemeProfile(2, "i", 1.11, 1.19, 0.86, 0.014, True),
    PhonemeProfile(3, "o", 0.94, 0.82, 0.97, 0.020, True),
    PhonemeProfile(4, "u", 0.89, 0.74, 0.82, 0.016, True),
    PhonemeProfile(5, "m", 0.97, 0.88, 0.68, 0.035, True),
    PhonemeProfile(6, "s", 1.00, 1.33, 0.43, 0.260, False),
    PhonemeProfile(7, "t", 1.00, 1.42, 0.50, 0.220, False),
)


@dataclass(frozen=True)
class SpeakerSpec:
    speaker_id: int
    ref_id: str
    f0_base: float
    formants: tuple[float, float, float]
    bandwidths: tuple[float, float, float]
    energy_base: float
    spectral_tilt: float
    vibrato_phase: float


@dataclass(frozen=True)
class TruthFrame:
    frame_index: int
    speaker_id: int
    f0_true: float
    energy_true: float
    phoneme_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "speaker_id": self.speaker_id,
            "f0_true": self.f0_true,
            "energy_true": self.energy_true,
            "phoneme_id": self.phoneme_id,
        }


@dataclass
class ReferenceSample:
    ref_id: str
    speaker_id: int
    seed: int
    pcm: np.ndarray
    path: Path
    frame_count: int


@dataclass
class UtteranceSample:
    utterance_id: str
    speaker_id: int
    ref_id: str
    seed: int
    pos_scale: float
    pcm: np.ndarray
    truth: tuple[TruthFrame, ...]
    path: Path
    truth_path: Path
    frame_count: int

    @property
    def num_samples(self) -> int:
        return int(self.pcm.shape[0])


@dataclass
class Dataset:
    root: Path
    config: FrameConfig
    seed: int
    speakers: dict[int, SpeakerSpec]
    references: dict[str, ReferenceSample]
    utterances: list[UtteranceSample]
    manifest_entries: list[dict[str, Any]] = field(default_factory=list)
    reference_entries: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: Path | None = None
    references_path: Path | None = None

    def utterance_map(self) -> dict[str, UtteranceSample]:
        return {utterance.utterance_id: utterance for utterance in self.utterances}

    def reference_map(self) -> dict[str, ReferenceSample]:
        return dict(self.references)


def utterance_frame_count_from_seed(seed: int) -> int:
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)):
        raise TypeError("seed must be an integer")
    rng = SplitMix64(derive_seed("utterance-shape-v1", int(seed)))
    return 24 + rng.randbelow(16)


def reference_frame_count() -> int:
    return 48


def _speaker_spec(
    seed: int,
    speaker_id: int,
    num_speakers: int,
    num_phonemes: int,
) -> SpeakerSpec:
    rng = SplitMix64(derive_seed("speaker-v1", seed, speaker_id))
    f0_min = 88.0
    f0_max = 225.0
    spacing = (f0_max - f0_min) / max(num_speakers, 1)
    center = f0_min + spacing * (speaker_id + 0.5)
    f0_base = center + rng.uniform_range(-0.14 * spacing, 0.14 * spacing)
    f1 = rng.uniform_range(470.0, 760.0)
    f2 = rng.uniform_range(1120.0, 1880.0)
    f3 = rng.uniform_range(2350.0, 3150.0)
    return SpeakerSpec(
        speaker_id=speaker_id,
        ref_id=f"ref_s{speaker_id:02d}",
        f0_base=f0_base,
        formants=(f1, f2, f3),
        bandwidths=(
            rng.uniform_range(70.0, 135.0),
            rng.uniform_range(110.0, 210.0),
            rng.uniform_range(170.0, 310.0),
        ),
        energy_base=rng.uniform_range(0.095, 0.175),
        spectral_tilt=rng.uniform_range(0.78, 1.18),
        vibrato_phase=rng.uniform_range(0.0, 2.0 * math.pi),
    )


def _phoneme_timeline(seed: int, frame_count: int, num_phonemes: int) -> list[int]:
    rng = SplitMix64(derive_seed("timeline-v1", seed))
    base, remainder = divmod(frame_count, num_phonemes)
    counts = [base] * num_phonemes
    order = list(range(num_phonemes))
    for index in range(len(order) - 1, 0, -1):
        swap_index = rng.randbelow(index + 1)
        order[index], order[swap_index] = order[swap_index], order[index]
    for phoneme_id in order[:remainder]:
        counts[phoneme_id] += 1
    labels: list[int] = []
    for phoneme_id, count in enumerate(counts):
        labels.extend([phoneme_id] * count)
    # A deterministic shuffle retains exact per-phoneme coverage while making
    # the timeline seed-specific.
    for index in range(len(labels) - 1, 0, -1):
        swap_index = rng.randbelow(index + 1)
        labels[index], labels[swap_index] = labels[swap_index], labels[index]
    return labels


def _harmonic_amplitudes(
    speaker: SpeakerSpec,
    f0: float,
    profile: PhonemeProfile,
    sample_rate: int,
) -> np.ndarray:
    amplitudes = np.zeros(7, dtype=np.float64)
    shifted_formants = tuple(value * profile.formant_shift for value in speaker.formants)
    for harmonic in range(1, 7):
        frequency = harmonic * f0
        if frequency >= sample_rate / 2:
            break
        resonance = 1.0
        for formant, bandwidth in zip(shifted_formants, speaker.bandwidths):
            resonance += 1.65 * math.exp(-0.5 * ((frequency - formant) / bandwidth) ** 2)
        amplitudes[harmonic] = resonance / (harmonic ** speaker.spectral_tilt)
    return amplitudes


def _synthesize_clip(
    speaker: SpeakerSpec,
    phoneme_by_frame: list[int],
    seed: int,
    config: FrameConfig,
    *,
    pos_scale: float,
) -> tuple[np.ndarray, list[float], list[float]]:
    config.validate()
    frame_count = len(phoneme_by_frame)
    if frame_count <= 0:
        raise ValueError("at least one frame is required")
    frame_length = config.frame_samples
    hop_length = config.hop_samples
    sample_rate = config.sample_rate
    sample_count = config.sample_count_for_frames(frame_count)
    rng = SplitMix64(derive_seed("pcm-v1", seed, speaker.speaker_id, pos_scale))
    phase = rng.uniform_range(0.0, 2.0 * math.pi)
    noise = np.empty(sample_count, dtype=np.float64)
    for index in range(sample_count):
        noise[index] = rng.gaussian()

    amplitudes_by_frame: list[np.ndarray] = []
    f0_true: list[float] = []
    envelope_by_frame: list[float] = []
    for frame_index, phoneme_id in enumerate(phoneme_by_frame):
        if phoneme_id < 0 or phoneme_id >= len(PHONEME_PROFILES):
            raise ValueError(f"phoneme_id {phoneme_id} has no synthesis profile")
        profile = PHONEME_PROFILES[phoneme_id]
        center_time = (frame_index * hop_length + frame_length / 2.0) / sample_rate
        vibrato = 1.0 + 0.014 * math.sin(
            2.0 * math.pi * 3.1 * pos_scale * center_time + speaker.vibrato_phase
        )
        f0 = speaker.f0_base * profile.f0_factor * vibrato
        f0_true.append(f0)
        amplitudes_by_frame.append(_harmonic_amplitudes(speaker, f0, profile, sample_rate))
        envelope_by_frame.append(
            speaker.energy_base
            * profile.energy_factor
            * (0.91 + 0.09 * math.sin(2.0 * math.pi * 1.7 * center_time + 0.4))
        )

    signal = np.zeros(sample_count, dtype=np.float64)
    phase_value = phase
    for sample_index in range(sample_count):
        frame_index = min(sample_index // hop_length, frame_count - 1)
        phoneme_id = phoneme_by_frame[frame_index]
        profile = PHONEME_PROFILES[phoneme_id]
        center_time = (frame_index * hop_length + frame_length / 2.0) / sample_rate
        vibrato = 1.0 + 0.014 * math.sin(
            2.0 * math.pi * 3.1 * pos_scale * center_time + speaker.vibrato_phase
        )
        f0 = speaker.f0_base * profile.f0_factor * vibrato
        phase_value += 2.0 * math.pi * f0 / sample_rate
        if phase_value > 2.0 * math.pi * 1024:
            phase_value -= 2.0 * math.pi * 1024
        harmonic_sum = 0.0
        for harmonic in range(1, 7):
            amplitude = amplitudes_by_frame[frame_index][harmonic]
            if amplitude == 0.0:
                break
            harmonic_sum += amplitude * math.sin(harmonic * phase_value)
        normalization = 1.0 + speaker.spectral_tilt
        voiced = harmonic_sum / normalization
        unvoiced = noise[sample_index] * profile.noise_level
        blend = 1.0 if profile.voiced else 0.28
        signal[sample_index] = envelope_by_frame[frame_index] * (
            blend * voiced + unvoiced
        )

    peak = float(np.max(np.abs(signal))) if sample_count else 0.0
    if peak > 0.90:
        signal = signal * (0.90 / peak)
    pcm = np.clip(np.rint(signal * 32767.0), -32768, 32767).astype(np.int16)
    return pcm, f0_true, envelope_by_frame


def _truth_for_clip(
    speaker_id: int,
    phoneme_by_frame: list[int],
    pcm: np.ndarray,
    f0_values: list[float],
    config: FrameConfig,
) -> tuple[TruthFrame, ...]:
    frame_length = config.frame_samples
    hop_length = config.hop_samples
    signal = pcm.astype(np.float64) / 32768.0
    truth: list[TruthFrame] = []
    for frame_index, phoneme_id in enumerate(phoneme_by_frame):
        start = frame_index * hop_length
        frame = signal[start : start + frame_length]
        energy_true = float(np.mean(frame * frame))
        truth.append(
            TruthFrame(
                frame_index=frame_index,
                speaker_id=speaker_id,
                f0_true=float(f0_values[frame_index]),
                energy_true=energy_true,
                phoneme_id=phoneme_id,
            )
        )
    return tuple(truth)


def write_wav_pcm16(path: Path, pcm: np.ndarray, sample_rate: int) -> None:
    if pcm.ndim != 1:
        raise ValueError("PCM must be one-dimensional")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.astype("<i2", copy=False).tobytes())


def read_wav_pcm16(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono PCM")
        if handle.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM")
        sample_rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if sample_rate != 16000:
        raise DomainError(
            E_SAMPLE_RATE_MISMATCH,
            f"{path}: expected 16000 Hz, found {sample_rate} Hz",
            field_name="sample_rate",
            details={"expected": 16000, "actual": sample_rate, "path": str(path)},
        )
    return np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True)


def pcm_bytes(pcm: np.ndarray) -> bytes:
    return pcm.astype("<i2", copy=False).tobytes()


def generate_dataset(
    root: Path | str,
    *,
    seed: int = 20261120,
    num_speakers: int = 3,
    utterances_per_speaker: int = 2,
    config: FrameConfig | None = None,
    pos_scale: float = 1.0,
    baseline_precision_tier: str = "float64",
) -> Dataset:
    """Generate and persist a complete deterministic local sample bundle."""

    frame_config = config or FrameConfig()
    frame_config.validate()
    if isinstance(num_speakers, bool) or not isinstance(num_speakers, int) or num_speakers <= 0:
        raise DomainError(
            E_CONFIG_INVALID,
            "num_speakers must be a positive integer",
            field_name="num_speakers",
        )
    if (
        isinstance(utterances_per_speaker, bool)
        or not isinstance(utterances_per_speaker, int)
        or utterances_per_speaker <= 0
    ):
        raise DomainError(
            E_CONFIG_INVALID,
            "utterances_per_speaker must be a positive integer",
            field_name="utterances_per_speaker",
        )
    if not (math.isfinite(pos_scale) and pos_scale > 0):
        raise DomainError(
            E_CONFIG_INVALID,
            "pos_scale must be finite and strictly positive",
            field_name="pos_scale",
        )

    root_path = Path(root)
    (root_path / "audio").mkdir(parents=True, exist_ok=True)
    (root_path / "refs").mkdir(parents=True, exist_ok=True)
    (root_path / "truth").mkdir(parents=True, exist_ok=True)

    speakers: dict[int, SpeakerSpec] = {}
    references: dict[str, ReferenceSample] = {}
    reference_entries: list[dict[str, Any]] = []
    reference_pattern = [
        index % len(PHONEME_PROFILES) for index in range(reference_frame_count())
    ]
    for speaker_id in range(num_speakers):
        speaker = _speaker_spec(
            seed,
            speaker_id,
            num_speakers,
            frame_config.num_phonemes,
        )
        speakers[speaker_id] = speaker
        ref_seed = int(derive_seed("reference-seed-v1", seed, speaker_id) % (2**63 - 1))
        ref_pcm, _, _ = _synthesize_clip(
            speaker,
            reference_pattern,
            ref_seed,
            frame_config,
            pos_scale=1.0,
        )
        ref_path = root_path / "refs" / f"{speaker.ref_id}.wav"
        write_wav_pcm16(ref_path, ref_pcm, frame_config.sample_rate)
        reference = ReferenceSample(
            ref_id=speaker.ref_id,
            speaker_id=speaker_id,
            seed=ref_seed,
            pcm=ref_pcm,
            path=ref_path,
            frame_count=reference_frame_count(),
        )
        references[speaker.ref_id] = reference
        reference_entries.append(
            {
                "ref_id": reference.ref_id,
                "speaker_id": speaker.speaker_id,
                "sample_rate": frame_config.sample_rate,
                "frame_ms": frame_config.frame_ms,
                "frame_count": reference.frame_count,
                "seed": ref_seed,
                "pcm_file": f"refs/{reference.path.name}",
                "pcm_sha256": sha256_bytes(pcm_bytes(ref_pcm)),
            }
        )

    utterances: list[UtteranceSample] = []
    manifest_entries: list[dict[str, Any]] = []
    for speaker_id in range(num_speakers):
        speaker = speakers[speaker_id]
        for local_index in range(utterances_per_speaker):
            utterance_seed = int(
                derive_seed("utterance-seed-v1", seed, speaker_id, local_index)
                % (2**63 - 1)
            )
            frame_count = utterance_frame_count_from_seed(utterance_seed)
            phoneme_by_frame = _phoneme_timeline(
                utterance_seed,
                frame_count,
                frame_config.num_phonemes,
            )
            pcm, f0_values, _ = _synthesize_clip(
                speaker,
                phoneme_by_frame,
                utterance_seed,
                frame_config,
                pos_scale=pos_scale,
            )
            truth = _truth_for_clip(speaker_id, phoneme_by_frame, pcm, f0_values, frame_config)
            utterance_id = f"utt_s{speaker_id:02d}_{local_index:03d}"
            audio_path = root_path / "audio" / f"{utterance_id}.wav"
            truth_path = root_path / "truth" / f"{utterance_id}.truth.jsonl"
            write_wav_pcm16(audio_path, pcm, frame_config.sample_rate)
            write_jsonl(truth_path, (frame.to_dict() for frame in truth))
            utterance = UtteranceSample(
                utterance_id=utterance_id,
                speaker_id=speaker_id,
                ref_id=speaker.ref_id,
                seed=utterance_seed,
                pos_scale=pos_scale,
                pcm=pcm,
                truth=truth,
                path=audio_path,
                truth_path=truth_path,
                frame_count=frame_count,
            )
            utterances.append(utterance)
            manifest_entries.append(
                {
                    "utterance_id": utterance_id,
                    "speaker_id": speaker_id,
                    "ref_id": speaker.ref_id,
                    "sample_rate": frame_config.sample_rate,
                    "frame_ms": frame_config.frame_ms,
                    "pos_scale": pos_scale,
                    "precision_tier": baseline_precision_tier,
                    "seed": utterance_seed,
                    "hop_ms": frame_config.hop_ms,
                    "num_frames": frame_count,
                    "num_samples": int(pcm.shape[0]),
                    "pcm_file": f"audio/{audio_path.name}",
                    "truth_file": f"truth/{truth_path.name}",
                    "pcm_sha256": sha256_bytes(pcm_bytes(pcm)),
                }
            )

    manifest_path = root_path / "manifest.jsonl"
    references_path = root_path / "references.jsonl"
    write_jsonl(manifest_path, manifest_entries)
    write_jsonl(references_path, reference_entries)
    metadata = {
        "format_version": 1,
        "seed": seed,
        "num_speakers": num_speakers,
        "utterances_per_speaker": utterances_per_speaker,
        "frame": frame_config.to_dict(),
        "pos_scale": pos_scale,
        "reference_frame_count": reference_frame_count(),
        "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
        "references_sha256": sha256_bytes(references_path.read_bytes()),
    }
    atomic_write_bytes(
        root_path / "dataset.json",
        (canonical_json_text(metadata, indent=2) + "\n").encode("utf-8"),
    )
    return Dataset(
        root=root_path,
        config=frame_config,
        seed=seed,
        speakers=speakers,
        references=references,
        utterances=utterances,
        manifest_entries=manifest_entries,
        reference_entries=reference_entries,
        manifest_path=manifest_path,
        references_path=references_path,
    )


def read_truth(path: Path) -> tuple[TruthFrame, ...]:
    rows = read_jsonl(path)
    truth: list[TruthFrame] = []
    for row in rows:
        truth.append(
            TruthFrame(
                frame_index=int(row["frame_index"]),
                speaker_id=int(row["speaker_id"]),
                f0_true=float(row["f0_true"]),
                energy_true=float(row["energy_true"]),
                phoneme_id=int(row["phoneme_id"]),
            )
        )
    return tuple(truth)


def load_dataset(root: Path | str, config: FrameConfig | None = None) -> Dataset:
    root_path = Path(root)
    frame_config = config or FrameConfig()
    frame_config.validate()
    manifest_path = root_path / "manifest.jsonl"
    references_path = root_path / "references.jsonl"
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    if not references_path.exists():
        raise FileNotFoundError(references_path)
    manifest_entries = read_jsonl(manifest_path)
    reference_entries = read_jsonl(references_path)
    references: dict[str, ReferenceSample] = {}
    for row in reference_entries:
        ref_id = str(row["ref_id"])
        path = root_path / str(row["pcm_file"])
        pcm = read_wav_pcm16(path)
        references[ref_id] = ReferenceSample(
            ref_id=ref_id,
            speaker_id=int(row["speaker_id"]),
            seed=int(row["seed"]),
            pcm=pcm,
            path=path,
            frame_count=int(row.get("frame_count", reference_frame_count())),
        )
    utterances: list[UtteranceSample] = []
    for row in manifest_entries:
        utterance_id = str(row["utterance_id"])
        audio_path = root_path / str(row["pcm_file"])
        truth_path = root_path / str(row["truth_file"])
        pcm = read_wav_pcm16(audio_path)
        truth = read_truth(truth_path)
        utterances.append(
            UtteranceSample(
                utterance_id=utterance_id,
                speaker_id=int(row["speaker_id"]),
                ref_id=str(row["ref_id"]),
                seed=int(row["seed"]),
                pos_scale=float(row["pos_scale"]),
                pcm=pcm,
                truth=truth,
                path=audio_path,
                truth_path=truth_path,
                frame_count=int(row.get("num_frames", len(truth))),
            )
        )
    metadata: dict[str, Any] = {}
    metadata_path = root_path / "dataset.json"
    if metadata_path.exists():
        import json

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    seed = int(metadata.get("seed", 0))
    speakers: dict[int, SpeakerSpec] = {}
    for reference in references.values():
        if reference.speaker_id not in speakers:
            speakers[reference.speaker_id] = SpeakerSpec(
                speaker_id=reference.speaker_id,
                ref_id=reference.ref_id,
                f0_base=0.0,
                formants=(0.0, 0.0, 0.0),
                bandwidths=(0.0, 0.0, 0.0),
                energy_base=0.0,
                spectral_tilt=0.0,
                vibrato_phase=0.0,
            )
    return Dataset(
        root=root_path,
        config=frame_config,
        seed=seed,
        speakers=speakers,
        references=references,
        utterances=utterances,
        manifest_entries=manifest_entries,
        reference_entries=reference_entries,
        manifest_path=manifest_path,
        references_path=references_path,
    )


def manifest_rows(dataset: Dataset) -> Iterable[dict[str, Any]]:
    return dataset.manifest_entries
