"""Strict RIFF/WAVE validation and deterministic PCM decoding."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import sha256_bytes
from .config import Config


class AudioFormatError(ValueError):
    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = "E_AUDIO_FORMAT"
        self.detail = detail or message


@dataclass(frozen=True)
class AudioData:
    name: str
    path: Path
    file_sha256: str
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int
    pcm: np.ndarray
    samples: np.ndarray

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.file_sha256,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width_bytes": self.sample_width,
            "bits_per_sample": self.sample_width * 8,
            "pcm_frame_count": self.frame_count,
            "duration_seconds": self.frame_count / self.sample_rate,
        }


def scan_wav_files(sample_dir: str | Path) -> list[Path]:
    root = Path(sample_dir)
    if not root.is_dir():
        raise AudioFormatError(f"sample directory does not exist: {root}", detail="directory")
    files = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".wav"]
    return sorted(files, key=lambda path: relative_name(path, root))


def relative_name(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _parse_chunks(blob: bytes) -> dict[str, tuple[int, int, int]]:
    """Return chunk id -> (declared payload size, payload offset, padded end)."""
    if len(blob) < 12 or blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise AudioFormatError("not a RIFF/WAVE container", detail="missing RIFF/WAVE signature")
    declared_riff_size = struct.unpack_from("<I", blob, 4)[0]
    if declared_riff_size + 8 != len(blob):
        raise AudioFormatError(
            "RIFF declared size does not match file length",
            detail=f"riff_size={declared_riff_size}, file_size={len(blob)}",
        )
    result: dict[str, tuple[int, int, int]] = {}
    offset = 12
    while offset < len(blob):
        if offset + 8 > len(blob):
            raise AudioFormatError("truncated RIFF chunk header", detail=f"offset={offset}")
        chunk_id = blob[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", blob, offset + 4)[0]
        payload_offset = offset + 8
        payload_end = payload_offset + chunk_size
        if payload_end > len(blob):
            raise AudioFormatError(
                "RIFF chunk exceeds file length",
                detail=f"chunk={chunk_id!r}, size={chunk_size}, offset={offset}",
            )
        padded_end = payload_end + (chunk_size & 1)
        if padded_end > len(blob):
            raise AudioFormatError("missing RIFF chunk padding", detail=f"chunk={chunk_id!r}")
        key = chunk_id.decode("ascii", errors="replace")
        if key in result:
            raise AudioFormatError("duplicate RIFF chunk", detail=key)
        result[key] = (chunk_size, payload_offset, padded_end)
        offset = padded_end
    return result


def read_wav(path: str | Path, root: str | Path, config: Config) -> AudioData:
    path = Path(path)
    root = Path(root)
    name = relative_name(path, root)
    blob = path.read_bytes()
    if not blob:
        raise AudioFormatError("empty WAV file", detail="empty_file")
    chunks = _parse_chunks(blob)
    if "fmt " not in chunks or "data" not in chunks:
        raise AudioFormatError("WAV requires fmt and data chunks", detail="missing_chunk")
    fmt_size, fmt_offset, _ = chunks["fmt "]
    if fmt_size < 16:
        raise AudioFormatError("fmt chunk is too short", detail=f"fmt_size={fmt_size}")
    (
        format_tag,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    ) = struct.unpack_from("<HHIIHH", blob, fmt_offset)
    if format_tag != 1:
        raise AudioFormatError("only integer PCM format is supported", detail=f"tag={format_tag}")
    if channels != config.expected_channels:
        raise AudioFormatError(
            "unexpected channel count", detail=f"channels={channels}, expected={config.expected_channels}"
        )
    if sample_rate != config.expected_sample_rate:
        raise AudioFormatError(
            "unexpected sample rate",
            detail=f"sample_rate={sample_rate}, expected={config.expected_sample_rate}",
        )
    if bits_per_sample != config.expected_sample_width * 8:
        raise AudioFormatError(
            "unexpected bit depth",
            detail=f"bits={bits_per_sample}, expected={config.expected_sample_width * 8}",
        )
    expected_align = channels * ((bits_per_sample + 7) // 8)
    if block_align != expected_align:
        raise AudioFormatError(
            "inconsistent block alignment",
            detail=f"block_align={block_align}, expected={expected_align}",
        )
    if byte_rate != sample_rate * block_align:
        raise AudioFormatError(
            "inconsistent byte rate",
            detail=f"byte_rate={byte_rate}, expected={sample_rate * block_align}",
        )
    data_size, data_offset, _ = chunks["data"]
    if data_size % block_align != 0:
        raise AudioFormatError(
            "data length is not a whole number of PCM frames",
            detail=f"data_size={data_size}, block_align={block_align}",
        )
    frame_count = data_size // block_align
    payload = blob[data_offset : data_offset + data_size]
    if len(payload) != data_size:
        raise AudioFormatError("data payload length mismatch", detail="short_payload")
    pcm = np.frombuffer(payload, dtype="<i2").astype(np.int16, copy=True)
    if pcm.size != frame_count * channels:
        raise AudioFormatError("decoded PCM length mismatch", detail="pcm_length")
    if channels != 1:
        pcm = pcm.reshape(-1, channels)
    samples = pcm.astype(np.float64) / 32768.0
    return AudioData(
        name=name,
        path=path,
        file_sha256=sha256_bytes(blob),
        sample_rate=sample_rate,
        channels=channels,
        sample_width=config.expected_sample_width,
        frame_count=frame_count,
        pcm=pcm,
        samples=samples,
    )


def validate_all_files(sample_dir: str | Path, config: Config) -> tuple[list[AudioData], list[dict[str, Any]]]:
    root = Path(sample_dir)
    audio: list[AudioData] = []
    failures: list[dict[str, Any]] = []
    for path in scan_wav_files(root):
        name = relative_name(path, root)
        try:
            audio.append(read_wav(path, root, config))
        except AudioFormatError as exc:
            failures.append(
                {
                    "sample": name,
                    "stage": "audio",
                    "code": exc.code,
                    "message": str(exc),
                    "detail": exc.detail,
                }
            )
    return audio, failures

