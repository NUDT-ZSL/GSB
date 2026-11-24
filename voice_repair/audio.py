from __future__ import annotations

import hashlib
import io
import math
import os
import tempfile
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import RepairError


ALLOWED_SAMPLE_RATES = frozenset({16000, 44100, 48000})
INT16_SCALE = 32768.0
INT16_MAX = 32767.0


@dataclass(frozen=True)
class WavInfo:
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    duration_seconds: float
    sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_wav(path: Path, *, compute_digest: bool = True) -> WavInfo:
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
            compression = wav.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise RepairError("E_WAV_HEADER", f"cannot parse WAV header: {exc}") from exc

    if compression != "NONE":
        raise RepairError("E_WAV_COMPRESSION", f"unsupported WAV compression: {compression}")
    if frames <= 0:
        raise RepairError("E_WAV_EMPTY", "WAV contains no sample frames")
    if sample_rate not in ALLOWED_SAMPLE_RATES:
        raise RepairError(
            "E_SAMPLE_RATE",
            f"sample rate {sample_rate} is not one of 16000/44100/48000",
        )
    if sample_width != 2:
        raise RepairError("E_BIT_DEPTH", f"bit depth {sample_width * 8} is not 16")
    if channels != 1:
        raise RepairError("E_CHANNELS", f"channel count {channels} is not 1")

    digest = sha256_file(path) if compute_digest else ""
    return WavInfo(
        path=path,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frames=frames,
        duration_seconds=frames / sample_rate,
        sha256=digest,
    )


def read_wav_interval(path: Path, start: int, end: int) -> np.ndarray:
    if start < 0 or end < start:
        raise RepairError("E_SAMPLE_RANGE", f"invalid WAV interval [{start}, {end})")
    try:
        with wave.open(str(path), "rb") as wav:
            wav.setpos(start)
            raw = wav.readframes(end - start)
    except (OSError, EOFError, wave.Error) as exc:
        raise RepairError("E_WAV_READ", f"cannot read WAV interval: {exc}") from exc
    expected_bytes = (end - start) * 2
    if len(raw) != expected_bytes:
        raise RepairError(
            "E_WAV_SHORT_READ",
            f"read {len(raw)} bytes, expected {expected_bytes}",
        )
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / INT16_SCALE


def wav_mean(path: Path, chunk_frames: int = 262_144) -> float:
    total = 0.0
    count = 0
    try:
        with wave.open(str(path), "rb") as wav:
            while True:
                raw = wav.readframes(chunk_frames)
                if not raw:
                    break
                values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / INT16_SCALE
                total += float(np.sum(values, dtype=np.float64))
                count += int(values.size)
    except (OSError, EOFError, wave.Error) as exc:
        raise RepairError("E_WAV_READ", f"cannot compute WAV mean: {exc}") from exc
    if count == 0:
        raise RepairError("E_WAV_EMPTY", "WAV contains no sample frames")
    return total / count


def float64_to_pcm16(values: np.ndarray) -> bytes:
    clipped = np.clip(values, -1.0, 1.0)
    pcm = np.rint(clipped * INT16_MAX).astype("<i2", copy=False)
    return pcm.tobytes(order="C")


def wav_bytes(values: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(float64_to_pcm16(values))
    return buffer.getvalue()


def write_wav_atomic(path: Path, values: np.ndarray, sample_rate: int) -> tuple[str, bool]:
    """Write a deterministic WAV through a same-directory temp file.

    Returns (sha256, changed). An existing file with an identical content digest is
    never replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = wav_bytes(values, sample_rate)
    digest = sha256_bytes(data)
    old_digest = sha256_file(path) if path.is_file() else None
    if old_digest == digest:
        return digest, False

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return digest, True


def write_wav_blocks_atomic(
    path: Path,
    blocks: Iterable[np.ndarray],
    total_samples: int,
    sample_rate: int,
) -> tuple[str, bool]:
    """Stream final audio blocks into a same-directory temp WAV, then replace.

    The caller owns block ordering and total length correctness.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    written = 0
    try:
        with wave.open(str(temp_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            for block in blocks:
                pcm = float64_to_pcm16(np.asarray(block, dtype=np.float64))
                wav.writeframes(pcm)
                written += len(pcm) // 2
        if written != total_samples:
            raise RepairError(
                "E_OUTPUT_LENGTH",
                f"wrote {written} samples, expected {total_samples}",
            )
        try:
            with temp_path.open("rb+") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass
        digest = sha256_file(temp_path)
        old_digest = sha256_file(path) if path.is_file() else None
        if old_digest == digest:
            return digest, False
        os.replace(temp_path, path)
        return digest, True
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict) -> None:
    import json

    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(path, data)


def finite_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RepairError("E_PARAM_TYPE", f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise RepairError("E_PARAM_FINITE", f"{name} must be finite")
    return result
