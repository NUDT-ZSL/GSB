"""WAV 配乐读取与格式校验。

支持范围（需求 1）::

    声道数: 1（单声道），双声道及多声道明确拒绝
    位深  : 8 / 16 / 24 / 32 bit PCM（sample width 1~4 字节）
    采样率: 8000 ~ 48000 Hz
    编码  : 无压缩 PCM（WAVE_FORMAT_PCM，wave 模块报告为 NONE）

不使用第三方音频库，也不依赖 Python 3.13 起已移除的 ``audioop``，
PCM 解码全部基于标准库 ``array`` 实现，可完全离线运行。
"""

from __future__ import annotations

import array
import os
import sys
import wave
from dataclasses import dataclass

from .errors import AudioReadError, UnsupportedAudioError

#: 支持的采样率闭区间
SAMPLE_RATE_RANGE = (8000, 48000)
#: sample width 字节数 -> 位深
SUPPORTED_SAMPLE_WIDTHS = {1: 8, 2: 16, 3: 24, 4: 32}
SUPPORTED_CHANNELS = (1,)


@dataclass(frozen=True)
class AudioInfo:
    """加载完成的音频数据与元信息。"""

    path: str
    sample_rate: int
    sample_width: int
    bits_per_sample: int
    channels: int
    frame_count: int
    duration_ms: int
    samples: list[float]
    """归一化到 ``[-1.0, 1.0]`` 的单声道采样序列。"""

    def describe(self) -> str:
        return (
            f"{self.sample_rate} Hz / {self.bits_per_sample} bit / "
            f"{self.channels} 声道 / {self.duration_ms} ms"
        )


def load_wav(path: str) -> AudioInfo:
    """读取 WAV 并完成全部格式校验。

    抛出:
        AudioReadError: 文件无法打开、不是合法 RIFF/WAVE、数据帧损坏等。
        UnsupportedAudioError: 格式可解析但不在支持范围内（如双声道）。
    """
    if not os.path.exists(path):
        raise AudioReadError(f"配乐文件不存在，无法读取：{path}")
    if os.path.isdir(path):
        raise AudioReadError(f"配乐路径是目录而不是 WAV 文件：{path}")

    try:
        reader = wave.open(path, "rb")
    except wave.Error as exc:  # 例如 "file does not start with RIFF id"
        raise AudioReadError(f"配乐读取失败，文件不是合法 WAV：{path}（{exc}）") from exc
    except (OSError, RuntimeError) as exc:
        # 残缺 RIFF 块会让 wave 内部 seek 抛出裸 RuntimeError
        raise AudioReadError(f"配乐读取失败，WAV 结构已损坏：{path}（{exc}）") from exc

    try:
        with reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
            compression = reader.getcomptype()
            try:
                raw = reader.readframes(frame_count)
            except (wave.Error, OSError) as exc:
                raise AudioReadError(
                    f"配乐读取失败，音频数据帧损坏或被截断：{path}（{exc}）"
                ) from exc
    except AudioReadError:
        raise
    except (wave.Error, OSError) as exc:
        raise AudioReadError(f"配乐读取失败，WAV 结构已损坏：{path}（{exc}）") from exc

    # --- 格式支持范围校验，逐条给出可读原因 -------------------------------
    if compression != "NONE":
        raise UnsupportedAudioError(
            f"不支持的音频编码：{compression}，本工具仅支持无压缩 PCM WAV（文件：{path}）"
        )

    if channels not in SUPPORTED_CHANNELS:
        raise UnsupportedAudioError(
            f"不支持 {channels} 声道音频：本工具仅支持单声道 WAV，"
            f"请先将配乐混音为单声道后重试（文件：{path}）"
        )

    if sample_width not in SUPPORTED_SAMPLE_WIDTHS:
        raise UnsupportedAudioError(
            f"不支持的位深：sample width={sample_width} 字节，"
            f"本工具仅支持 8/16/24/32 bit PCM（文件：{path}）"
        )

    low, high = SAMPLE_RATE_RANGE
    if not low <= sample_rate <= high:
        raise UnsupportedAudioError(
            f"不支持的采样率 {sample_rate} Hz，允许范围为 {low}~{high} Hz（文件：{path}）"
        )

    if frame_count <= 0 or not raw:
        raise AudioReadError(f"配乐读取失败：音频中没有任何采样数据（文件：{path}）")

    try:
        samples = _decode_pcm(raw, sample_width)
    except (ValueError, EOFError) as exc:
        raise AudioReadError(f"配乐读取失败，PCM 数据无法解码：{path}（{exc}）") from exc

    # 数据帧被截断时实际采样数可能少于声明值，以实际可用数据为准
    if len(samples) > frame_count:
        samples = samples[:frame_count]
    if not samples:
        raise AudioReadError(f"配乐读取失败：解码后没有任何有效采样（文件：{path}）")

    duration_ms = round(len(samples) * 1000 / sample_rate)
    return AudioInfo(
        path=path,
        sample_rate=sample_rate,
        sample_width=sample_width,
        bits_per_sample=SUPPORTED_SAMPLE_WIDTHS[sample_width],
        channels=channels,
        frame_count=len(samples),
        duration_ms=duration_ms,
        samples=samples,
    )


def _decode_pcm(raw: bytes, sample_width: int) -> list[float]:
    """把原始 PCM 字节解码为 ``[-1, 1]`` 浮点采样（单声道，小端）。"""
    native_big_endian = sys.byteorder != "little"

    if sample_width == 1:
        # 8-bit PCM 是无符号整数，中心 128
        data = array.array("B")
        data.frombytes(raw)
        return [(v - 128) / 128.0 for v in data]

    if sample_width == 2:
        data = array.array("h")  # 16-bit 有符号，WAV 为小端
        data.frombytes(raw)
        if native_big_endian:
            data.byteswap()
        return [v / 32768.0 for v in data]

    if sample_width == 4:
        data = array.array("i")  # 32-bit 有符号，WAV 为小端
        data.frombytes(raw)
        if native_big_endian:
            data.byteswap()
        return [v / 2147483648.0 for v in data]

    # 24-bit 小端有符号：标准库没有对应 array 类型，逐样本做补码符号扩展
    step = 3
    count = len(raw) // step
    out: list[float] = []
    append = out.append
    for i in range(count):
        base = i * step
        value = raw[base] | (raw[base + 1] << 8) | (raw[base + 2] << 16)
        if value & 0x800000:  # 符号位为 1，扩展为负整数
            value -= 0x1000000
        append(value / 8388608.0)
    return out
