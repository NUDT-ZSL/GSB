"""确定性 WAV 样本构造与读取辅助，仅用于测试与演示。"""

from __future__ import annotations

import array
import math
import os
import shutil
import sys
import wave
from pathlib import Path


def ramp_frames(frames: int, channels: int, sample_width: int,
                seed: int = 0) -> list[int]:
    """生成确定性、可辨认的 PCM 样本值（有符号原始值）。

    每个声道的样本都是唯一的，便于验证裁剪区间与左右声道独立性。
    """
    limit = (1 << (sample_width * 8 - 1)) - 1
    amp = max(1, limit // 4)
    values: list[int] = []
    for i in range(frames):
        for ch in range(channels):
            base = ((i * (ch + 1) * 37 + ch * 101 + seed * 17) % (2 * amp)) - amp
            values.append(base)
    return values


def encode(values: list[int], sample_width: int) -> bytes:
    if sample_width == 1:
        return bytes(max(0, min(255, value + 128)) for value in values)
    if sample_width == 3:
        out = bytearray()
        for value in values:
            out += (value & 0xFFFFFF).to_bytes(3, "little")
        return bytes(out)
    typecode = "h" if sample_width == 2 else "i"
    arr = array.array(typecode, values)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def decode(data: bytes, sample_width: int) -> list[int]:
    if sample_width == 1:
        return [byte - 128 for byte in data]
    if sample_width == 3:
        out = []
        for offset in range(0, len(data), 3):
            value = data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)
            out.append(value - 0x1000000 if value & 0x800000 else value)
        return out
    typecode = "h" if sample_width == 2 else "i"
    arr = array.array(typecode)
    arr.frombytes(data)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tolist()


def write_wav(path: os.PathLike | str, sample_rate: int, channels: int,
              sample_width: int, frames: int, seed: int = 0) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = ramp_frames(frames, channels, sample_width, seed=seed)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(encode(values, sample_width))


def write_wav_from_values(path: os.PathLike | str, sample_rate: int,
                          channels: int, sample_width: int,
                          values: list[int]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(encode(values, sample_width))


def read_frames(path: os.PathLike | str) -> tuple[dict, list[int]]:
    with wave.open(str(path), "rb") as wf:
        params = {
            "channels": wf.getnchannels(),
            "sample_rate": wf.getframerate(),
            "sample_width": wf.getsampwidth(),
            "frames": wf.getnframes(),
        }
        data = wf.readframes(wf.getnframes())
    return params, decode(data, params["sample_width"])


def frame_at(values: list[int], channels: int, frame: int) -> list[int]:
    start = frame * channels
    return values[start:start + channels]


def frame_span(values: list[int], channels: int, start: int, end: int) -> list[int]:
    return values[start * channels:end * channels]


def clean_dir(path: os.PathLike | str) -> None:
    """测试前清空临时目录，保证可复现。"""
    target = Path(path)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def write_non_pcm_wav(path: os.PathLike | str, sample_rate: int = 8000,
                      frames: int = 100) -> None:
    """写一个 IEEE float (format tag 3) 的 WAV，模拟非 PCM 输入。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(struct_float(i / 100.0) for i in range(frames))
    fmt = (
        (3).to_bytes(2, "little")      # wFormatTag = WAVE_FORMAT_IEEE_FLOAT
        + (1).to_bytes(2, "little")    # channels
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * 4).to_bytes(4, "little")
        + (4).to_bytes(2, "little")    # block align
        + (32).to_bytes(2, "little")   # bits per sample
    )
    body = b"WAVE" + b"fmt " + len(fmt).to_bytes(4, "little") + fmt
    body += b"data" + len(data).to_bytes(4, "little") + data
    path.write_bytes(b"RIFF" + len(body).to_bytes(4, "little") + body)


def struct_float(value: float) -> bytes:
    import struct
    return struct.pack("<f", value)


def expected_gain_sample(value: int, gain: float, sample_width: int) -> int:
    """与实现一致的参考增益计算：0.5 远离 0 取整 + 饱和裁剪。"""
    scaled = math.floor(value * gain + 0.5) if value * gain >= 0 else -math.floor(
        -(value * gain) + 0.5)
    limit = 1 << (sample_width * 8 - 1)
    return max(-limit, min(limit - 1, scaled))
