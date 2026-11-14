# -*- coding: utf-8 -*-
"""确定性构造本地样例 WAV 与接缝点清单 manifest.json。

全部数据由固定公式/固定随机种子生成，可重复运行、结果一致。
生成目录结构：
    samples/
      manifest.json        接缝点清单（含 output_dir 与 entries）
      sine_tone.wav        连续正弦（44100/16/单声道，正常条目）
      abrupt_step.wav      中点电平突变（正常条目，接缝得分应 >0.8）
      stereo_tone.wav      立体声 -> 跳过
      low_rate_tone.wav    32000Hz -> 跳过
      float_tone.wav       32 位浮点 -> 跳过
      corrupted.wav        损坏文件（非法 WAV 头）
      short_tone.wav       时长不足 -> 跳过
      silence.wav          全零静音 -> 跳过，得分 1.0
      dup_tone.wav         正常文件，清单中出现两次
      locked_tone.wav      正常文件，用于锁定/重读测试
      （missing_tone.wav 不生成，由清单引用 -> 缺失跳过）
"""

from __future__ import annotations

import json
import os

import numpy as np
import soundfile as sf

try:
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SR = 44100
# 取周期为整数采样（110 采样/周期 ≈ 400.91Hz），使 220 采样的 5ms 窗口
# 恰好覆盖整数个周期，任意位置窗口 RMS 一致 -> 连续接缝得分接近 0。
TONE_PERIOD = 110
AMP = int(0.5 * 32767)


def sine_wave(num_samples: int, amp: int = AMP, period: int = TONE_PERIOD) -> np.ndarray:
    t = np.arange(num_samples)
    return (amp * np.sin(2.0 * np.pi * t / period)).astype(np.int16)


def build(samples_dir: str) -> str:
    os.makedirs(samples_dir, exist_ok=True)

    # 1) 连续正弦 2 秒
    sf.write(os.path.join(samples_dir, "sine_tone.wav"),
             sine_wave(2 * SR), SR, subtype="PCM_16")

    # 2) 中点突变：前 1 秒满幅正弦，后 1 秒静音
    abrupt = np.zeros(2 * SR, dtype=np.int16)
    abrupt[:SR] = sine_wave(SR, amp=int(0.8 * 32767))
    sf.write(os.path.join(samples_dir, "abrupt_step.wav"), abrupt, SR, subtype="PCM_16")

    # 3) 立体声 1 秒
    mono = sine_wave(SR)
    sf.write(os.path.join(samples_dir, "stereo_tone.wav"),
             np.column_stack([mono, mono]), SR, subtype="PCM_16")

    # 4) 32000Hz 单声道 16 位
    sf.write(os.path.join(samples_dir, "low_rate_tone.wav"),
             sine_wave(SR), 32000, subtype="PCM_16")

    # 5) 32 位浮点 WAV
    sf.write(os.path.join(samples_dir, "float_tone.wav"),
             (sine_wave(SR).astype(np.float64) / 32768.0), SR, subtype="FLOAT")

    # 6) 损坏文件：固定种子随机字节，不是合法 RIFF/WAV
    rng = np.random.default_rng(20261114)
    with open(os.path.join(samples_dir, "corrupted.wav"), "wb") as fh:
        fh.write(bytes(rng.integers(0, 256, size=256, dtype=np.uint8)))

    # 7) 时长不足：仅 0.5 秒，清单预期 1 秒
    sf.write(os.path.join(samples_dir, "short_tone.wav"),
             sine_wave(SR // 2), SR, subtype="PCM_16")

    # 8) 全零静音 1 秒
    sf.write(os.path.join(samples_dir, "silence.wav"),
             np.zeros(SR, dtype=np.int16), SR, subtype="PCM_16")

    # 9) 重复条目所用文件（1 秒，预期取 44000 采样 = 400 个完整周期）
    sf.write(os.path.join(samples_dir, "dup_tone.wav"),
             sine_wave(SR), SR, subtype="PCM_16")

    # 10) 锁定测试所用文件
    sf.write(os.path.join(samples_dir, "locked_tone.wav"),
             sine_wave(SR), SR, subtype="PCM_16")

    # 预期长度 44000 采样 = 110 的整数倍，循环首尾相位天然连续
    loop_len_tone = 44000
    entries = [
        # 正常：三个候选接缝得分近似 0 且并列，应选索引 0
        {"file": "sine_tone.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000, 22050, 50000]},
        # 突变：三个候选全部跨在中点突变附近，得分均 >0.8，取真正最低者
        {"file": "abrupt_step.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [44100, 44090, 44110]},
        # 立体声 -> 跳过
        {"file": "stereo_tone.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000]},
        # 采样率不符 -> 跳过
        {"file": "low_rate_tone.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000]},
        # 位深不符（浮点）-> 跳过
        {"file": "float_tone.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000]},
        # 损坏文件 -> 暂时不可读
        {"file": "corrupted.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000]},
        # 预期 1 秒 > 实际 0.5 秒 -> 长度不足
        {"file": "short_tone.wav",
         "expected_loop_length": SR,
         "candidate_seams": [10000]},
        # 静音 -> 得分 1.0 并跳过
        {"file": "silence.wav",
         "expected_loop_length": SR // 2,
         "candidate_seams": [10000]},
        # 同名首次出现 -> 正常导出
        {"file": "dup_tone.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000, 20000]},
        # 同名再次出现 -> 重复跳过
        {"file": "dup_tone.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000]},
        # 锁定测试文件（首次运行前由 lock_helper.py 独占锁定）
        {"file": "locked_tone.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000]},
        # 文件缺失 -> 跳过
        {"file": "missing_tone.wav",
         "expected_loop_length": loop_len_tone,
         "candidate_seams": [10000]},
    ]

    manifest = {"output_dir": "out", "entries": entries}
    manifest_path = os.path.join(samples_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest_path


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    path = build(os.path.join(here, "samples"))
    print("样例清单已生成：%s" % path)
