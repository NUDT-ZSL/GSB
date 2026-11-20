from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import PipelineConfig
from .errors import (
    DomainError,
    E_CONFIG_INVALID,
    E_DUP_DIALOG,
    E_REGISTRY_INVALID,
)
from .util import (
    read_jsonl,
    safe_relative_path,
    sha256_file_chunks,
    sha256_value,
)


@dataclass(frozen=True)
class DialogSpec:
    dialog_id: str
    sample_rate: int
    duration_ms: int
    seed: int
    speaker_ids: tuple[str, ...]
    audio_path: Path
    truth_path: Path
    truth_index: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RegistrySample:
    speaker_id: str
    ref_seed: int
    ref_duration_ms: int
    embedding_version: str
    ref_path: Path | None
    raw: dict[str, Any]


@dataclass
class Dataset:
    root: Path
    dialogs: list[DialogSpec]
    registry: list[RegistrySample]
    corrections_path: Path
    registry_path: Path
    corrections: list[dict[str, Any]]
    registry_version: str
    corrections_versions: dict[str, str]

    @property
    def dialog_map(self) -> dict[str, DialogSpec]:
        return {dialog.dialog_id: dialog for dialog in self.dialogs}

    def truth_path_for(self, dialog: DialogSpec, config: PipelineConfig) -> Path:
        for index_row in dialog.truth_index:
            if (
                float(index_row.get("frame_ms", -1)) == float(config.frame_ms)
                and float(index_row.get("frame_shift_ms", -1)) == float(config.frame_shift_ms)
            ):
                return safe_relative_path(self.root, index_row["path"], "truth_index.path")
        return dialog.truth_path


def _require_field(row: dict[str, Any], field: str, code: str, row_number: int) -> Any:
    if field not in row or row[field] is None:
        raise DomainError(
            code,
            f"required field is missing: {field}",
            field=field,
            row=row_number,
        )
    return row[field]


def load_wav_pcm(path: str | Path) -> tuple[np.ndarray, int, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            raw = handle.readframes(frame_count)
    except (wave.Error, OSError) as exc:
        raise DomainError(
            E_CONFIG_INVALID,
            "audio file is not a readable PCM WAV file",
            field="audio_path",
            path=str(path),
        ) from exc
    if channels != 1 or sample_width != 2:
        raise DomainError(
            E_CONFIG_INVALID,
            "audio must be 16-bit mono PCM",
            field="audio_path",
            channels=channels,
            sample_width=sample_width,
        )
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return pcm, sample_rate, frame_count


def load_truth(path: str | Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    for index, row in enumerate(rows):
        for field in ("frame_index", "speaker_id", "valence", "arousal", "is_speech"):
            if field not in row:
                raise DomainError(
                    E_CONFIG_INVALID,
                    "truth row is missing a required field",
                    field=field,
                    path=str(path),
                    row=index,
                )
        if int(row["frame_index"]) != index:
            raise DomainError(
                E_CONFIG_INVALID,
                "truth frame_index must be contiguous and zero-based",
                field="frame_index",
                path=str(path),
                row=index,
                value=row["frame_index"],
            )
    return rows


def load_dataset(
    root: str | Path,
    config: PipelineConfig,
    registry_path: str | Path | None = None,
    corrections_path: str | Path | None = None,
) -> Dataset:
    config.validate()
    root = Path(root).resolve()
    dialog_manifest = root / "dialog.jsonl"
    if not dialog_manifest.exists():
        raise DomainError(
            E_CONFIG_INVALID,
            "dialog manifest is missing",
            field="dialog.jsonl",
            path=str(dialog_manifest),
        )
    raw_dialogs = read_jsonl(dialog_manifest)
    seen: set[str] = set()
    dialogs: list[DialogSpec] = []
    for row_number, row in enumerate(raw_dialogs, start=1):
        dialog_id = str(_require_field(row, "dialog_id", E_CONFIG_INVALID, row_number))
        if dialog_id in seen:
            raise DomainError(
                E_DUP_DIALOG,
                "dialog_id occurs more than once",
                field="dialog_id",
                dialog_id=dialog_id,
                row=row_number,
            )
        seen.add(dialog_id)
        sample_rate = int(_require_field(row, "sample_rate", E_CONFIG_INVALID, row_number))
        if sample_rate != 16000:
            raise DomainError(
                E_CONFIG_INVALID,
                "sample rate must be exactly 16000",
                field="sample_rate",
                dialog_id=dialog_id,
                value=sample_rate,
            )
        duration_ms = int(_require_field(row, "duration_ms", E_CONFIG_INVALID, row_number))
        seed = int(_require_field(row, "seed", E_CONFIG_INVALID, row_number))
        audio_path = safe_relative_path(root, _require_field(row, "audio_path", E_CONFIG_INVALID, row_number), "audio_path")
        truth_path = safe_relative_path(
            root,
            _require_field(row, "truth_path", E_CONFIG_INVALID, row_number),
            "truth_path",
        )
        speaker_ids_value = row.get("speaker_ids")
        if not speaker_ids_value:
            truth_rows = load_truth(truth_path)
            speaker_ids = tuple(sorted({str(item["speaker_id"]) for item in truth_rows}))
        else:
            speaker_ids = tuple(sorted(str(item) for item in speaker_ids_value))
        truth_index = tuple(row.get("truth_index", []))
        dialogs.append(
            DialogSpec(
                dialog_id=dialog_id,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                seed=seed,
                speaker_ids=speaker_ids,
                audio_path=audio_path,
                truth_path=truth_path,
                truth_index=truth_index,
            )
        )
    if not dialogs:
        raise DomainError(E_CONFIG_INVALID, "dialog manifest is empty", field="dialog.jsonl")
    registry_file = Path(registry_path).resolve() if registry_path else root / "registry.jsonl"
    if not registry_file.exists():
        raise DomainError(
            E_REGISTRY_INVALID,
            "registration manifest is missing",
            field="registry.jsonl",
            path=str(registry_file),
        )
    raw_registry = read_jsonl(registry_file)
    registry_samples: list[RegistrySample] = []
    for row_number, row in enumerate(raw_registry, start=1):
        speaker_id = _require_field(row, "speaker_id", E_REGISTRY_INVALID, row_number)
        if not str(speaker_id).strip():
            raise DomainError(
                E_REGISTRY_INVALID,
                "speaker_id must not be empty",
                field="speaker_id",
                row=row_number,
            )
        ref_seed = int(_require_field(row, "ref_seed", E_REGISTRY_INVALID, row_number))
        ref_duration_ms = int(_require_field(row, "ref_duration_ms", E_REGISTRY_INVALID, row_number))
        embedding_version = str(_require_field(row, "embedding_version", E_REGISTRY_INVALID, row_number))
        if ref_duration_ms <= 0:
            raise DomainError(
                E_REGISTRY_INVALID,
                "ref_duration_ms must be positive",
                field="ref_duration_ms",
                row=row_number,
            )
        ref_path_value = row.get("ref_path")
        ref_path: Path | None = None
        if ref_path_value:
            root_candidate = safe_relative_path(root, ref_path_value, "ref_path")
            registry_candidate = safe_relative_path(registry_file.parent, ref_path_value, "ref_path")
            ref_path = root_candidate if root_candidate.exists() else registry_candidate
        registry_samples.append(
            RegistrySample(
                speaker_id=str(speaker_id),
                ref_seed=ref_seed,
                ref_duration_ms=ref_duration_ms,
                embedding_version=embedding_version,
                ref_path=ref_path,
                raw=row,
            )
        )
    registered = {sample.speaker_id for sample in registry_samples}
    for dialog in dialogs:
        for speaker_id in dialog.speaker_ids:
            if speaker_id not in registered:
                raise DomainError(
                    E_REGISTRY_INVALID,
                    "dialog declares a speaker that has no registration sample",
                    field="speaker_id",
                    dialog_id=dialog.dialog_id,
                    speaker_id=speaker_id,
                )
    corrections_file = Path(corrections_path).resolve() if corrections_path else root / "corrections.jsonl"
    corrections = read_jsonl(corrections_file) if corrections_file.exists() else []
    for row_number, correction in enumerate(corrections, start=1):
        for field in ("dialog_id", "start_frame", "end_frame", "valence", "arousal"):
            if field not in correction:
                raise DomainError(
                    E_CONFIG_INVALID,
                    "correction row is missing a required field",
                    field=field,
                    row=row_number,
                )
        if str(correction["dialog_id"]) not in seen:
            raise DomainError(
                E_CONFIG_INVALID,
                "correction refers to an unknown dialog",
                field="dialog_id",
                row=row_number,
                dialog_id=correction["dialog_id"],
            )
        if int(correction["start_frame"]) < 0 or int(correction["end_frame"]) <= int(correction["start_frame"]):
            raise DomainError(
                E_CONFIG_INVALID,
                "correction frame interval is invalid",
                field="start_frame",
                row=row_number,
            )
    registry_version = sha256_value(
        {
            "samples": [sample.raw for sample in registry_samples],
            "count": len(registry_samples),
        }
    )
    correction_versions: dict[str, str] = {}
    for dialog in dialogs:
        relevant = [row for row in corrections if str(row["dialog_id"]) == dialog.dialog_id]
        correction_versions[dialog.dialog_id] = sha256_value(
            {"dialog_id": dialog.dialog_id, "corrections": relevant}
        )
    return Dataset(
        root=root,
        dialogs=dialogs,
        registry=registry_samples,
        corrections_path=corrections_file,
        registry_path=registry_file,
        corrections=corrections,
        registry_version=registry_version,
        corrections_versions=correction_versions,
    )


def corrected_truth(
    truth_rows: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    dialog_id: str,
) -> list[dict[str, Any]]:
    result = [dict(row) for row in truth_rows]
    for correction in corrections:
        if str(correction["dialog_id"]) != dialog_id:
            continue
        start = int(correction["start_frame"])
        end = int(correction["end_frame"])
        if end > len(result):
            raise DomainError(
                E_CONFIG_INVALID,
                "truth correction extends beyond the truth frame count",
                field="end_frame",
                dialog_id=dialog_id,
                start_frame=start,
                end_frame=end,
                frame_count=len(result),
            )
        for frame_index in range(start, end):
            result[frame_index]["valence"] = float(correction["valence"])
            result[frame_index]["arousal"] = float(correction["arousal"])
    return result


def input_versions(
    dataset: Dataset,
    dialog: DialogSpec,
    truth_path: Path,
    correction_version: str,
) -> dict[str, Any]:
    return {
        "audio_content_hash": sha256_file_chunks(dialog.audio_path),
        "truth_content_hash": sha256_file_chunks(truth_path),
        "truth_correction_version": correction_version,
        "registration_version": dataset.registry_version,
        "registry_sample_count": len(dataset.registry),
        "dialog_seed": dialog.seed,
        "sample_rate": dialog.sample_rate,
    }
