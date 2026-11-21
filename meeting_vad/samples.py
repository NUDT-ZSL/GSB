"""Sample manifest and annotation manifest loading/validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import fail
from .models import Annotation, Clip
from .util import canonical_json_bytes, is_finite_number, sha256_bytes, write_bytes_atomic


def _read_jsonl(path: Path, kind: str) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(f"E_{kind.upper()}_MISSING", f"{kind} manifest does not exist", path=str(path))
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            fail(
                f"E_{kind.upper()}_INVALID",
                f"invalid JSONL line in {kind} manifest",
                path=str(path),
                line=line_number,
                error=str(exc),
            )
        if not isinstance(row, dict):
            fail(
                f"E_{kind.upper()}_INVALID",
                f"{kind} manifest row must be an object",
                path=str(path),
                line=line_number,
            )
        rows.append(row)
    return rows


def _required(row: dict[str, Any], key: str, code: str, kind: str) -> Any:
    if key not in row:
        fail(code, f"missing required field {key}", kind=kind, field=key, row=row)
    return row[key]


def _manifest_path(value: str, manifest_dir: Path) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else (manifest_dir / path).resolve())


def load_clip_manifest(path: str | Path, sample_rate: int) -> list[Clip]:
    manifest_path = Path(path).resolve()
    rows = _read_jsonl(manifest_path, "sample")
    clips: list[Clip] = []
    seen: set[str] = set()
    for row in rows:
        clip_id = _required(row, "clip_id", "E_MANIFEST_INVALID", "sample")
        if not isinstance(clip_id, str) or not clip_id:
            fail("E_MANIFEST_INVALID", "clip_id must be a non-empty string", row=row)
        if clip_id in seen:
            fail("E_DUPLICATE_CLIP", "duplicate clip_id", clip_id=clip_id)
        seen.add(clip_id)

        row_sample_rate = _required(row, "sample_rate", "E_MANIFEST_INVALID", "sample")
        if row_sample_rate != sample_rate or row_sample_rate != 16_000:
            fail(
                "E_SAMPLE_RATE",
                "clip sample rate must be 16000",
                clip_id=clip_id,
                expected=sample_rate,
                actual=row_sample_rate,
            )
        duration_ms = _required(row, "duration_ms", "E_MANIFEST_INVALID", "sample")
        if not is_finite_number(duration_ms) or duration_ms <= 0:
            fail("E_MANIFEST_INVALID", "duration_ms must be positive", clip_id=clip_id)
        seed = _required(row, "seed", "E_MANIFEST_INVALID", "sample")
        if isinstance(seed, bool) or not isinstance(seed, int):
            fail("E_MANIFEST_INVALID", "seed must be an integer", clip_id=clip_id)
        frame_ms = _required(row, "frame_ms", "E_MANIFEST_INVALID", "sample")
        if not is_finite_number(frame_ms) or frame_ms <= 0:
            fail("E_MANIFEST_INVALID", "frame_ms must be positive", clip_id=clip_id)
        audio_pcm = _required(row, "audio_pcm", "E_MANIFEST_INVALID", "sample")
        channels = row.get("channels", 1)
        sample_width = row.get("sample_width", 2)
        expected_samples = int(round(float(duration_ms) * row_sample_rate / 1000.0))
        clips.append(
            Clip(
                clip_id=clip_id,
                sample_rate=int(row_sample_rate),
                duration_ms=float(duration_ms),
                seed=seed,
                frame_ms=float(frame_ms),
                audio_pcm=_manifest_path(str(audio_pcm), manifest_path.parent),
                audio_wav=(
                    _manifest_path(str(row["audio_wav"]), manifest_path.parent)
                    if row.get("audio_wav")
                    else None
                ),
                channels=int(channels),
                sample_width=int(sample_width),
                expected_samples=expected_samples,
                pcm_sha256=str(row["pcm_sha256"]) if row.get("pcm_sha256") else None,
                raw=row,
            )
        )
    if not clips:
        fail("E_MANIFEST_INVALID", "sample manifest is empty", path=str(manifest_path))
    return clips


def _parse_annotation(row: dict[str, Any]) -> Annotation:
    if "segment_id" not in row:
        fail(
            "E_SEGMENT_ID_MISSING",
            "annotation is missing segment_id",
            clip_id=row.get("clip_id"),
            row=row,
        )
    try:
        clip_id = str(row["clip_id"])
        segment_id = str(row["segment_id"])
        start_ms = float(row["start_ms"])
        end_ms = float(row["end_ms"])
        speaker_label = str(row["speaker_label"])
        label_revision = int(row["label_revision"])
        confidence = float(row["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        fail(
            "E_ANNOTATION_INVALID",
            "annotation has invalid or missing fields",
            row=row,
            error=str(exc),
        )
    if not clip_id or not segment_id:
        fail("E_ANNOTATION_INVALID", "clip_id and segment_id must be non-empty", row=row)
    if not is_finite_number(start_ms) or not is_finite_number(end_ms) or end_ms <= start_ms:
        fail(
            "E_INTERVAL_INVALID",
            "annotation interval requires start_ms < end_ms",
            segment_id=segment_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    if label_revision < 0:
        fail(
            "E_ANNOTATION_INVALID",
            "label_revision cannot be negative",
            segment_id=segment_id,
            label_revision=label_revision,
        )
    if not 0.0 <= confidence <= 1.0:
        fail(
            "E_INTERVAL_INVALID",
            "annotation confidence must be in [0, 1]",
            segment_id=segment_id,
            confidence=confidence,
        )
    lower = row.get("confidence_lower")
    upper = row.get("confidence_upper")
    if (lower is None) != (upper is None):
        fail(
            "E_INTERVAL_INVALID",
            "confidence_lower and confidence_upper must be supplied together",
            segment_id=segment_id,
        )
    if lower is not None:
        lower = float(lower)
        upper = float(upper)
        if (
            not is_finite_number(lower)
            or not is_finite_number(upper)
            or lower > upper
            or lower < 0.0
            or upper > 1.0
        ):
            fail(
                "E_INTERVAL_INVALID",
                "annotation confidence interval is invalid",
                segment_id=segment_id,
                confidence_lower=lower,
                confidence_upper=upper,
            )
    return Annotation(
        clip_id=clip_id,
        segment_id=segment_id,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_label=speaker_label,
        label_revision=label_revision,
        confidence=confidence,
        confidence_lower=float(lower) if lower is not None else None,
        confidence_upper=float(upper) if upper is not None else None,
    )


def load_annotations(path: str | Path, clips: Iterable[Clip]) -> list[Annotation]:
    annotation_path = Path(path).resolve()
    rows = _read_jsonl(annotation_path, "annotation")
    clip_by_id = {clip.clip_id: clip for clip in clips}
    annotations: list[Annotation] = []
    seen_segments: set[str] = set()
    for row in rows:
        annotation = _parse_annotation(row)
        if annotation.clip_id not in clip_by_id:
            fail(
                "E_CLIP_UNKNOWN",
                "annotation references an unknown clip",
                clip_id=annotation.clip_id,
                segment_id=annotation.segment_id,
            )
        if annotation.segment_id in seen_segments:
            fail(
                "E_DUPLICATE_SEGMENT",
                "segment_id must be unique across the annotation manifest",
                segment_id=annotation.segment_id,
            )
        seen_segments.add(annotation.segment_id)
        clip = clip_by_id[annotation.clip_id]
        if annotation.start_ms < 0 or annotation.end_ms > clip.duration_ms:
            fail(
                "E_OUT_OF_RANGE",
                "annotation lies outside the clip duration",
                clip_id=annotation.clip_id,
                segment_id=annotation.segment_id,
                start_ms=annotation.start_ms,
                end_ms=annotation.end_ms,
                duration_ms=clip.duration_ms,
            )
        annotations.append(annotation)
    annotations.sort(key=lambda item: (item.clip_id, item.start_ms, item.end_ms, item.segment_id))
    return annotations


def annotation_hash(annotations: Iterable[Annotation]) -> str:
    rows = [
        annotation.to_dict()
        for annotation in sorted(
            annotations, key=lambda item: (item.clip_id, item.segment_id)
        )
    ]
    return sha256_bytes(canonical_json_bytes(rows))


def annotation_revision(annotations: Iterable[Annotation]) -> int:
    return max((annotation.label_revision for annotation in annotations), default=0)


def write_annotations(path: str | Path, annotations: Iterable[Annotation]) -> None:
    target = Path(path)
    rows = sorted(annotations, key=lambda item: (item.clip_id, item.start_ms, item.segment_id))
    data = b"".join(canonical_json_bytes(row.to_dict()) + b"\n" for row in rows)
    write_bytes_atomic(target, data)
