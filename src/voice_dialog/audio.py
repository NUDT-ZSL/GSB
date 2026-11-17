"""Deterministic WAV discovery, RIFF/format validation, and PCM decoding."""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from .constants import E_AUDIO_FORMAT, E_NONFINITE_SAMPLE, E_ZERO_DURATION
from .errors import BusinessError
from .util import file_hash


@dataclass(frozen=True)
class WavHeader:
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int
    compression: str
    duration_sec: float


@dataclass
class AudioScan:
    filename: str
    path: Path
    content_sha256: str
    valid: bool
    header: WavHeader | None = None
    error_code: str = ""
    error_message: str = ""
    injection_id: str = ""

    @property
    def duration_sec(self) -> float:
        return self.header.duration_sec if self.header else 0.0

    def failure(self) -> dict:
        return {
            "code": self.error_code or E_AUDIO_FORMAT,
            "phase": "scan",
            "filename": self.filename,
            "turn_id": "",
            "exception_type": "AudioFormatError",
            "message": self.error_message,
            "injection_id": self.injection_id,
        }


@dataclass
class DecodedAudio:
    header: WavHeader
    samples: np.ndarray
    zero_duration_injected: bool = False
    nonfinite_injected: bool = False


def _wave_reader(path: Path) -> WavHeader:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(12)
    except OSError as exc:
        raise BusinessError(
            E_AUDIO_FORMAT, "scan", f"cannot open WAV: {exc}", filename=path.name,
            exception_type=type(exc).__name__,
        ) from exc
    if len(prefix) < 12 or prefix[0:4] != b"RIFF" or prefix[8:12] != b"WAVE":
        raise BusinessError(
            E_AUDIO_FORMAT, "scan", "missing RIFF/WAVE container header",
            filename=path.name, exception_type="AudioFormatError",
        )
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            sample_rate = reader.getframerate()
            sample_width = reader.getsampwidth()
            frame_count = reader.getnframes()
            compression = reader.getcomptype()
    except (wave.Error, EOFError, OSError) as exc:
        raise BusinessError(
            E_AUDIO_FORMAT, "scan", f"invalid WAV payload: {exc}", filename=path.name,
            exception_type=type(exc).__name__,
        ) from exc
    if sample_rate != 16000:
        raise BusinessError(
            E_AUDIO_FORMAT, "scan", f"sample rate must be 16000, got {sample_rate}",
            filename=path.name, exception_type="AudioFormatError",
        )
    if channels != 1:
        raise BusinessError(
            E_AUDIO_FORMAT, "scan", f"channel count must be 1, got {channels}",
            filename=path.name, exception_type="AudioFormatError",
        )
    if sample_width != 2:
        raise BusinessError(
            E_AUDIO_FORMAT, "scan", f"sample width must be 2 bytes, got {sample_width}",
            filename=path.name, exception_type="AudioFormatError",
        )
    if compression != "NONE":
        raise BusinessError(
            E_AUDIO_FORMAT, "scan", f"compression must be PCM/NONE, got {compression}",
            filename=path.name, exception_type="AudioFormatError",
        )
    if frame_count < 0:
        raise BusinessError(
            E_AUDIO_FORMAT, "scan", "negative frame count", filename=path.name,
            exception_type="AudioFormatError",
        )
    return WavHeader(
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frame_count=frame_count,
        compression=compression,
        duration_sec=frame_count / sample_rate if sample_rate else 0.0,
    )


def scan_audio_directory(root: Path) -> list[AudioScan]:
    """Recursively scan WAVs in a stable, case-insensitive relative-path order."""
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".wav":
            candidates.append(path)
    candidates.sort(key=lambda p: (p.relative_to(root).as_posix().lower(), p.relative_to(root).as_posix()))
    scans: list[AudioScan] = []
    for path in candidates:
        filename = path.relative_to(root).as_posix()
        content_hash = file_hash(path)
        try:
            header = _wave_reader(path)
        except BusinessError as exc:
            scans.append(
                AudioScan(
                    filename=filename,
                    path=path,
                    content_sha256=content_hash,
                    valid=False,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            )
            continue
        scans.append(
            AudioScan(
                filename=filename,
                path=path,
                content_sha256=content_hash,
                valid=True,
                header=header,
            )
        )
    return scans


def _injection_for_target(injections: Iterable[dict], kind: str, target: str) -> dict | None:
    for item in injections:
        if item.get("kind") == kind and item.get("target") == target:
            return item
    return None


def decode_audio(scan: AudioScan, injections: Iterable[dict] | None = None) -> DecodedAudio:
    injections = list(injections or [])
    if not scan.valid or scan.header is None:
        raise BusinessError(
            scan.error_code or E_AUDIO_FORMAT,
            "audio",
            scan.error_message or "invalid audio",
            filename=scan.filename,
            exception_type="AudioFormatError",
            injection_id=scan.injection_id,
        )
    zero_injection = _injection_for_target(injections, "zero_duration", scan.filename)
    if scan.header.frame_count == 0 or zero_injection is not None:
        raise BusinessError(
            E_ZERO_DURATION,
            "audio",
            "audio has zero duration",
            filename=scan.filename,
            exception_type="ZeroDurationError",
            injection_id=zero_injection.get("id", "") if zero_injection else "",
        )
    try:
        with wave.open(str(scan.path), "rb") as reader:
            raw = reader.readframes(scan.header.frame_count)
    except (wave.Error, EOFError, OSError) as exc:
        raise BusinessError(
            E_AUDIO_FORMAT,
            "audio",
            f"cannot decode PCM payload: {exc}",
            filename=scan.filename,
            exception_type=type(exc).__name__,
        ) from exc
    expected_bytes = scan.header.frame_count * scan.header.channels * scan.header.sample_width
    if len(raw) != expected_bytes:
        raise BusinessError(
            E_AUDIO_FORMAT,
            "audio",
            f"PCM payload length mismatch: expected {expected_bytes}, got {len(raw)}",
            filename=scan.filename,
            exception_type="AudioFormatError",
        )
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    nonfinite_injection = _injection_for_target(injections, "nonfinite_sample", scan.filename)
    if nonfinite_injection is not None and samples.size:
        samples[0] = np.nan
    return DecodedAudio(
        header=scan.header,
        samples=samples,
        zero_duration_injected=zero_injection is not None,
        nonfinite_injected=nonfinite_injection is not None,
    )


def validate_samples(samples: np.ndarray, *, filename: str, injection_id: str = "") -> None:
    if not np.all(np.isfinite(samples)):
        raise BusinessError(
            E_NONFINITE_SAMPLE,
            "features",
            "decoded sample stream contains a non-finite value",
            filename=filename,
            exception_type="NonFiniteSampleError",
            injection_id=injection_id,
        )
