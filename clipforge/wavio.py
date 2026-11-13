"""PCM WAV 的流式读取、增益处理与原子写出。

只接受 RIFF/WAVE 且 fmt 块为 PCM（format tag = 1）的文件；
采样位宽支持 1/2/3/4 字节（8/16/24/32 bit），声道数原样保留。
"""

from __future__ import annotations

import array
import math
import os
import struct
import sys
import tempfile
import wave
from dataclasses import dataclass


class AudioError(Exception):
    """带稳定错误码的音频错误，供 report.json 使用。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AudioInfo:
    path: str
    sample_rate: int
    channels: int
    sample_width: int
    frames: int

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def open_pcm_source(path: str) -> tuple[wave.Wave_read, AudioInfo]:
    """打开源 WAV 并返回 (Wave_read, AudioInfo)。

    文件缺失、非 PCM、位宽不受支持等都会抛出 AudioError。
    """
    if not os.path.exists(path):
        raise AudioError("source_missing", f"源文件不存在：{path}")
    if not os.path.isfile(path):
        raise AudioError("source_unreadable", f"源路径不是普通文件：{path}")

    try:
        wf = wave.open(path, "rb")
    except wave.Error as exc:
        raise AudioError(
            "source_not_pcm",
            f"源文件不是受支持的 PCM WAV：{exc}",
        ) from exc
    except EOFError as exc:
        raise AudioError("source_unreadable", f"源 WAV 头不完整：{exc}") from exc
    except OSError as exc:
        raise AudioError("source_unreadable", f"无法读取源文件：{exc}") from exc

    try:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frames = wf.getnframes()
    except (wave.Error, EOFError, OSError) as exc:
        wf.close()
        raise AudioError("source_not_pcm", f"源 WAV 格式信息不可用：{exc}") from exc

    if channels < 1:
        wf.close()
        raise AudioError("source_unsupported", f"源声道数非法：{channels}")
    if sample_rate <= 0:
        wf.close()
        raise AudioError("source_unsupported", f"源采样率非法：{sample_rate}")
    if sample_width not in (1, 2, 3, 4):
        wf.close()
        raise AudioError(
            "source_unsupported",
            f"不支持的采样位宽 {sample_width * 8} bit（仅支持 8/16/24/32 bit）",
        )

    return wf, AudioInfo(
        path=path,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frames=frames,
    )


def _round_half_away_from_zero(value: float) -> int:
    """确定性取整：0.5 远离 0，避免 Python round 的银行家舍入。"""
    if value >= 0:
        return int(math.floor(value + 0.5))
    return -int(math.floor(-value + 0.5))


def _clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _scale_array(data: bytes, typecode: str, width: int, low: int, high: int,
                 gain: float) -> bytes:
    """用 array 处理 16/32 bit 小端样本。"""
    arr = array.array(typecode)
    if arr.itemsize != width:  # 极少数平台 int 不是 4 字节，退回逐样本实现
        return _scale_via_int_bytes(data, width, low, high, gain)
    arr.frombytes(data)
    if sys.byteorder == "big":
        arr.byteswap()
    scaled = array.array(
        typecode,
        (_clamp(_round_half_away_from_zero(v * gain), low, high) for v in arr),
    )
    if sys.byteorder == "big":
        scaled.byteswap()
    return scaled.tobytes()


def _scale_via_int_bytes(data: bytes, width: int, low: int, high: int,
                         gain: float) -> bytes:
    if len(data) % width:
        raise AudioError(
            "source_read_error",
            f"PCM 数据长度 {len(data)} 不是采样位宽 {width} 的整数倍",
        )
    out = bytearray(len(data))
    for offset in range(0, len(data), width):
        value = int.from_bytes(data[offset:offset + width], "little", signed=True)
        scaled = _clamp(_round_half_away_from_zero(value * gain), low, high)
        out[offset:offset + width] = scaled.to_bytes(width, "little", signed=True)
    return bytes(out)


def _scale_8bit(data: bytes, gain: float) -> bytes:
    # WAV 8 bit 是无符号存储，静音中点为 128。
    out = bytearray(len(data))
    for i, value in enumerate(data):
        out[i] = _clamp(_round_half_away_from_zero((value - 128) * gain) + 128, 0, 255)
    return bytes(out)


def _scale_24bit(data: bytes, gain: float) -> bytes:
    if len(data) % 3:
        raise AudioError(
            "source_read_error",
            f"24 bit PCM 数据长度 {len(data)} 不是 3 的整数倍",
        )
    out = bytearray(len(data))
    for offset in range(0, len(data), 3):
        value = data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)
        if value & 0x800000:
            value -= 0x1000000
        scaled = _clamp(_round_half_away_from_zero(value * gain), -8388608, 8388607)
        out[offset:offset + 3] = (scaled & 0xFFFFFF).to_bytes(3, "little")
    return bytes(out)


def apply_gain_bytes(data: bytes, sample_width: int, gain: float) -> bytes:
    """对一段交错排列的 PCM 样本整体应用 gain，带饱和裁剪。

    gain == 1.0 时直接返回原字节，保证合法片段数据逐字节不变。
    """
    if gain == 1.0:
        return data
    if sample_width == 1:
        return _scale_8bit(data, gain)
    if sample_width == 2:
        return _scale_array(data, "h", 2, -32768, 32767, gain)
    if sample_width == 3:
        return _scale_24bit(data, gain)
    if sample_width == 4:
        return _scale_array(data, "i", 4, -2147483648, 2147483647, gain)
    raise AudioError("source_unsupported", f"不支持的采样位宽：{sample_width}")


class AtomicWaveWriter:
    """先写临时文件，全部成功后再原子替换目标，失败时绝不留下半成品。"""

    def __init__(self, target_path: str, sample_rate: int, channels: int,
                 sample_width: int):
        self.target_path = target_path
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self._wf: wave.Wave_write | None = None
        self._tmp_path: str | None = None
        self.frames_written = 0

    def __enter__(self) -> "AtomicWaveWriter":
        parent = os.path.dirname(os.path.abspath(self.target_path)) or "."
        try:
            os.makedirs(parent, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".clipforge-", suffix=".wav.part", dir=parent
            )
            os.close(fd)
        except OSError as exc:
            raise AudioError("output_write_error", f"无法创建输出临时文件：{exc}") from exc
        self._tmp_path = tmp_path
        try:
            self._wf = wave.open(tmp_path, "wb")
            self._wf.setnchannels(self.channels)
            self._wf.setsampwidth(self.sample_width)
            self._wf.setframerate(self.sample_rate)
        except (wave.Error, OSError) as exc:
            self._cleanup_tmp()
            raise AudioError("output_write_error", f"无法写入输出 WAV：{exc}") from exc
        return self

    def write_frames(self, data: bytes, frame_count: int) -> None:
        if self._wf is None:
            raise AudioError("output_write_error", "输出 WAV 尚未打开")
        try:
            self._wf.writeframes(data)
        except (wave.Error, OSError) as exc:
            raise AudioError("output_write_error", f"写入输出 WAV 失败：{exc}") from exc
        self.frames_written += frame_count

    def __exit__(self, exc_type, exc, tb) -> bool:
        wf = self._wf
        self._wf = None
        close_error: Exception | None = None
        if wf is not None:
            try:
                wf.close()
            except (wave.Error, OSError) as err:
                close_error = err
        if exc_type is not None:
            self._cleanup_tmp()
            return False
        if close_error is not None:
            self._cleanup_tmp()
            raise AudioError("output_write_error",
                             f"关闭输出 WAV 失败：{close_error}") from close_error
        try:
            os.replace(self._tmp_path, self.target_path)
        except OSError as err:
            self._cleanup_tmp()
            raise AudioError("output_write_error", f"无法落盘输出文件：{err}") from err
        self._tmp_path = None
        return False

    def _cleanup_tmp(self) -> None:
        if self._tmp_path:
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass
            self._tmp_path = None


DEFAULT_CHUNK_FRAMES = 65536


def iter_frame_chunks(wf: wave.Wave_read, count: int,
                      chunk_frames: int = DEFAULT_CHUNK_FRAMES):
    """分段读取指定帧数，避免长录音片段整体驻留内存。

    产出 (bytes, 帧数)。若源文件实际数据不足，抛出可报告的 AudioError。
    """
    if count < 0:
        raise AudioError("source_read_error", f"请求帧数为负：{count}")
    framesize = wf.getnchannels() * wf.getsampwidth()
    remaining = count
    while remaining > 0:
        n = min(chunk_frames, remaining)
        try:
            data = wf.readframes(n)
        except (wave.Error, EOFError, OSError) as exc:
            raise AudioError("source_read_error", f"读取源 WAV 失败：{exc}") from exc
        expected = n * framesize
        if len(data) != expected:
            raise AudioError(
                "source_truncated",
                f"源 WAV 数据不完整：期望 {expected} 字节，实际读取 {len(data)} 字节",
            )
        yield data, n
        remaining -= n


def verify_riff_structure(path: str) -> dict:
    """独立于 wave 模块校验 RIFF 块长度，确认成片可被标准工具完整读取。"""
    file_size = os.path.getsize(path)
    with open(path, "rb") as f:
        header = f.read(12)
        if len(header) != 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ValueError("不是 RIFF/WAVE 文件")
        riff_size = struct.unpack("<I", header[4:8])[0]
        if riff_size + 8 != file_size:
            raise ValueError(
                f"RIFF 长度字段 {riff_size} + 8 与文件大小 {file_size} 不一致"
            )
        chunks: list[dict] = []
        data_bytes = None
        while f.tell() + 8 <= file_size:
            chunk_id = f.read(4)
            if len(chunk_id) != 4:
                raise ValueError("块头不完整")
            chunk_size = struct.unpack("<I", f.read(4))[0]
            payload_start = f.tell()
            if payload_start + chunk_size > file_size:
                raise ValueError(f"{chunk_id!r} 块声明长度超出文件")
            if chunk_id == b"data":
                data_bytes = chunk_size
            chunks.append({"id": chunk_id.decode("ascii", "replace"), "size": chunk_size})
            f.seek(payload_start + chunk_size + (chunk_size & 1))
        if data_bytes is None:
            raise ValueError("缺少 data 块")
    return {"file_size": file_size, "chunks": chunks, "data_bytes": data_bytes}
