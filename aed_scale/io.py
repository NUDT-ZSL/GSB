"""Deterministic local I/O for manifests, truth frames, PCM and reports."""

from __future__ import annotations

import hashlib
import json
import math
import wave
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np

from .catalog import EVENT_TYPE_IDS
from .codes import BusinessCode
from .errors import AedError


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def write_json(path: str | Path, payload: Any, *, indent: int | None = 2) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        allow_nan=False,
    )
    target.write_text(text + "\n", encoding="utf-8")


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        for row in rows:
            handle.write(canonical_json_bytes(row) + b"\n")


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AedError(
                    BusinessCode.E_CONFIG_INVALID,
                    f"invalid JSON on line {line_number} of {path}: {exc.msg}",
                    field="jsonl",
                    details={"line": line_number},
                ) from exc
            if not isinstance(value, dict):
                raise AedError(
                    BusinessCode.E_CONFIG_INVALID,
                    f"JSONL row {line_number} in {path} must be an object",
                    field="jsonl",
                    details={"line": line_number},
                )
            yield value


def load_manifest(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = ("dialog_id", "sample_rate", "duration_ms", "modality_mask")
    for row in iter_jsonl(path):
        missing = [field for field in required if field not in row]
        if missing:
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                f"manifest row is missing fields: {missing}",
                field=missing[0],
                details={"missing_fields": missing},
            )
        dialog_id = row["dialog_id"]
        if not isinstance(dialog_id, str) or not dialog_id:
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                "dialog_id must be a non-empty string",
                field="dialog_id",
            )
        if dialog_id in seen:
            raise AedError(
                BusinessCode.E_DUP_DIALOG,
                f"duplicate dialog_id: {dialog_id}",
                dialog_id=dialog_id,
                field="dialog_id",
            )
        seen.add(dialog_id)
        if row["sample_rate"] != 16_000:
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                f"sample_rate must be 16000 for dialog {dialog_id}",
                dialog_id=dialog_id,
                field="sample_rate",
            )
        duration_ms = row["duration_ms"]
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, (int, float))
            or not math.isfinite(float(duration_ms))
            or float(duration_ms) <= 0
        ):
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                f"duration_ms must be positive for dialog {dialog_id}",
                dialog_id=dialog_id,
                field="duration_ms",
            )
        rows.append(row)
    if not rows:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "manifest contains no dialogs",
            field="manifest",
        )
    return rows


def load_truth_frames(path: str | Path, *, dialog_id: str | None = None) -> list[dict[str, Any]]:
    required = ("frame_index", "event_id", "event_type", "speaker_id", "is_speech")
    rows: list[dict[str, Any]] = []
    expected_index = 0
    for row in iter_jsonl(path):
        missing = [field for field in required if field not in row]
        if missing:
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                f"truth row is missing fields: {missing}",
                dialog_id=dialog_id,
                field=missing[0],
                details={"frame_index": row.get("frame_index")},
            )
        frame_index = row["frame_index"]
        if isinstance(frame_index, bool) or not isinstance(frame_index, int):
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                "frame_index must be an integer",
                dialog_id=dialog_id,
                field="frame_index",
            )
        if frame_index != expected_index:
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                f"truth frame indices must be contiguous; expected {expected_index}, got {frame_index}",
                dialog_id=dialog_id,
                field="frame_index",
                details={"expected": expected_index, "actual": frame_index},
            )
        event_type = row["event_type"]
        if event_type not in EVENT_TYPE_IDS:
            raise AedError(
                BusinessCode.E_EVENT_TYPE,
                f"unknown truth event type {event_type!r}",
                dialog_id=dialog_id,
                field="event_type",
                details={"frame_index": frame_index, "event_type": event_type},
            )
        if not isinstance(row["is_speech"], bool):
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                "is_speech must be boolean",
                dialog_id=dialog_id,
                field="is_speech",
                details={"frame_index": frame_index},
            )
        if row["event_id"] is not None and not isinstance(row["event_id"], str):
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                "event_id must be a string or null",
                dialog_id=dialog_id,
                field="event_id",
                details={"frame_index": frame_index},
            )
        if row["speaker_id"] is not None and not isinstance(row["speaker_id"], str):
            raise AedError(
                BusinessCode.E_TIMELINE_INVALID,
                "speaker_id must be a string or null",
                dialog_id=dialog_id,
                field="speaker_id",
                details={"frame_index": frame_index},
            )
        rows.append(row)
        expected_index += 1
    if not rows:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "truth file contains no frames",
            dialog_id=dialog_id,
            field="truth_frames",
        )
    return rows


def write_wav_pcm16_mono(path: str | Path, pcm: np.ndarray, sample_rate: int = 16_000) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    samples = np.asarray(pcm, dtype=np.float64)
    if samples.ndim != 1:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "PCM must be a one-dimensional mono signal",
            field="pcm",
        )
    quantized = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype("<i2")
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(quantized.tobytes())


def load_wav_pcm16_mono(path: str | Path, *, dialog_id: str | None = None) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frame_count = handle.getnframes()
        raw = handle.readframes(frame_count)
    if channels != 1 or sample_width != 2:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "audio must be 16-bit mono PCM",
            dialog_id=dialog_id,
            field="audio",
            details={"channels": channels, "sample_width": sample_width},
        )
    if sample_rate != 16_000:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            f"audio sample_rate must be 16000, got {sample_rate}",
            dialog_id=dialog_id,
            field="sample_rate",
        )
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return pcm, sample_rate


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
