#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_samples.py
===============

确定性生成测试用 WAV 样本（仅标准库），全部为单声道 16 位 PCM。
不使用随机数，重复运行得到逐字节相同的文件。

生成到 samples/ 目录：
  sample_44k_silence_speech.wav   2.0s 静音 + 3.0s 正弦语音（RMS≈0.2）
  sample_44k_two_speech.wav       1s 静音 + 2s RMS≈0.1 语音 + 1s 静音
                                  + 2s RMS≈0.2 语音 + 1s 静音
  sample_44k_need_gain3.wav       1s 静音 + 2s RMS≈0.1 + 1s 静音
                                  + 2s RMS≈0.2 + 1s 静音（同 two_speech，
                                  用于验证增益上限 2.0 与最终 RMS≈0.2）
  sample_44k_gain_clamp.wav     1s 静音 + 2s RMS≈0.1 语音（含 5 个 32000
                                尖峰采样）+ 1s 静音 + 2s RMS≈0.5 语音
                                + 1s 静音；中位数目标 0.3，第一段需 3 倍增益
                                被截断为 2.0，尖峰被限制到 32767
  sample_44k_all_silence.wav      6.0s 全静音
  sample_44k_short_speech.wav     1s 静音 + 0.05s RMS≈0.2 短语音 + 1s 静音
                                  + 2s RMS≈0.1 长语音 + 1s 静音（中位数 0.15，
                                  仅长语音以 1.5 倍增益调整）
  sample_8k.wav                   8kHz 格式不符样本（内容与 44k 静音语音相同）

用法：
    python make_samples.py [输出目录]
"""

import array
import math
import os
import sys
import wave

RATE = 44100
FULL_SCALE = 32768.0
TONE_HZ = 220.0  # 确定性正弦频率


def sine_segment(duration_seconds, amplitude):
    """生成指定时长、指定峰值幅度的正弦波采样（array('h')）。"""
    n = int(round(duration_seconds * RATE))
    out = array.array("h")
    for i in range(n):
        value = amplitude * math.sin(2.0 * math.pi * TONE_HZ * i / RATE)
        out.append(int(round(value)))
    return out


def silence(duration_seconds):
    return array.array("h", [0]) * int(round(duration_seconds * RATE))


def write_wav(path, samples, rate=RATE):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def constant_segment(duration_seconds, amplitude):
    """恒定电平片段：amplitude 为原始整数幅度，RMS 精确等于 amplitude/32768，
    不受时长与周期整除影响。"""
    n = int(round(duration_seconds * RATE))
    return array.array("h", [int(round(amplitude))]) * n


def concat(*parts):
    out = array.array("h")
    for part in parts:
        out.extend(part)
    return out


def spiked_sine(duration_seconds, base_amplitude, spike_amplitude=32000,
                spike_count=5):
    """低幅正弦波中插入若干近满幅尖峰采样（确定性等间距），用于溢出截断验证。"""
    out = sine_segment(duration_seconds, base_amplitude)
    step = max(1, len(out) // (spike_count + 1))
    for k in range(1, spike_count + 1):
        out[k * step] = spike_amplitude
    return out


def main(out_dir="samples"):
    # 正弦满幅 RMS = 峰值 / sqrt(2)
    amp_01 = int(round(0.1 * math.sqrt(2.0) * FULL_SCALE))  # 正弦 RMS≈0.1
    amp_02 = int(round(0.2 * math.sqrt(2.0) * FULL_SCALE))  # 正弦 RMS≈0.2
    amp_05 = int(round(0.5 * math.sqrt(2.0) * FULL_SCALE))  # 正弦 RMS≈0.5
    # 恒定电平的 RMS 即电平本身，要得到 RMS 0.2 直接取 0.2*32768
    const_amp_02 = int(round(0.2 * FULL_SCALE))

    samples = {
        "sample_44k_silence_speech.wav": concat(
            silence(2.0), sine_segment(3.0, amp_02)),
        "sample_44k_two_speech.wav": concat(
            silence(1.0), sine_segment(2.0, amp_01), silence(1.0),
            sine_segment(2.0, amp_02), silence(1.0)),
        "sample_44k_gain_clamp.wav": concat(
            silence(1.0), spiked_sine(2.0, amp_01), silence(1.0),
            sine_segment(2.0, amp_05), silence(1.0)),
        "sample_44k_all_silence.wav": silence(6.0),
        "sample_44k_short_speech.wav": concat(
            silence(1.0), constant_segment(0.05, const_amp_02), silence(1.0),
            sine_segment(2.0, amp_01), silence(1.0)),
    }
    for name, data in samples.items():
        write_wav(os.path.join(out_dir, name), data, RATE)

    # 8kHz 格式不符样本：0.5s 静音 + 1s 语音，按 8000Hz 写头
    data_8k = array.array("h")
    data_8k.extend(silence(0.5))
    n = 8000
    for i in range(n):
        data_8k.append(int(round(amp_02 * math.sin(2 * math.pi * 220 * i / 8000))))
    write_wav(os.path.join(out_dir, "sample_8k.wav"), data_8k, 8000)

    for name in sorted(os.listdir(out_dir)):
        if name.endswith(".wav"):
            print("generated:", os.path.join(out_dir, name))


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "samples"
    main(target)
