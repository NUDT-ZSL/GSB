# -*- coding: utf-8 -*-
"""WAV 底层读写与响度处理工具（仅使用 Python 标准库）。

约束：仅支持 16 位 PCM、双声道、采样率 8000~48000Hz 的 WAV 文件。
所有数值计算均以 16 位有符号整数域 [-32768, 32767] 为准。
"""
from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass

# 16 位 PCM 满量程峰值（与格式的整数域保持一致）
FULL_SCALE = 32768.0
INT16_MIN = -32768
INT16_MAX = 32767


class WavError(Exception):
    """WAV 读取/格式相关错误，message 为面向用户的中文原因。"""


@dataclass
class WavData:
    """内存中的一段 WAV 数据。

    samples 为扁平的 16 位有符号整数列表，
    双声道按 [左样本0, 右样本0, 左样本1, 右样本1, ...] 交错排列。
    """

    sample_rate: int
    channels: int
    sampwidth: int
    frames: int
    samples: list[int]

    @property
    def duration(self) -> float:
        """时长（秒）。"""
        return self.frames / self.sample_rate


def read_wav(path: str) -> WavData:
    """读取并校验 WAV 文件。

    任何不符合"16位PCM双声道、8000~48000Hz"约束的情况，
    都抛出带中文原因的 WavError（错误中包含"格式不支持"）。
    """
    try:
        with wave.open(path, "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            sample_rate = wf.getframerate()
            frames = wf.getnframes()
            raw = wf.readframes(frames)
    except wave.Error as exc:
        raise WavError("格式不支持：文件不是可解析的 WAVE PCM 音频（%s）" % exc) from exc
    except EOFError as exc:
        raise WavError("格式不支持：WAV 文件数据不完整或已损坏（%s）" % exc) from exc

    # 逐项给出具体原因，便于在报告中区分
    problems = []
    if sampwidth != 2:
        problems.append("位深为 %d 位（要求 16 位 PCM）" % (sampwidth * 8))
    if channels != 2:
        problems.append("声道数为 %d（要求双声道/立体声）" % channels)
    if not (8000 <= sample_rate <= 48000):
        problems.append("采样率为 %dHz（要求在 8000 至 48000Hz 之间）" % sample_rate)
    if problems:
        raise WavError("格式不支持：" + "；".join(problems))

    expected = frames * channels * sampwidth
    if len(raw) != expected:
        raise WavError(
            "格式不支持：WAV 数据块长度异常（期望 %d 字节，实际 %d 字节），文件可能已损坏"
            % (expected, len(raw))
        )

    try:
        samples = list(struct.unpack("<%dh" % (frames * channels), raw))
    except struct.error as exc:
        raise WavError("格式不支持：PCM 样本解析失败（%s）" % exc) from exc

    return WavData(sample_rate, channels, sampwidth, frames, samples)


def write_wav(path: str, data: WavData) -> None:
    """按 data 自身的采样率/位深/声道数写出 WAV，不改变 PCM 格式。"""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(data.channels)
        wf.setsampwidth(data.sampwidth)
        wf.setframerate(data.sample_rate)
        frames = len(data.samples) // data.channels
        wf.writeframes(struct.pack("<%dh" % len(data.samples), *data.samples))
        # 以实际写入帧数为准，保持对象自洽
        data.frames = frames


def rms_dbfs(samples: list[int]) -> float:
    """计算 RMS 响度（dBFS），以 16 位满量程 32768 为基准。

    全静音（能量为 0）返回负无穷，由调用方决定如何处理。
    """
    if not samples:
        return float("-inf")
    total = 0
    for s in samples:
        total += s * s
    rms = math.sqrt(total / len(samples))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms / FULL_SCALE)


def peak_abs(samples: list[int]) -> int:
    """返回样本的最大绝对值（含 -32768）。"""
    peak = 0
    for s in samples:
        a = -s if s < 0 else s
        if a > peak:
            peak = a
    return peak


def apply_gain(samples: list[int], target_dbfs: float) -> tuple[list[int], list[str]]:
    """对片段做 RMS 增益对齐。

    目标响度 target_dbfs 范围 -40~0 dBFS。
    若按目标计算出的增益会使峰值超过满量程，则以不削波方式回退增益，
    并追加一条中文警告。返回 (新样本列表, 警告列表)。
    """
    warnings: list[str] = []

    total = 0
    for s in samples:
        total += s * s
    rms = math.sqrt(total / len(samples)) if samples else 0.0
    if rms <= 0.0:
        # 无法对全静音片段计算增益，交由上层按失败处理
        raise WavError("片段为全静音，RMS 为 0，无法计算增益对齐")

    target_rms = FULL_SCALE * (10.0 ** (target_dbfs / 20.0))
    gain = target_rms / rms

    src_peak = peak_abs(samples)
    # 峰值保护：放大后任何样本都不得超出 16 位整数域
    if gain * src_peak > FULL_SCALE - 1.0:
        safe_gain = (FULL_SCALE - 1.0) / src_peak if src_peak > 0 else gain
        warnings.append(
            "目标响度 %.3f dBFS 会导致峰值超过 0 dBFS，已将增益由 %.4f 回退至 %.4f 以避免削波"
            % (target_dbfs, gain, safe_gain)
        )
        gain = safe_gain

    out: list[int] = []
    lo, hi = INT16_MIN, INT16_MAX
    for s in samples:
        # round 为银行家舍入，确定性且无偏；再做一次钳制保证绝不削波
        v = int(round(s * gain))
        if v < lo:
            v = lo
        elif v > hi:
            v = hi
        out.append(v)

    # 写出前复核：输出峰值必须不超过满量程
    if peak_abs(out) >= FULL_SCALE:
        raise WavError("内部错误：增益处理后峰值达到满量程，存在削波风险")
    return out, warnings


def slice_samples(samples: list[int], channels: int, start_frame: int, end_frame: int) -> list[int]:
    """按帧（每帧 channels 个交错样本）切片。"""
    return samples[start_frame * channels:end_frame * channels]
