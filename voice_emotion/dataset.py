"""Dataset manifests, local fixture generation and enrollment records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .audio import write_wav
from .canonical import hash_value, read_jsonl, write_jsonl
from .errors import (
    E_DUPLICATE_DIALOG,
    E_ENROLLMENT_MISSING,
    E_INPUT_INVALID,
    E_SAMPLE_RATE,
    raise_error,
)
from .synthesis import (
    make_timeline,
    synthesize_dialog,
    synthesize_registration,
    synthesize_truth,
)


@dataclass(frozen=True)
class DialogRecord:
    dialog_id: str
    sample_rate: int
    duration_ms: int
    speaker_ids: tuple[int, ...]
    seed: int
    audio_path: Path
    truth_path: Path

    def to_manifest(self, manifest_dir: Path) -> dict[str, Any]:
        return {
            "dialog_id": self.dialog_id,
            "sample_rate": self.sample_rate,
            "duration_ms": self.duration_ms,
            "speaker_ids": list(self.speaker_ids),
            "seed": self.seed,
            "audio_path": self.audio_path.relative_to(manifest_dir).as_posix(),
            "truth_path": self.truth_path.relative_to(manifest_dir).as_posix(),
        }


@dataclass(frozen=True)
class EnrollmentRecord:
    speaker_id: int
    seed: int
    duration_ms: int
    sample_index: int = 0
    audio_path: Path | None = None

    def to_manifest(self, manifest_dir: Path | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "speaker_id": self.speaker_id,
            "seed": self.seed,
            "duration_ms": self.duration_ms,
            "sample_index": self.sample_index,
        }
        if self.audio_path is not None:
            path = self.audio_path
            if manifest_dir is not None:
                try:
                    path = path.relative_to(manifest_dir)
                except ValueError:
                    pass
            body["audio_path"] = path.as_posix()
        return body


@dataclass(frozen=True)
class Dataset:
    dialogs: tuple[DialogRecord, ...]
    enrollments: tuple[EnrollmentRecord, ...]
    manifest_path: Path
    enrollment_path: Path

    @property
    def root(self) -> Path:
        return self.manifest_path.parent

    @property
    def enrollment_version(self) -> str:
        all_speakers = sorted({record.speaker_id for record in self.enrollments})
        return self.enrollment_version_for(all_speakers)

    def enrollment_version_for(self, speaker_ids: Iterable[int]) -> str:
        selected = {int(item) for item in speaker_ids}
        semantic = [
            {
                "speaker_id": record.speaker_id,
                "seed": record.seed,
                "duration_ms": record.duration_ms,
                "sample_index": record.sample_index,
            }
            for record in self.enrollments
            if int(record.speaker_id) in selected
        ]
        semantic.sort(
            key=lambda item: (
                item["speaker_id"],
                item["seed"],
                item["duration_ms"],
                item["sample_index"],
            )
        )
        return "enr-" + hash_value({"kind": "enrollment", "samples": semantic})[:24]


def _require_int(value: Any, field: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise_error(
            E_INPUT_INVALID,
            f"manifest field {field} must be an integer",
            field=field,
            path=str(path),
        )
    return value


def _require_str(value: Any, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value:
        raise_error(
            E_INPUT_INVALID,
            f"manifest field {field} must be a non-empty string",
            field=field,
            path=str(path),
        )
    return value


def load_dialogs(manifest_path: str | Path) -> tuple[DialogRecord, ...]:
    path = Path(manifest_path).resolve()
    rows = read_jsonl(path)
    seen: set[str] = set()
    dialogs: list[DialogRecord] = []
    for row in rows:
        dialog_id = _require_str(row.get("dialog_id"), "dialog_id", path)
        if dialog_id in seen:
            raise_error(
                E_DUPLICATE_DIALOG,
                "duplicate dialog_id in manifest",
                dialog_id=dialog_id,
                path=str(path),
            )
        seen.add(dialog_id)
        sample_rate = _require_int(row.get("sample_rate"), "sample_rate", path)
        if sample_rate != 16_000:
            raise_error(
                E_SAMPLE_RATE,
                "dialog manifest sample_rate must be 16000",
                dialog_id=dialog_id,
                expected=16_000,
                actual=sample_rate,
            )
        duration_ms = _require_int(row.get("duration_ms"), "duration_ms", path)
        if duration_ms <= 0:
            raise_error(
                E_INPUT_INVALID,
                "duration_ms must be positive",
                dialog_id=dialog_id,
                duration_ms=duration_ms,
            )
        raw_speakers = row.get("speaker_ids")
        if not isinstance(raw_speakers, list) or not raw_speakers:
            raise_error(
                E_INPUT_INVALID,
                "speaker_ids must be a non-empty list",
                dialog_id=dialog_id,
            )
        speaker_ids = tuple(sorted({_require_int(item, "speaker_ids", path) for item in raw_speakers}))
        seed = _require_int(row.get("seed"), "seed", path)
        audio_path = _require_str(row.get("audio_path"), "audio_path", path)
        truth_path = _require_str(row.get("truth_path"), "truth_path", path)
        dialogs.append(
            DialogRecord(
                dialog_id=dialog_id,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                speaker_ids=speaker_ids,
                seed=seed,
                audio_path=(path.parent / audio_path).resolve(),
                truth_path=(path.parent / truth_path).resolve(),
            )
        )
    dialogs.sort(key=lambda item: item.dialog_id)
    return tuple(dialogs)


def load_enrollments(enrollment_path: str | Path) -> tuple[EnrollmentRecord, ...]:
    path = Path(enrollment_path).resolve()
    rows = read_jsonl(path)
    records: list[EnrollmentRecord] = []
    for row in rows:
        if "speaker_id" not in row:
            raise_error(
                E_ENROLLMENT_MISSING,
                "enrollment record is missing speaker_id",
                path=str(path),
            )
        speaker_id = _require_int(row.get("speaker_id"), "speaker_id", path)
        seed = _require_int(row.get("seed"), "seed", path)
        duration_ms = _require_int(row.get("duration_ms"), "duration_ms", path)
        if duration_ms <= 0:
            raise_error(
                E_INPUT_INVALID,
                "enrollment duration_ms must be positive",
                speaker_id=speaker_id,
                duration_ms=duration_ms,
            )
        sample_index = row.get("sample_index", 0)
        sample_index = _require_int(sample_index, "sample_index", path)
        audio_path = row.get("audio_path")
        resolved_audio = None
        if audio_path is not None:
            audio_path = _require_str(audio_path, "audio_path", path)
            resolved_audio = (path.parent / audio_path).resolve()
        records.append(
            EnrollmentRecord(
                speaker_id=speaker_id,
                seed=seed,
                duration_ms=duration_ms,
                sample_index=sample_index,
                audio_path=resolved_audio,
            )
        )
    records.sort(
        key=lambda item: (item.speaker_id, item.sample_index, item.seed, item.duration_ms)
    )
    return tuple(records)


def load_dataset(
    manifest_path: str | Path,
    enrollment_path: str | Path,
) -> Dataset:
    dialogs = load_dialogs(manifest_path)
    enrollments = load_enrollments(enrollment_path)
    return Dataset(
        dialogs=dialogs,
        enrollments=enrollments,
        manifest_path=Path(manifest_path).resolve(),
        enrollment_path=Path(enrollment_path).resolve(),
    )


def assert_enrollment_coverage(
    dialogs: Iterable[DialogRecord],
    enrollments: Iterable[EnrollmentRecord],
) -> None:
    enrolled = {record.speaker_id for record in enrollments}
    for dialog in dialogs:
        missing = sorted(set(dialog.speaker_ids) - enrolled)
        if missing:
            raise_error(
                E_ENROLLMENT_MISSING,
                "dialog speaker is absent from enrollment manifest",
                dialog_id=dialog.dialog_id,
                missing_speaker_ids=missing,
            )


def build_demo_dataset(
    output_dir: str | Path,
    seed: int = 20261121,
    dialog_count: int = 3,
    speaker_ids: Iterable[int] = (1, 2, 3),
    duration_ms: int = 2_400,
    frame_ms: float = 20.0,
    frame_shift_ms: float = 20.0,
    enrollment_duration_ms: int = 900,
) -> Dataset:
    """Create a fully local, deterministic sample corpus."""

    root = Path(output_dir).resolve()
    audio_dir = root / "audio"
    truth_dir = root / "truth"
    enrollment_dir = root / "enrollment"
    for directory in (audio_dir, truth_dir, enrollment_dir):
        directory.mkdir(parents=True, exist_ok=True)
    speakers = tuple(sorted({int(item) for item in speaker_ids}))
    if not speakers:
        raise ValueError("speaker_ids cannot be empty")
    if dialog_count < 1:
        raise ValueError("dialog_count must be positive")

    dialogs: list[DialogRecord] = []
    for index in range(1, dialog_count + 1):
        dialog_id = f"dlg_{index:04d}"
        # All dialogue voices share the enrollment seed; dialogue_id provides
        # independent timeline variation without changing speaker identity.
        dialog_seed = int(seed)
        audio, timeline = synthesize_dialog(
            dialog_id=dialog_id,
            seed=dialog_seed,
            duration_ms=duration_ms,
            speaker_ids=speakers,
        )
        audio_path = audio_dir / f"{dialog_id}.wav"
        truth_path = truth_dir / f"{dialog_id}.truth.jsonl"
        write_wav(audio_path, audio)
        truth = synthesize_truth(
            timeline,
            seed=dialog_seed,
            frame_ms=frame_ms,
            frame_shift_ms=frame_shift_ms,
        )
        write_jsonl(truth_path, truth)
        dialogs.append(
            DialogRecord(
                dialog_id=dialog_id,
                sample_rate=16_000,
                duration_ms=int(duration_ms),
                speaker_ids=speakers,
                seed=dialog_seed,
                audio_path=audio_path,
                truth_path=truth_path,
            )
        )

    enrollments: list[EnrollmentRecord] = []
    for speaker_id in speakers:
        clip = synthesize_registration(
            speaker_id=speaker_id,
            seed=int(seed),
            duration_ms=int(enrollment_duration_ms),
            sample_index=0,
        )
        enrollment_audio = enrollment_dir / f"speaker_{speaker_id}.wav"
        write_wav(enrollment_audio, clip)
        enrollments.append(
            EnrollmentRecord(
                speaker_id=speaker_id,
                seed=int(seed),
                duration_ms=int(enrollment_duration_ms),
                sample_index=0,
                audio_path=enrollment_audio,
            )
        )

    manifest_path = root / "dialogs.jsonl"
    enrollment_path = root / "enrollments.jsonl"
    write_jsonl(manifest_path, [record.to_manifest(root) for record in dialogs])
    write_jsonl(
        enrollment_path,
        [record.to_manifest(root) for record in enrollments],
    )
    return load_dataset(manifest_path, enrollment_path)


def regenerate_truth(
    dialog: DialogRecord,
    frame_ms: float = 20.0,
    frame_shift_ms: float = 20.0,
) -> list[dict[str, Any]]:
    """Regenerate matching truth when a test intentionally changes frame hop."""

    timeline = make_timeline(
        dialog_id=dialog.dialog_id,
        seed=dialog.seed,
        duration_ms=dialog.duration_ms,
        speaker_ids=dialog.speaker_ids,
        sample_rate=dialog.sample_rate,
    )
    return synthesize_truth(
        timeline,
        seed=dialog.seed,
        frame_ms=frame_ms,
        frame_shift_ms=frame_shift_ms,
    )


def write_truth(path: str | Path, rows: list[dict[str, Any]]) -> None:
    write_jsonl(path, rows)


def truth_semantic_hash(rows: Iterable[dict[str, Any]]) -> str:
    normalized = []
    for row in rows:
        normalized.append(
            {
                "frame_index": int(row["frame_index"]),
                "speaker_id": int(row["speaker_id"]),
                "valence": float(row["valence"]),
                "arousal": float(row["arousal"]),
                "is_speech": bool(row["is_speech"]),
            }
        )
    return "truth-" + hash_value({"kind": "truth", "rows": normalized})[:24]


def truth_frame_hash(row: dict[str, Any]) -> str:
    return hash_value(
        {
            "frame_index": int(row["frame_index"]),
            "speaker_id": int(row["speaker_id"]),
            "valence": float(row["valence"]),
            "arousal": float(row["arousal"]),
            "is_speech": bool(row["is_speech"]),
        }
    )


def load_truth(path: str | Path, expected_frame_count: int | None = None) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    for index, row in enumerate(rows):
        for field in ("frame_index", "speaker_id", "valence", "arousal", "is_speech"):
            if field not in row:
                raise_error(
                    E_INPUT_INVALID,
                    "truth row is missing a required field",
                    path=str(path),
                    row=index,
                    field=field,
                )
        try:
            frame_index = int(row["frame_index"])
            speaker_id = int(row["speaker_id"])
            valence = float(row["valence"])
            arousal = float(row["arousal"])
        except (TypeError, ValueError) as exc:
            raise_error(
                E_INPUT_INVALID,
                "truth row contains a non-numeric value",
                path=str(path),
                row=index,
                reason=str(exc),
            )
        if not isinstance(row["is_speech"], bool):
            raise_error(
                E_INPUT_INVALID,
                "truth is_speech must be boolean",
                path=str(path),
                row=index,
            )
        if frame_index != index:
            raise_error(
                E_INPUT_INVALID,
                "truth frame_index must be contiguous and zero-based",
                path=str(path),
                row=index,
                frame_index=frame_index,
            )
        row["frame_index"] = frame_index
        row["speaker_id"] = speaker_id
        row["valence"] = valence
        row["arousal"] = arousal
        if not -1.0 <= valence <= 1.0 or not -1.0 <= arousal <= 1.0:
            raise_error(
                E_INPUT_INVALID,
                "truth valence/arousal must be in [-1, 1]",
                path=str(path),
                frame_index=frame_index,
            )
    if expected_frame_count is not None and len(rows) != expected_frame_count:
        from .errors import E_FRAME_COUNT

        raise_error(
            E_FRAME_COUNT,
            "feature frame count does not match truth frame count",
            expected=expected_frame_count,
            actual=len(rows),
            path=str(path),
        )
    return rows
