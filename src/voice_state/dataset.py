"""Dataset manifests, registry metadata, WAV loading, and truth validation."""

from __future__ import annotations

import hashlib
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import checksum_value, read_jsonl
from .errors import BusinessError, E_CONFIG_INVALID
from .synth import registry_sample_checksum


@dataclass(frozen=True, slots=True)
class DialogRecord:
    dialog_id: str
    sample_rate: int
    duration_ms: int
    seed: int
    speaker_count: int
    frame_ms: float
    audio_path: Path
    truth_path: Path


@dataclass(frozen=True, slots=True)
class DialogTruth:
    frame_index: np.ndarray
    speaker_id: np.ndarray
    valence: np.ndarray
    arousal: np.ndarray
    is_speech: np.ndarray

    def __len__(self) -> int:
        return int(self.frame_index.size)


@dataclass(frozen=True, slots=True)
class Dataset:
    root: Path
    manifest_path: Path
    registry_path: Path
    dialogs: tuple[DialogRecord, ...]
    registry_rows: tuple[dict[str, Any], ...]
    registry_version: int
    registry_checksum: str
    fingerprint: str

    @property
    def speaker_ids(self) -> tuple[int, ...]:
        return tuple(sorted({int(row["speaker_id"]) for row in self.registry_rows}))

    def dialog(self, dialog_id: str) -> DialogRecord:
        for item in self.dialogs:
            if item.dialog_id == dialog_id:
                return item
        raise BusinessError(
            E_CONFIG_INVALID,
            "Unknown dialog_id",
            {"field": "dialog_id", "dialog_id": dialog_id},
        )


def _require_typed(row: dict[str, Any], name: str, path: Path, *, number: type) -> Any:
    if name not in row:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Required field is missing",
            {"field": name, "path": str(path)},
        )
    value = row[name]
    if number is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise BusinessError(
                E_CONFIG_INVALID,
                "Field must be an integer",
                {"field": name, "path": str(path), "actual": value},
            )
    elif number is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BusinessError(
                E_CONFIG_INVALID,
                "Field must be numeric",
                {"field": name, "path": str(path), "actual": value},
            )
        value = float(value)
    elif number is str:
        if not isinstance(value, str) or not value:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Field must be a non-empty string",
                {"field": name, "path": str(path), "actual": value},
            )
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _registry_sort_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(row["registry_version"]),
        int(row["speaker_id"]),
        int(row["clip_seed"]),
        int(row["clip_ms"]),
    )


def load_dataset(root: Path) -> Dataset:
    manifest_path = root / "manifest.jsonl"
    registry_path = root / "registrations.jsonl"
    if not manifest_path.is_file():
        raise BusinessError(
            E_CONFIG_INVALID,
            "Dialog manifest is missing",
            {"field": "manifest", "path": str(manifest_path)},
        )
    if not registry_path.is_file():
        raise BusinessError(
            E_CONFIG_INVALID,
            "Speaker registry is missing",
            {"field": "registrations", "path": str(registry_path)},
        )

    manifest_rows = read_jsonl(manifest_path)
    dialogs: list[DialogRecord] = []
    seen_ids: set[str] = set()
    for row in manifest_rows:
        dialog_id = _require_typed(row, "dialog_id", manifest_path, number=str)
        if dialog_id in seen_ids:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Duplicate dialog_id",
                {"field": "dialog_id", "dialog_id": dialog_id},
            )
        seen_ids.add(dialog_id)
        sample_rate = _require_typed(row, "sample_rate", manifest_path, number=int)
        duration_ms = _require_typed(row, "duration_ms", manifest_path, number=int)
        seed = _require_typed(row, "seed", manifest_path, number=int)
        speaker_count = _require_typed(row, "speaker_count", manifest_path, number=int)
        frame_ms = _require_typed(row, "frame_ms", manifest_path, number=float)
        if sample_rate <= 0 or duration_ms <= 0 or speaker_count <= 0 or frame_ms <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Manifest values must be positive",
                {"field": "manifest", "dialog_id": dialog_id},
            )
        audio_path = root / "audio" / f"{dialog_id}.wav"
        truth_path = root / "truth" / f"{dialog_id}.jsonl"
        if not audio_path.is_file():
            raise BusinessError(
                E_CONFIG_INVALID,
                "Dialog audio file is missing",
                {"field": "audio_path", "dialog_id": dialog_id, "path": str(audio_path)},
            )
        if not truth_path.is_file():
            raise BusinessError(
                E_CONFIG_INVALID,
                "Dialog truth file is missing",
                {"field": "truth_path", "dialog_id": dialog_id, "path": str(truth_path)},
            )
        dialogs.append(
            DialogRecord(
                dialog_id=dialog_id,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                seed=seed,
                speaker_count=speaker_count,
                frame_ms=frame_ms,
                audio_path=audio_path,
                truth_path=truth_path,
            )
        )
    if not dialogs:
        raise BusinessError(E_CONFIG_INVALID, "Manifest contains no dialogs", {"field": "manifest"})

    registry_rows = read_jsonl(registry_path)
    if not registry_rows:
        raise BusinessError(E_CONFIG_INVALID, "Registry contains no samples", {"field": "registrations"})
    normalized_registry_rows: list[dict[str, Any]] = []
    for row in registry_rows:
        _require_typed(row, "speaker_id", registry_path, number=int)
        _require_typed(row, "clip_seed", registry_path, number=int)
        _require_typed(row, "clip_ms", registry_path, number=int)
        _require_typed(row, "registry_version", registry_path, number=int)
        if int(row["speaker_id"]) < 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "speaker_id must be non-negative",
                {"field": "speaker_id", "actual": row["speaker_id"]},
            )
        if int(row["clip_ms"]) <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "clip_ms must be positive",
                {"field": "clip_ms", "actual": row["clip_ms"]},
            )
        if int(row["registry_version"]) <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "registry_version must be positive",
                {"field": "registry_version", "actual": row["registry_version"]},
            )
        expected_sample_checksum = registry_sample_checksum(row)
        stored_sample_checksum = row.get("sample_checksum")
        if stored_sample_checksum is not None and stored_sample_checksum != expected_sample_checksum:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Registry sample checksum mismatch",
                {
                    "field": "sample_checksum",
                    "speaker_id": row["speaker_id"],
                    "expected": expected_sample_checksum,
                    "actual": stored_sample_checksum,
                },
            )
        normalized_registry_rows.append({**row, "sample_checksum": expected_sample_checksum})
    ordered_registry = sorted(normalized_registry_rows, key=_registry_sort_key)
    registry_version = max(int(row["registry_version"]) for row in ordered_registry)
    registry_checksum = checksum_value(ordered_registry)
    ordered_dialogs = sorted(dialogs, key=lambda item: item.dialog_id)

    digest = hashlib.sha256()
    digest.update(b"voice-state-dataset-v1\n")
    manifest_signature = [
        {
            "dialog_id": item.dialog_id,
            "sample_rate": item.sample_rate,
            "duration_ms": item.duration_ms,
            "seed": item.seed,
            "speaker_count": item.speaker_count,
            "frame_ms": item.frame_ms,
        }
        for item in ordered_dialogs
    ]
    digest.update(checksum_value(manifest_signature).encode())
    digest.update(registry_checksum.encode())
    for item in ordered_dialogs:
        digest.update(item.dialog_id.encode())
        digest.update(_hash_file(item.audio_path).encode())
        digest.update(_hash_file(item.truth_path).encode())
    fingerprint = digest.hexdigest()

    return Dataset(
        root=root,
        manifest_path=manifest_path,
        registry_path=registry_path,
        dialogs=tuple(ordered_dialogs),
        registry_rows=tuple(ordered_registry),
        registry_version=registry_version,
        registry_checksum=registry_checksum,
        fingerprint=fingerprint,
    )


def load_audio(record: DialogRecord, expected_sample_rate: int) -> np.ndarray:
    if record.sample_rate != expected_sample_rate:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Dialog manifest sample_rate is invalid",
            {
                "field": "sample_rate",
                "dialog_id": record.dialog_id,
                "expected": expected_sample_rate,
                "actual": record.sample_rate,
            },
        )
    try:
        with wave.open(str(record.audio_path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            frame_rate = handle.getframerate()
            frame_count = handle.getnframes()
            pcm = handle.readframes(frame_count)
    except (wave.Error, OSError) as exc:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Dialog WAV could not be read",
            {"field": "audio", "dialog_id": record.dialog_id},
        ) from exc
    if channels != 1:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Dialog audio must be mono",
            {"field": "audio_channels", "dialog_id": record.dialog_id, "actual": channels},
        )
    if sample_width != 2:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Dialog audio must use 16-bit PCM",
            {"field": "audio_sample_width", "dialog_id": record.dialog_id, "actual": sample_width},
        )
    if frame_rate != expected_sample_rate:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Dialog WAV sample rate does not match 16 kHz",
            {
                "field": "audio_sample_rate",
                "dialog_id": record.dialog_id,
                "actual": frame_rate,
            },
        )
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    return samples


def load_truth(record: DialogRecord) -> DialogTruth:
    rows = read_jsonl(record.truth_path)
    frame_index = np.empty(len(rows), dtype=np.int64)
    speaker_id = np.empty(len(rows), dtype=np.int64)
    valence = np.empty(len(rows), dtype=np.float64)
    arousal = np.empty(len(rows), dtype=np.float64)
    is_speech = np.empty(len(rows), dtype=bool)
    for position, row in enumerate(rows):
        index = _require_typed(row, "frame_index", record.truth_path, number=int)
        if index != position:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Truth frame indexes must be contiguous and zero-based",
                {"field": "frame_index", "dialog_id": record.dialog_id, "actual": index, "expected": position},
            )
        speaker = _require_typed(row, "speaker_id", record.truth_path, number=int)
        value_v = _require_typed(row, "valence", record.truth_path, number=float)
        value_a = _require_typed(row, "arousal", record.truth_path, number=float)
        speech = row.get("is_speech")
        if not isinstance(speech, bool):
            raise BusinessError(
                E_CONFIG_INVALID,
                "is_speech must be a boolean",
                {"field": "is_speech", "dialog_id": record.dialog_id, "frame_index": index},
            )
        if not math.isfinite(value_v) or not math.isfinite(value_a):
            raise BusinessError(
                E_CONFIG_INVALID,
                "Truth emotion values must be finite",
                {"field": "valence/arousal", "dialog_id": record.dialog_id, "frame_index": index},
            )
        frame_index[position] = index
        speaker_id[position] = speaker
        valence[position] = value_v
        arousal[position] = value_a
        is_speech[position] = speech
    return DialogTruth(frame_index, speaker_id, valence, arousal, is_speech)

