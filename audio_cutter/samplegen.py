"""确定性样本生成与剪辑点构造（无随机数，重复运行字节一致）。

信号设计：
- 基波 440Hz 正弦 + 0.3 幅度的 880Hz 谐波，按时间段施加固定幅度包络；
- 立体声右通道 = 左通道 * 0.55（固定电平差，用于验证共用增益）；
- 3.0s-4.0s 为极弱音段（增益将触达 10 倍上限）；
- 2.0s-3.0s 为较强音段（增益将触达 0.1 倍下限）。
"""

from __future__ import annotations

import array
import json
import math
import os
import wave
from typing import Any, Dict, List

SAMPLE_WIDTH = 2
DURATION_SECONDS = 6.0
F1 = 440.0
F2 = 880.0
FSQUARE = 220.0
HARMONIC_RATIO = 0.3
RIGHT_GAIN = 0.55  # 右声道相对左声道的固定比例
SQUARE_SEGMENT = (2.0, 3.0)  # 强音段叠加满幅方波，确保 RMS 触发增益下限

# (起点秒, 终点秒, 幅度)
# 强音段(2-3s)另叠加 220Hz 满幅方波，淡变后 RMS ≈ -2.9dBFS：
#   目标 -24dBFS 时所需增益 ≈ 0.088 < 0.1（下限截断）
# 极弱段(3-4s)幅度 0.0008，淡变后 RMS ≈ -66dBFS：
#   目标 -20dBFS 时所需增益 ≈ 210 > 10（上限截断）
AMPLITUDE_ENVELOPE = (
    (0.0, 1.0, 0.30),
    (1.0, 2.0, 0.15),
    (2.0, 3.0, 1.4),     # 强音段（叠加方波）：-24dBFS 目标下增益被下限截断
    (3.0, 4.0, 0.0008),  # 极弱音段：归一化增益被上限 10 截断
    (4.0, 5.0, 0.25),
    (5.0, 6.0, 0.35),
)


def _clamp_int16(x: float) -> int:
    q = int(x + 0.5) if x >= 0.0 else int(x - 0.5)
    if q > 32767:
        return 32767
    if q < -32768:
        return -32768
    return q


def _envelope_amplitude(t: float) -> float:
    for start, end, amp in AMPLITUDE_ENVELOPE:
        if start <= t < end:
            return amp
    return AMPLITUDE_ENVELOPE[-1][2]


def synthesize_frames(sample_rate: int, channels: int,
                      duration: float = DURATION_SECONDS) -> array.array:
    """生成确定性的交错 int16 样本。"""
    n_frames = int(round(sample_rate * duration))
    frames = array.array("h")
    two_pi = 2.0 * math.pi
    for n in range(n_frames):
        t = n / sample_rate
        amp = _envelope_amplitude(t)
        value = amp * (
            math.sin(two_pi * F1 * t)
            + HARMONIC_RATIO * math.sin(two_pi * F2 * t)
        )
        # 强音段叠加固定 220Hz 满幅方波（确定性），把该段 RMS 推高，
        # 使归一化所需衰减稳定低于 0.1 增益下限
        if SQUARE_SEGMENT[0] <= t < SQUARE_SEGMENT[1]:
            value += 0.85 * (1.0 if math.sin(two_pi * FSQUARE * t) >= 0
                             else -1.0)
        left = _clamp_int16(value * 32767.0)
        if channels == 1:
            frames.append(left)
        else:
            right = _clamp_int16(value * 32767.0 * RIGHT_GAIN)
            frames.append(left)
            frames.append(right)
    return frames


def write_pcm_wav(path: str, frames: array.array, sample_rate: int,
                  channels: int, sample_width: int = SAMPLE_WIDTH) -> None:
    """写 WAV；sample_width 可指定 1 用于构造非法样本。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if sample_width == 2:
        payload = frames
    elif sample_width == 1:
        # 8 位 PCM 为无符号、中点 128
        payload = array.array("B", (
            min(255, max(0, (v >> 8) + 128)) for v in frames
        ))
    else:
        raise ValueError("示例生成器仅支持 1/2 字节位深")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(payload.tobytes())


def build_valid_clips() -> List[Dict[str, Any]]:
    """合法剪辑点：含极短片段 c004 与裁后为空的 c007。"""
    return [
        {"id": "c001", "start": 0.20, "end": 1.00},   # 普通段 0.30
        {"id": "c002", "start": 1.10, "end": 1.90},   # 普通段 0.15
        {"id": "c003", "start": 2.10, "end": 2.90},   # 强音段 0.95 -> 增益下限
        # 0.015s 极短片段：0.01s 淡入超过其一半（7.5ms），需自动缩短
        {"id": "c004", "start": 5.20, "end": 5.215},
        {"id": "c005", "start": 3.10, "end": 3.90},   # 极弱段 0.0008 -> 增益上限
        {"id": "c006", "start": 5.10, "end": 5.90},
        # 5 微秒：通过 JSON 校验（start<end 且未越界），但按帧取整后为空
        {"id": "c007", "start": 5.0, "end": 5.000005},
    ]


def build_invalid_clips() -> List[Dict[str, Any]]:
    """含越界与倒置片段，用于任务 1 验收。"""
    return [
        {"id": "c101", "start": 5.50, "end": 6.50},  # 终点越界（时长 6s）
        {"id": "c102", "start": 2.00, "end": 1.50},  # 起点大于终点
        {"id": "c103", "start": 0.30, "end": 0.90},  # 合法对照片段
    ]


def _dump_json(path: str, items: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"clips": items}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_all(workdir: str) -> Dict[str, str]:
    """在 workdir 下确定性生成全部样本与剪辑点，返回路径字典。"""
    samples = os.path.join(workdir, "samples")
    clips_dir = os.path.join(workdir, "clips")

    mono_44100 = os.path.join(samples, "source_44100_mono.wav")
    stereo_48000 = os.path.join(samples, "source_48000_stereo.wav")
    invalid_8bit = os.path.join(samples, "invalid_8bit_48k.wav")
    invalid_32k = os.path.join(samples, "invalid_32k_16bit.wav")

    write_pcm_wav(
        mono_44100, synthesize_frames(44100, 1), 44100, 1)
    write_pcm_wav(
        stereo_48000, synthesize_frames(48000, 2), 48000, 2)
    # 非法样本：8 位 / 32kHz
    write_pcm_wav(
        invalid_8bit, synthesize_frames(48000, 1, duration=1.0),
        48000, 1, sample_width=1)
    write_pcm_wav(
        invalid_32k, synthesize_frames(32000, 1, duration=1.0),
        32000, 1, sample_width=2)

    valid_json = os.path.join(clips_dir, "valid_clips.json")
    invalid_json = os.path.join(clips_dir, "invalid_clips.json")
    _dump_json(valid_json, build_valid_clips())
    _dump_json(invalid_json, build_invalid_clips())

    return {
        "mono_44100": mono_44100,
        "stereo_48000": stereo_48000,
        "invalid_8bit": invalid_8bit,
        "invalid_32k": invalid_32k,
        "valid_clips": valid_json,
        "invalid_clips": invalid_json,
    }
