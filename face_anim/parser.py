"""Read and normalize heterogeneous project-version annotations."""

from __future__ import annotations

import wave
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

from .catalog import (
    CANONICAL_CHANNELS,
    PHONEME_ALIASES,
    normalize_channel_name,
    normalize_phoneme_label,
    phoneme_reference,
)
from .errors import fail
from .io_utils import read_json
from .models import PhonemeEvent, Sentence, StressMark, VersionData


_STYLE_BY_VERSION = {"a": "arpabet", "b": "ipa", "c": "legacy"}
_KNOWN_TIME_BASES = {
    "ms": "milliseconds",
    "msec": "milliseconds",
    "millisecond": "milliseconds",
    "milliseconds": "milliseconds",
    "frame": "frames",
    "frames": "frames",
    "fps": "frames",
}


def _pick(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _as_float(value: Any, *, path: Path, field_name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        fail(
            "E_INVALID_NUMBER",
            f"{field_name} must be numeric",
            path=str(path),
            value=value,
        )
    if not math.isfinite(numeric):
        fail(
            "E_INVALID_NUMBER",
            f"{field_name} must be finite",
            path=str(path),
            value=value,
        )
    return numeric


def _as_int(value: Any, *, path: Path, field_name: str) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        fail(
            "E_INVALID_NUMBER",
            f"{field_name} must be an integer",
            path=str(path),
            value=value,
        )
    return numeric


def _parse_time_us(
    item: dict[str, Any],
    side: str,
    *,
    time_base: str,
    fps: float | None,
    path: Path,
    version: str,
    sentence_id: str | None,
    phoneme_id: str | None,
) -> int:
    ms_keys = (f"{side}_ms", f"{side}_milliseconds")
    frame_keys = (f"{side}_frame", f"{side}_frames")
    generic = (side, f"{side}_time")
    value = None
    kind = time_base
    for key in ms_keys:
        if key in item:
            value = item[key]
            kind = "milliseconds"
            break
    if value is None:
        for key in frame_keys:
            if key in item:
                value = item[key]
                kind = "frames"
                break
    if value is None:
        for key in generic:
            if key in item:
                value = item[key]
                break
    if value is None:
        fail(
            "E_MISSING_TIME",
            f"missing {side} time",
            path=str(path),
            version=version,
            sentence_id=sentence_id,
            phoneme_id=phoneme_id,
        )
    numeric = _as_float(value, path=path, field_name=f"{side}_time")
    if numeric < 0:
        fail(
            "E_INVALID_TIME",
            "time values must not be negative",
            path=str(path),
            version=version,
            sentence_id=sentence_id,
            phoneme_id=phoneme_id,
            value=numeric,
        )
    if kind == "frames":
        if fps is None or fps <= 0:
            fail(
                "E_SAMPLE_RATE_MISMATCH",
                "frame-based input requires a positive fps",
                path=str(path),
                version=version,
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
                fps=fps,
            )
        return int(round(Fraction(str(numeric)) * 1_000_000 / Fraction(str(fps))))
    return int(round(numeric * 1000.0))


def _extract_audio_info(
    annotation: dict[str, Any],
    project: dict[str, Any],
    version_dir: Path,
    *,
    path: Path,
    version: str,
) -> tuple[Path, int, int]:
    audio_name = _pick(project, "audio_file", "audio", default=None)
    if isinstance(audio_name, dict):
        audio_name = _pick(audio_name, "path", "file")
    if not audio_name:
        audio_name = _pick(annotation, "audio_file", "audio", default="narration.wav")
        if isinstance(audio_name, dict):
            audio_name = _pick(audio_name, "path", "file", default="narration.wav")
    audio_path = (version_dir / str(audio_name)).resolve()
    if not audio_path.exists():
        fail(
            "E_INPUT_MISSING",
            "narration audio does not exist",
            path=str(audio_path),
            version=version,
        )
    try:
        with wave.open(str(audio_path), "rb") as handle:
            actual_rate = int(handle.getframerate())
            frame_count = int(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        fail(
            "E_AUDIO_INVALID",
            f"cannot read WAV audio: {exc}",
            path=str(audio_path),
            version=version,
        )
    declared = _pick(
        project,
        "audio_sample_rate",
        "sample_rate_hz",
        "sample_rate",
        default=_pick(annotation, "audio_sample_rate", "sample_rate_hz", "sample_rate"),
    )
    if declared is not None:
        declared_int = _as_int(
            declared, path=path, field_name="audio_sample_rate"
        )
        if declared_int != actual_rate:
            fail(
                "E_SAMPLE_RATE_MISMATCH",
                f"declared sample rate {declared_int} Hz does not match WAV rate {actual_rate} Hz",
                path=str(audio_path),
                version=version,
                declared=declared_int,
                actual=actual_rate,
            )
    if actual_rate <= 0:
        fail(
            "E_SAMPLE_RATE_MISMATCH",
            "WAV sample rate must be positive",
            path=str(audio_path),
            version=version,
        )
    audio_duration_us = int(round(frame_count * 1_000_000 / actual_rate))
    return audio_path, actual_rate, audio_duration_us


def _parse_channel_bindings(
    bindings: dict[str, Any], *, path: Path, version: str
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    sources: dict[str, str] = {}
    details: dict[str, dict[str, Any]] = {}
    rows = _pick(bindings, "channel_bindings", "bindings", "channels")
    if rows is None:
        fail(
            "E_BINDING",
            "bindings file has no channel_bindings/channels section",
            path=str(path),
            version=version,
        )

    def add(source: Any, role_hint: Any = None, extra: dict[str, Any] | None = None) -> None:
        if not isinstance(source, str) or not source.strip():
            fail(
                "E_BINDING",
                "channel source name must be a non-empty string",
                path=str(path),
                version=version,
                value=source,
            )
        canonical = (
            normalize_channel_name(str(role_hint)) if role_hint is not None else None
        )
        if canonical is None:
            canonical = normalize_channel_name(source)
        if canonical is None:
            fail(
                "E_UNKNOWN_CHANNEL",
                f"unrecognized facial channel {source!r}",
                path=str(path),
                version=version,
                source=source,
            )
        if canonical in sources:
            fail(
                "E_BINDING",
                f"channel role {canonical} is bound more than once",
                path=str(path),
                version=version,
                source=source,
            )
        sources[canonical] = source
        details[canonical] = dict(extra or {})
        details[canonical]["source"] = source

    if isinstance(rows, dict):
        for key, value in rows.items():
            canonical_key = normalize_channel_name(str(key))
            if canonical_key is not None:
                if isinstance(value, str):
                    add(value, canonical_key)
                elif isinstance(value, dict):
                    source = _pick(value, "source", "name", "channel", default=key)
                    add(source, canonical_key, value)
                else:
                    fail(
                        "E_BINDING",
                        "channel binding value must be a string or object",
                        path=str(path),
                        version=version,
                        key=key,
                    )
            else:
                if isinstance(value, str):
                    add(key, value)
                elif isinstance(value, dict):
                    role = _pick(value, "canonical", "role", "target")
                    add(key, role, value)
                else:
                    fail(
                        "E_BINDING",
                        "invalid channel binding entry",
                        path=str(path),
                        version=version,
                        key=key,
                    )
    elif isinstance(rows, list):
        for index, row in enumerate(rows):
            if isinstance(row, str):
                add(row)
            elif isinstance(row, dict):
                source = _pick(row, "source", "name", "channel", "source_channel")
                role = _pick(row, "canonical", "role", "target", "target_channel")
                if source is None:
                    fail(
                        "E_BINDING",
                        "channel binding row has no source name",
                        path=str(path),
                        version=version,
                        index=index,
                    )
                add(source, role, row)
            else:
                fail(
                    "E_BINDING",
                    "channel binding rows must be strings or objects",
                    path=str(path),
                    version=version,
                    index=index,
                )
    else:
        fail(
            "E_BINDING",
            "channel bindings must be an object or list",
            path=str(path),
            version=version,
        )

    missing = [channel for channel in CANONICAL_CHANNELS if channel not in sources]
    if missing:
        fail(
            "E_BINDING",
            "missing required facial channel bindings: " + ", ".join(missing),
            path=str(path),
            version=version,
            missing_channels=missing,
        )
    return sources, details


def _infer_style(version: str, project: dict[str, Any]) -> str:
    style = _pick(project, "label_style", "phoneme_style", "alphabet")
    if style:
        key = str(style).strip().lower()
        if key in PHONEME_ALIASES:
            return key
    version_key = version.strip().lower()
    if version_key in _STYLE_BY_VERSION:
        return _STYLE_BY_VERSION[version_key]
    return "canonical"


def parse_version(version_dir: str | Path) -> VersionData:
    directory = Path(version_dir).expanduser().resolve()
    annotation_path = directory / "annotation.json"
    annotation = read_json(annotation_path)
    if not isinstance(annotation, dict):
        fail(
            "E_JSON_INVALID",
            "annotation root must be an object",
            path=str(annotation_path),
        )
    project = _pick(annotation, "project", "meta", "metadata", default={})
    if not isinstance(project, dict):
        project = {}
    version = str(
        _pick(project, "version", "project_version", default=_pick(annotation, "version", default=directory.name))
    )
    time_base_raw = _pick(
        project, "time_base", "timebase", default=_pick(annotation, "time_base", "timebase")
    )
    if not time_base_raw:
        fail(
            "E_TIME_BASE",
            "missing time_base",
            path=str(annotation_path),
            version=version,
        )
    time_base = _KNOWN_TIME_BASES.get(str(time_base_raw).strip().lower())
    if time_base is None:
        fail(
            "E_TIME_BASE",
            f"unsupported time_base {time_base_raw!r}",
            path=str(annotation_path),
            version=version,
            supported=sorted(set(_KNOWN_TIME_BASES.values())),
        )

    fps_value = _pick(
        project, "fps", "frame_rate", "source_fps", default=_pick(annotation, "fps", "frame_rate")
    )
    source_fps = (
        _as_float(fps_value, path=annotation_path, field_name="fps")
        if fps_value is not None
        else None
    )
    if source_fps is not None and source_fps <= 0:
        fail(
            "E_SAMPLE_RATE_MISMATCH",
            "fps must be positive",
            path=str(annotation_path),
            version=version,
            fps=source_fps,
        )
    if time_base == "frames" and source_fps is None:
        fail(
            "E_SAMPLE_RATE_MISMATCH",
            "frame-based annotation has no fps",
            path=str(annotation_path),
            version=version,
        )

    bindings_name = _pick(
        project,
        "channel_map_file",
        "bindings_file",
        "channel_bindings_file",
        default="bindings.json",
    )
    bindings_path = (directory / str(bindings_name)).resolve()
    bindings = read_json(bindings_path)
    if not isinstance(bindings, dict):
        fail(
            "E_BINDING",
            "bindings root must be an object",
            path=str(bindings_path),
            version=version,
        )
    binding_fps = _pick(bindings, "fps", "frame_rate")
    if (
        binding_fps is not None
        and source_fps is not None
        and abs(float(binding_fps) - source_fps) > 1e-9
    ):
        fail(
            "E_SAMPLE_RATE_MISMATCH",
            "annotation fps and bindings fps disagree",
            path=str(bindings_path),
            version=version,
            annotation_fps=source_fps,
            bindings_fps=float(binding_fps),
        )
    channel_sources, channel_details = _parse_channel_bindings(
        bindings, path=bindings_path, version=version
    )
    label_style = _infer_style(version, project)
    audio_path, sample_rate, audio_duration_us = _extract_audio_info(
        annotation, project, directory, path=annotation_path, version=version
    )

    raw_sentences = _pick(annotation, "sentences", "utterances")
    if not isinstance(raw_sentences, list) or not raw_sentences:
        fail(
            "E_MISSING_PHONEME",
            "annotation contains no sentences/phonemes",
            path=str(annotation_path),
            version=version,
        )

    sentences: list[Sentence] = []
    seen_phoneme_ids: set[str] = set()
    seen_sentence_ids: set[str] = set()
    for sentence_index, raw_sentence in enumerate(raw_sentences, start=1):
        if not isinstance(raw_sentence, dict):
            fail(
                "E_JSON_INVALID",
                "sentence entry must be an object",
                path=str(annotation_path),
                version=version,
                details={"index": sentence_index},
            )
        sentence_id = str(
            _pick(raw_sentence, "id", "sentence_id", default=f"S{sentence_index:02d}")
        )
        if sentence_id in seen_sentence_ids:
            fail(
                "E_DUPLICATE_SENTENCE",
                "sentence id occurs more than once",
                path=str(annotation_path),
                version=version,
                sentence_id=sentence_id,
            )
        seen_sentence_ids.add(sentence_id)
        raw_phonemes = _pick(raw_sentence, "phonemes", "segments", "phones")
        if not isinstance(raw_phonemes, list) or not raw_phonemes:
            fail(
                "E_MISSING_PHONEME",
                "sentence contains no phonemes",
                path=str(annotation_path),
                version=version,
                sentence_id=sentence_id,
            )
        phonemes: list[PhonemeEvent] = []
        for phoneme_index, raw_phoneme in enumerate(raw_phonemes, start=1):
            if not isinstance(raw_phoneme, dict):
                fail(
                    "E_MISSING_PHONEME",
                    "phoneme entry must be an object",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    details={"phoneme_index": phoneme_index},
                )
            phoneme_id = _pick(raw_phoneme, "id", "phoneme_id", "phone_id")
            if phoneme_id is None:
                fail(
                    "E_MISSING_PHONEME",
                    "phoneme has no stable id",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    details={"phoneme_index": phoneme_index},
                )
            phoneme_id = str(phoneme_id)
            if phoneme_id in seen_phoneme_ids:
                fail(
                    "E_DUPLICATE_PHONEME",
                    "phoneme id occurs more than once",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    phoneme_id=phoneme_id,
                )
            seen_phoneme_ids.add(phoneme_id)
            label = _pick(raw_phoneme, "label", "phoneme", "phone", "symbol")
            if label is None or str(label).strip() == "":
                fail(
                    "E_MISSING_PHONEME",
                    "phoneme has no source label",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    phoneme_id=phoneme_id,
                )
            canonical = normalize_phoneme_label(str(label), label_style)
            if canonical is None:
                fail(
                    "E_UNKNOWN_PHONEME",
                    f"unknown phoneme label {label!r} in style {label_style}",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    phoneme_id=phoneme_id,
                    source_label=str(label),
                )
            start_us = _parse_time_us(
                raw_phoneme,
                "start",
                time_base=time_base,
                fps=source_fps,
                path=annotation_path,
                version=version,
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
            )
            end_us = _parse_time_us(
                raw_phoneme,
                "end",
                time_base=time_base,
                fps=source_fps,
                path=annotation_path,
                version=version,
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
            )
            if end_us <= start_us:
                fail(
                    "E_INVALID_INTERVAL",
                    "phoneme end must be later than start",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    phoneme_id=phoneme_id,
                    start_us=start_us,
                    end_us=end_us,
                )
            reference = phoneme_reference(canonical)
            phonemes.append(
                PhonemeEvent(
                    id=phoneme_id,
                    source_label=str(label),
                    canonical=canonical,
                    category=str(reference["category"]),
                    viseme=str(reference["viseme"]),
                    start_us=start_us,
                    end_us=end_us,
                    targets=dict(reference["targets"]),
                )
            )
        phonemes.sort(key=lambda item: (item.start_us, item.end_us, item.id))
        for previous, current in zip(phonemes, phonemes[1:]):
            if current.start_us < previous.end_us:
                fail(
                    "E_TIME_OVERLAP",
                    f"phoneme {current.id} overlaps preceding phoneme {previous.id}",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    phoneme_id=current.id,
                    previous_phoneme_id=previous.id,
                    previous_end_us=previous.end_us,
                    current_start_us=current.start_us,
                )

        sentence_id_value = phonemes[0].start_us
        sentence_end_value = phonemes[-1].end_us
        if any(
            _pick(raw_sentence, key) is not None
            for key in ("start_ms", "start_frame", "start")
        ):
            parsed_start = _parse_time_us(
                raw_sentence,
                "start",
                time_base=time_base,
                fps=source_fps,
                path=annotation_path,
                version=version,
                sentence_id=sentence_id,
                phoneme_id=None,
            )
            if parsed_start <= sentence_id_value:
                sentence_id_value = parsed_start
        if any(
            _pick(raw_sentence, key) is not None
            for key in ("end_ms", "end_frame", "end")
        ):
            parsed_end = _parse_time_us(
                raw_sentence,
                "end",
                time_base=time_base,
                fps=source_fps,
                path=annotation_path,
                version=version,
                sentence_id=sentence_id,
                phoneme_id=None,
            )
            if parsed_end >= sentence_end_value:
                sentence_end_value = parsed_end

        raw_emotion = _pick(raw_sentence, "emotion", "emotion_label", default="neutral")
        emotion_intensity = 0.0
        emotion_anchor_id = _pick(
            raw_sentence, "emotion_anchor_phoneme_id", "emotion_anchor_id"
        )
        if isinstance(raw_emotion, dict):
            emotion_label = str(
                _pick(raw_emotion, "label", "name", "emotion", default="neutral")
            )
            emotion_intensity = _as_float(
                _pick(raw_emotion, "intensity", "level", default=0.0),
                path=annotation_path,
                field_name="emotion.intensity",
            )
            anchor = _pick(raw_emotion, "anchor_phoneme_id", "anchor_id")
            if anchor is not None:
                emotion_anchor_id = anchor
        else:
            emotion_label = str(raw_emotion or "neutral")
            intensity_value = _pick(raw_sentence, "emotion_intensity")
            if intensity_value is not None:
                emotion_intensity = _as_float(
                    intensity_value,
                    path=annotation_path,
                    field_name="emotion_intensity",
                )
        if emotion_anchor_id is not None:
            emotion_anchor_id = str(emotion_anchor_id)
            if not any(item.id == emotion_anchor_id for item in phonemes):
                fail(
                    "E_MISSING_PHONEME",
                    "emotion anchor phoneme is not present in its sentence",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    phoneme_id=emotion_anchor_id,
                )

        raw_stress = _pick(raw_sentence, "stress", "stress_marks", default=[])
        if raw_stress is None:
            raw_stress = []
        if not isinstance(raw_stress, list):
            raw_stress = [raw_stress]
        stress_marks: list[StressMark] = []
        for index, item in enumerate(raw_stress):
            if isinstance(item, str):
                stress_id = item
                level = 1.0
            elif isinstance(item, dict):
                stress_id = _pick(item, "phoneme_id", "id", "phone_id")
                level = _as_float(
                    _pick(item, "level", "strength", "weight", default=1.0),
                    path=annotation_path,
                    field_name="stress.level",
                )
            else:
                fail(
                    "E_MISSING_PHONEME",
                    "stress entry does not identify a phoneme",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    details={"stress_index": index},
                )
            if stress_id is None:
                fail(
                    "E_MISSING_PHONEME",
                    "stress entry has no phoneme id",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    details={"stress_index": index},
                )
            stress_id = str(stress_id)
            if not any(item.id == stress_id for item in phonemes):
                fail(
                    "E_MISSING_PHONEME",
                    "stress phoneme is not present in its sentence",
                    path=str(annotation_path),
                    version=version,
                    sentence_id=sentence_id,
                    phoneme_id=stress_id,
                )
            stress_marks.append(
                StressMark(phoneme_id=stress_id, level=max(0.0, min(1.0, level)))
            )
        sentences.append(
            Sentence(
                id=sentence_id,
                text=str(_pick(raw_sentence, "text", default="")),
                start_us=sentence_id_value,
                end_us=sentence_end_value,
                emotion_label=emotion_label,
                emotion_intensity=max(0.0, min(1.0, emotion_intensity)),
                emotion_anchor_id=emotion_anchor_id,
                stress=stress_marks,
                phonemes=phonemes,
            )
        )

    sentences.sort(key=lambda item: (item.start_us, item.id))
    for previous, current in zip(sentences, sentences[1:]):
        if current.start_us < previous.end_us:
            fail(
                "E_TIME_OVERLAP",
                f"sentence {current.id} overlaps preceding sentence {previous.id}",
                path=str(annotation_path),
                version=version,
                sentence_id=current.id,
                previous_sentence_id=previous.id,
            )
    all_phonemes = [phoneme for sentence in sentences for phoneme in sentence.phonemes]
    all_phonemes.sort(key=lambda item: (item.start_us, item.end_us, item.id))
    for previous, current in zip(all_phonemes, all_phonemes[1:]):
        if current.start_us < previous.end_us:
            fail(
                "E_TIME_OVERLAP",
                f"phoneme {current.id} overlaps preceding phoneme {previous.id}",
                path=str(annotation_path),
                version=version,
                phoneme_id=current.id,
                previous_phoneme_id=previous.id,
                previous_end_us=previous.end_us,
                current_start_us=current.start_us,
            )
    annotation_end_us = max(item.end_us for item in all_phonemes)
    declared_duration_us = None
    duration_ms = _pick(project, "duration_ms")
    if duration_ms is not None:
        declared_duration_us = int(round(float(duration_ms) * 1000.0))
    duration_us = max(annotation_end_us, audio_duration_us, declared_duration_us or 0)
    if duration_us <= 0:
        fail(
            "E_INVALID_INTERVAL",
            "timeline duration must be positive",
            path=str(annotation_path),
            version=version,
        )
    return VersionData(
        version=version,
        label_style=label_style,
        time_base=time_base,
        source_fps=source_fps,
        audio_path=audio_path,
        audio_sample_rate=sample_rate,
        audio_duration_us=audio_duration_us,
        duration_us=duration_us,
        sentences=sentences,
        channel_sources=channel_sources,
        annotation_path=annotation_path,
        bindings_path=bindings_path,
        channel_binding_details=channel_details,
    )


def parse_versions(version_dirs: list[str | Path]) -> list[VersionData]:
    if len(version_dirs) != 3:
        fail(
            "E_VERSION_COUNT",
            "exactly three project versions are required",
            count=len(version_dirs),
        )
    versions = [parse_version(directory) for directory in version_dirs]
    versions.sort(key=lambda item: item.version)
    ids = [item.version for item in versions]
    if len(set(ids)) != len(ids):
        fail("E_VERSION_COUNT", "version identifiers must be unique", versions=ids)
    rates = {item.audio_sample_rate for item in versions}
    if len(rates) != 1:
        fail(
            "E_SAMPLE_RATE_MISMATCH",
            "three project versions use different audio sample rates",
            versions=ids,
            sample_rates=sorted(rates),
        )
    return versions


def version_dirs_from_samples(samples_dir: str | Path) -> list[Path]:
    root = Path(samples_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    mapping = manifest.get("versions") if isinstance(manifest, dict) else None
    if not isinstance(mapping, dict) or not mapping:
        fail(
            "E_INPUT_MISSING",
            "sample manifest has no versions mapping",
            path=str(manifest_path),
        )
    paths = [root / str(mapping[key]) for key in sorted(mapping)]
    for path in paths:
        if not (path / "annotation.json").exists():
            fail(
                "E_INPUT_MISSING",
                "version directory from manifest is incomplete",
                path=str(path),
            )
    return paths


def infer_ground_truth_path(version_dirs: list[str | Path]) -> Path | None:
    paths = [Path(item).expanduser().resolve() for item in version_dirs]
    try:
        common = Path(os_commonpath(paths))
    except ValueError:
        common = None
    if common is not None:
        candidate = common / "ground_truth.json"
        if candidate.exists():
            return candidate
    for path in paths:
        candidate = path / "ground_truth.json"
        if candidate.exists():
            return candidate
    return None


def os_commonpath(paths: list[Path]) -> str:
    import os

    return os.path.commonpath([str(path) for path in paths])
