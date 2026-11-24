"""Minimal dependency-free RIFF/WAVE PCM reader and writer."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from .errors import PipelineError
from .util import sha256_file


_PCM = 1
_IEEE_FLOAT = 3
_EXTENSIBLE = 0xFFFE


@dataclass(frozen=True)
class WavInfo:
    path: Path
    sample_rate: int
    channels: int
    bits_per_sample: int
    container_bits: int
    frames: int
    data_offset: int
    data_size: int
    block_align: int
    is_float: bool
    file_size: int
    sha256: str


def _read_exact(handle, count: int, *, path: Path) -> bytes:
    data = handle.read(count)
    if len(data) != count:
        raise PipelineError("E_WAV", f"truncated WAV file: {path}")
    return data


def inspect_wav(path: Path) -> WavInfo:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PipelineError("E_INPUT_MISSING", f"cannot stat input {path}: {exc}") from exc
    try:
        with path.open("rb") as handle:
            header = _read_exact(handle, 12, path=path)
            riff, riff_size, wave = struct.unpack("<4sI4s", header)
            if riff != b"RIFF" or wave != b"WAVE":
                raise PipelineError("E_WAV", f"not a little-endian RIFF/WAVE file: {path}")
            if riff_size + 8 > size:
                raise PipelineError("E_WAV", f"RIFF size exceeds file size: {path}")

            fmt: tuple[int, int, int, int, int, int, int] | None = None
            data_offset = -1
            data_size = -1
            while handle.tell() + 8 <= min(size, riff_size + 8):
                chunk_id, chunk_size = struct.unpack("<4sI", _read_exact(handle, 8, path=path))
                body_offset = handle.tell()
                if body_offset + chunk_size > size:
                    raise PipelineError("E_WAV", f"chunk {chunk_id!r} exceeds file size: {path}")
                if chunk_id == b"fmt ":
                    if chunk_size < 16:
                        raise PipelineError("E_WAV", f"fmt chunk is too short: {path}")
                    body = _read_exact(handle, chunk_size, path=path)
                    (
                        tag, channels, sample_rate, byte_rate, block_align,
                        container_bits,
                    ) = struct.unpack("<HHIIHH", body[:16])
                    valid_bits = container_bits
                    if tag == _EXTENSIBLE:
                        if chunk_size < 40:
                            raise PipelineError("E_WAV", f"extensible fmt chunk is short: {path}")
                        valid_bits = struct.unpack("<H", body[18:20])[0]
                        subformat = body[24:40]
                        tag = struct.unpack("<H", subformat[:2])[0]
                    fmt = (tag, channels, sample_rate, block_align,
                           container_bits, valid_bits, chunk_size)
                    if byte_rate != sample_rate * block_align:
                        raise PipelineError(
                            "E_WAV", f"invalid byte rate {byte_rate}: {path}"
                        )
                elif chunk_id == b"data":
                    data_offset = body_offset
                    data_size = chunk_size
                    handle.seek(chunk_size, os.SEEK_CUR)
                else:
                    handle.seek(chunk_size, os.SEEK_CUR)
                if chunk_size & 1:
                    handle.seek(1, os.SEEK_CUR)
    except PipelineError:
        raise
    except OSError as exc:
        raise PipelineError("E_WAV", f"cannot read WAV {path}: {exc}") from exc

    if fmt is None:
        raise PipelineError("E_WAV", f"missing fmt chunk: {path}")
    if data_offset < 0:
        raise PipelineError("E_WAV", f"missing data chunk: {path}")

    tag, channels, sample_rate, block_align, container_bits, valid_bits, _ = fmt
    if tag not in (_PCM, _IEEE_FLOAT):
        raise PipelineError("E_WAV", f"unsupported WAV format tag {tag}: {path}")
    if channels < 1:
        raise PipelineError("E_WAV", f"invalid channel count {channels}: {path}")
    if sample_rate <= 0:
        raise PipelineError("E_SR", f"invalid sample rate {sample_rate}: {path}")
    expected_align = channels * ((container_bits + 7) // 8)
    if block_align != expected_align:
        raise PipelineError("E_WAV", f"invalid block alignment {block_align}: {path}")
    if data_size % block_align:
        raise PipelineError("E_WAV", f"data size is not frame aligned: {path}")
    frames = data_size // block_align
    return WavInfo(
        path=path,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=valid_bits,
        container_bits=container_bits,
        frames=frames,
        data_offset=data_offset,
        data_size=data_size,
        block_align=block_align,
        is_float=tag == _IEEE_FLOAT,
        file_size=size,
        sha256=sha256_file(path),
    )


def _decode_block(raw: bytes, bits: int, channels: int, is_float: bool) -> np.ndarray:
    if is_float:
        if bits == 32:
            values = np.frombuffer(raw, dtype="<f4").astype(np.float32)
        elif bits == 64:
            values = np.frombuffer(raw, dtype="<f8").astype(np.float32)
        else:
            raise PipelineError("E_WAV", f"unsupported float WAV width {bits}")
    elif bits == 16:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif bits == 24:
        packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        integer = (
            packed[:, 0].astype(np.int32)
            | (packed[:, 1].astype(np.int32) << 8)
            | (packed[:, 2].astype(np.int32) << 16)
        )
        integer = np.where(integer & 0x800000, integer - 0x1000000, integer)
        values = integer.astype(np.float32) / 8388608.0
    elif bits == 8:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif bits == 32:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
        values = values.astype(np.float32)
    else:
        raise PipelineError("E_BIT_DEPTH", f"unsupported PCM WAV width {bits}")
    return values.reshape(-1, channels)


def iter_decoded_blocks(info: WavInfo, block_frames: int = 16384) -> Iterator[np.ndarray]:
    if block_frames <= 0:
        raise ValueError("block_frames must be positive")
    bytes_per_frame = info.block_align
    frames_left = info.frames
    try:
        with info.path.open("rb") as handle:
            handle.seek(info.data_offset)
            while frames_left:
                count = min(block_frames, frames_left)
                raw = _read_exact(handle, count * bytes_per_frame, path=info.path)
                yield _decode_block(
                    raw, info.container_bits, info.channels, info.is_float
                )
                frames_left -= count
    except PipelineError:
        raise
    except OSError as exc:
        raise PipelineError("E_WAV", f"cannot decode WAV {info.path}: {exc}") from exc


def decode_wav(info: WavInfo, block_frames: int = 16384,
               on_block=None) -> np.ndarray:
    data = np.empty((info.frames, info.channels), dtype=np.float32)
    position = 0
    for block in iter_decoded_blocks(info, block_frames=block_frames):
        end = position + block.shape[0]
        data[position:end] = block
        position = end
        if on_block is not None:
            on_block(block)
    if data.shape[0] != info.frames:
        raise PipelineError(
            "E_WAV",
            f"decoded frame count {data.shape[0]} != header {info.frames}",
        )
    if position != info.frames:
        raise PipelineError("E_WAV", "decoded frame count differs from header")
    return data


def quantize_pcm(samples: np.ndarray, bits: int) -> bytes:
    if bits not in (16, 24):
        raise PipelineError("E_BIT_DEPTH", f"output bit depth must be 16 or 24, got {bits}")
    scale = float(1 << (bits - 1))
    payload = bytearray()
    chunk_frames = 65536
    for start in range(0, samples.shape[0], chunk_frames):
        block = samples[start:start + chunk_frames].astype(np.float64, copy=False)
        clipped = np.clip(block, -1.0, 1.0 - 1.0 / scale)
        integers = np.rint(clipped * scale).astype(np.int64)
        integers = np.clip(
            integers, -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        )
        flat = integers.reshape(-1)
        if bits == 16:
            payload.extend(flat.astype("<i2").tobytes())
        else:
            unsigned = flat & 0xFFFFFF
            packed = np.empty((flat.size, 3), dtype=np.uint8)
            packed[:, 0] = unsigned & 0xFF
            packed[:, 1] = (unsigned >> 8) & 0xFF
            packed[:, 2] = (unsigned >> 16) & 0xFF
            payload.extend(packed.tobytes())
    return bytes(payload)


def encode_wav_bytes(samples: np.ndarray, sample_rate: int, bits: int) -> bytes:
    if samples.ndim != 2 or samples.shape[1] not in (1, 2):
        raise PipelineError("E_CHANNELS", "output samples must be mono or stereo")
    if sample_rate <= 0:
        raise PipelineError("E_SR", "output sample rate must be positive")
    payload = quantize_pcm(samples, bits)
    channels = samples.shape[1]
    bytes_per_sample = bits // 8
    block_align = channels * bytes_per_sample
    byte_rate = sample_rate * block_align
    fmt = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,
        _PCM,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
    )
    data_header = struct.pack("<4sI", b"data", len(payload))
    body = b"WAVE" + fmt + data_header + payload
    if len(payload) & 1:
        body += b"\x00"
    return struct.pack("<4sI", b"RIFF", len(body)) + body


def write_wav_atomic(path: Path, samples: np.ndarray, sample_rate: int, bits: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(encode_wav_bytes(samples, sample_rate, bits))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise PipelineError("E_IO", f"cannot write WAV {path}: {exc}") from exc


def resample_rational(samples: np.ndarray, source_rate: int,
                      target_rate: int) -> np.ndarray:
    """Resample on exact rational source/target phase positions using linear interpolation."""
    if source_rate <= 0 or target_rate <= 0:
        raise PipelineError("E_SR", "resampling rates must be positive")
    frames = samples.shape[0]
    if source_rate == target_rate:
        return samples.copy()
    gcd = int(np.gcd(source_rate, target_rate))
    up = target_rate // gcd
    down = source_rate // gcd
    output_frames = int(round(frames * up / down))
    if output_frames <= 0 or frames == 0:
        return np.empty((output_frames, samples.shape[1]), dtype=np.float32)
    if frames == 1:
        return np.repeat(samples, output_frames, axis=0)
    result = np.empty((output_frames, samples.shape[1]), dtype=np.float32)
    chunk_frames = 65536
    ratio = down / up
    for start in range(0, output_frames, chunk_frames):
        end = min(output_frames, start + chunk_frames)
        positions = np.arange(start, end, dtype=np.float64) * ratio
        positions = np.minimum(positions, frames - 1)
        base = np.floor(positions).astype(np.int64)
        frac = (positions - base).astype(np.float32)
        next_index = np.minimum(base + 1, frames - 1)
        left = samples[base]
        right = samples[next_index]
        result[start:end] = left + (right - left) * frac[:, None]
    return result
