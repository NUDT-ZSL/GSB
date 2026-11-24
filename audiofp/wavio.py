from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import fail


ALLOWED_SAMPLE_RATES = frozenset({16000, 44100, 48000})


@dataclass(frozen=True)
class WavInfo:
    path: Path
    label: str
    sample_rate: int
    channels: int
    bits_per_sample: int
    data_offset: int
    data_size: int
    sample_count: int
    pcm_sha256: str

    @property
    def duration_seconds(self) -> float:
        return self.sample_count / float(self.sample_rate)


def _read_exact(handle, length: int) -> bytes:
    data = handle.read(length)
    if len(data) != length:
        fail("E_WAV_TRUNCATED", "WAV 数据块被截断")
    return data


def _parse_fmt(raw: bytes) -> tuple[int, int, int, int, int]:
    if len(raw) < 16:
        fail("E_WAV_HEADER", "WAV fmt 块长度不足 16 字节")
    audio_format, channels, sample_rate, byte_rate, block_align, bits = struct.unpack(
        "<HHIIHH", raw[:16]
    )
    if audio_format == 0xFFFE:
        if len(raw) < 40:
            fail("E_WAV_HEADER", "WAVE_FORMAT_EXTENSIBLE fmt 块长度不足")
        extension_size = struct.unpack("<H", raw[16:18])[0]
        if extension_size < 22:
            fail("E_WAV_HEADER", "WAVE_FORMAT_EXTENSIBLE 扩展信息非法")
        sub_format = raw[24:40]
        if sub_format[:2] != b"\x01\x00":
            fail("E_WAV_FORMAT", "仅支持整数 PCM WAV", audio_format=audio_format)
        audio_format = 1
    if audio_format != 1:
        fail("E_WAV_FORMAT", "仅支持整数 PCM WAV", audio_format=audio_format)
    if channels != 1:
        fail("E_CHANNELS", "WAV 必须为单声道", channels=channels)
    if bits != 16:
        fail("E_BIT_DEPTH", "WAV 必须为 16bit", bits_per_sample=bits)
    if sample_rate not in ALLOWED_SAMPLE_RATES:
        fail(
            "E_SAMPLE_RATE",
            "WAV 采样率不在允许集合内",
            sample_rate=sample_rate,
            allowed=sorted(ALLOWED_SAMPLE_RATES),
        )
    if block_align != 2:
        fail("E_WAV_HEADER", "单声道 16bit WAV 的 block_align 必须为 2", block_align=block_align)
    if byte_rate != sample_rate * block_align:
        fail(
            "E_WAV_HEADER",
            "WAV byte_rate 与采样率/block_align 不一致",
            byte_rate=byte_rate,
        )
    return audio_format, channels, sample_rate, block_align, bits


def parse_wav(path: Path, *, label: str | None = None) -> WavInfo:
    path = Path(path)
    display_label = label if label is not None else str(path)
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        fail("E_PATH", "无法读取 WAV 文件", path=display_label, error=str(exc))
    if file_size < 12:
        fail("E_WAV_HEADER", "WAV 文件过短", path=display_label)

    fmt_data: bytes | None = None
    fmt_parsed: tuple[int, int, int, int, int] | None = None
    data_offset: int | None = None
    data_size: int | None = None

    try:
        with path.open("rb") as handle:
            header = _read_exact(handle, 12)
            if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                fail("E_WAV_HEADER", "不是 RIFF/WAVE 文件", path=display_label)
            riff_size = struct.unpack("<I", header[4:8])[0]
            if riff_size < 4 or riff_size + 8 > file_size:
                fail(
                    "E_WAV_TRUNCATED",
                    "RIFF 声明长度超过文件长度",
                    path=display_label,
                    file_size=file_size,
                    riff_size=riff_size,
                )

            position = 12
            while position + 8 <= file_size:
                handle.seek(position)
                chunk_header = handle.read(8)
                if len(chunk_header) != 8:
                    break
                chunk_id = chunk_header[:4]
                chunk_size = struct.unpack("<I", chunk_header[4:])[0]
                chunk_data_offset = position + 8
                if chunk_data_offset + chunk_size > file_size:
                    fail(
                        "E_WAV_TRUNCATED",
                        "WAV 块长度超过文件长度",
                        path=display_label,
                        chunk=chunk_id.decode("latin-1", "replace"),
                    )
                if chunk_id == b"fmt ":
                    if fmt_data is not None:
                        fail("E_WAV_HEADER", "WAV 包含多个 fmt 块", path=display_label)
                    if chunk_size > 1024 * 1024:
                        fail("E_WAV_HEADER", "WAV fmt 块异常过大", path=display_label)
                    handle.seek(chunk_data_offset)
                    fmt_data = _read_exact(handle, chunk_size)
                    fmt_parsed = _parse_fmt(fmt_data)
                elif chunk_id == b"data":
                    if data_offset is not None:
                        fail("E_WAV_HEADER", "WAV 包含多个 data 块", path=display_label)
                    if fmt_parsed is None:
                        fail("E_WAV_HEADER", "WAV data 块出现在 fmt 块之前", path=display_label)
                    data_offset = chunk_data_offset
                    data_size = chunk_size
                    break
                position = chunk_data_offset + chunk_size + (chunk_size & 1)
    except OSError as exc:
        fail("E_PATH", "读取 WAV 头失败", path=display_label, error=str(exc))

    if fmt_parsed is None or fmt_data is None:
        fail("E_WAV_HEADER", "WAV 缺少 fmt 块", path=display_label)
    if data_offset is None or data_size is None:
        fail("E_WAV_HEADER", "WAV 缺少 data 块", path=display_label)
    if data_size % 2 != 0:
        fail("E_WAV_HEADER", "16bit PCM data 块字节数必须为偶数", path=display_label)

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            handle.seek(data_offset)
            remaining = data_size
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    fail("E_WAV_TRUNCATED", "WAV PCM 数据被截断", path=display_label)
                digest.update(block)
                remaining -= len(block)
    except OSError as exc:
        fail("E_PATH", "读取 WAV PCM 数据失败", path=display_label, error=str(exc))

    _, channels, sample_rate, _, bits = fmt_parsed
    return WavInfo(
        path=path,
        label=display_label,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits,
        data_offset=data_offset,
        data_size=data_size,
        sample_count=data_size // 2,
        pcm_sha256=digest.hexdigest(),
    )


def audio_memmap(info: WavInfo) -> np.memmap:
    try:
        return np.memmap(
            info.path,
            dtype="<i2",
            mode="r",
            offset=info.data_offset,
            shape=(info.sample_count,),
            order="C",
        )
    except (OSError, ValueError) as exc:
        fail(
            "E_WAV_READ",
            "无法映射 WAV PCM 数据",
            path=info.label,
            error=str(exc),
        )


def read_audio(info: WavInfo) -> np.ndarray:
    view = audio_memmap(info)
    return np.asarray(view, dtype=np.float64) / 32768.0


def pcm_bytes_for_range(info: WavInfo, start_sample: int, end_sample: int) -> bytes:
    start = max(0, min(info.sample_count, int(start_sample)))
    end = max(start, min(info.sample_count, int(end_sample)))
    if end == start:
        return b""
    view = audio_memmap(info)
    return np.asarray(view[start:end], dtype="<i2").tobytes()


def file_uri_or_none(path: Path) -> str | None:
    try:
        return os.fspath(path.resolve())
    except OSError:
        return None

