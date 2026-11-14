# -*- coding: utf-8 -*-
"""确定性生成校准工具所需的本地样例（WAV + 时序 JSON + 校准清单）。

所有样本由代码确定性生成，无需音频设备或网络。
覆盖场景：
  fireball   正常技能：100ms 静音后起音，标称 50ms，3 个合法关键帧 -> 完整
  frostbolt  全静音 WAV -> 不可用
  slash      时序 JSON 缺失 -> 时序缺失（仍记录音效起音）
  thunder    实际 44.1kHz，清单声明标准 48kHz -> 记录实际采样率并注明需重采样
  quake      5 个关键帧中 2 个坐标越界 -> 部分完成，保留 3 张合法帧
  zerorate   WAV 采样率为 0 -> 不可用（采样率不合法）
  truncated  WAV 文件头声明长度大于实际文件大小 -> 按实际可读采样计算起音
  tinydev    标称与实测起音相差 0.4ms -> 偏差为 0
  （另有一条缺少 wav 字段的清单条目 -> 清单条目无效，不影响其他技能）
"""

import json
import struct
from pathlib import Path

import numpy as np

SAMPLES_DIR = Path(__file__).parent / "samples"
STANDARD_RATE = 48000


def tone(rate, silence_ms, duration_ms=300, freq=440.0, amp=12000, seed_phase=0.0):
    """生成 16-bit PCM 采样：前 silence_ms 毫秒静音，之后为正弦音。"""
    n_silence = int(round(rate * silence_ms / 1000.0))
    n_tone = int(round(rate * duration_ms / 1000.0))
    t = np.arange(n_tone) / rate
    wave = (amp * np.sin(2 * np.pi * freq * t + seed_phase)).astype("<i2")
    return np.concatenate([np.zeros(n_silence, dtype="<i2"), wave])


def write_wav(path, samples, rate, declared_data_size=None):
    """手写 PCM WAV。declared_data_size 可故意写大，模拟文件头长度与实际不符。"""
    data = np.asarray(samples, dtype="<i2").tobytes()
    data_size = len(data) if declared_data_size is None else declared_data_size
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
    fmt = (b"fmt " + struct.pack("<I", 16)
           + struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16))
    data_chunk = b"data" + struct.pack("<I", data_size) + data
    Path(path).write_bytes(header + fmt + data_chunk)


def write_timing(path, nominal_onset_ms, keyframes, fps=60):
    payload = {
        "nominal_onset_ms": nominal_onset_ms,
        "fps": fps,
        "keyframes": keyframes,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def main():
    SAMPLES_DIR.mkdir(exist_ok=True)
    s = SAMPLES_DIR

    # 1) fireball：100ms 静音，标称 50ms，48kHz，3 个合法关键帧 -> 完整
    write_wav(s / "fireball.wav", tone(STANDARD_RATE, 100), STANDARD_RATE)
    write_timing(s / "fireball_timing.json", 50, [
        {"frame": 0, "x": 40, "y": 90},
        {"frame": 1, "x": 160, "y": 60},
        {"frame": 2, "x": 280, "y": 120},
    ])

    # 2) frostbolt：全静音 -> 不可用
    write_wav(s / "frostbolt.wav", np.zeros(STANDARD_RATE // 2, dtype="<i2"),
              STANDARD_RATE)
    write_timing(s / "frostbolt_timing.json", 0, [{"frame": 0, "x": 10, "y": 10}])

    # 3) slash：音效正常，但时序 JSON 不存在 -> 时序缺失
    write_wav(s / "slash.wav", tone(STANDARD_RATE, 30), STANDARD_RATE)

    # 4) thunder：实际 44.1kHz，清单声明 48kHz -> 需重采样，起音按 44.1kHz 算
    write_wav(s / "thunder.wav", tone(44100, 80, freq=220.0), 44100)
    write_timing(s / "thunder_timing.json", 80, [
        {"frame": 0, "x": 100, "y": 100},
        {"frame": 1, "x": 200, "y": 80},
    ])

    # 5) quake：5 帧中 2 帧坐标越界 -> 部分完成，保留 3 张 PNG
    write_wav(s / "quake.wav", tone(STANDARD_RATE, 20, freq=110.0), STANDARD_RATE)
    write_timing(s / "quake_timing.json", 20, [
        {"frame": 0, "x": 50, "y": 50},
        {"frame": 1, "x": 400, "y": 100},   # 越界 (x >= 320)
        {"frame": 2, "x": 120, "y": 140},
        {"frame": 3, "x": -5, "y": 60},     # 越界 (x < 0)
        {"frame": 4, "x": 300, "y": 170},
    ])

    # 6) zerorate：采样率为 0 -> 不可用（采样率不合法）
    write_wav(s / "zerorate.wav", tone(STANDARD_RATE, 0, duration_ms=50), 0)

    # 7) truncated：文件头声明 data 长度大于实际文件大小 -> 按实际可读采样计算
    real = tone(STANDARD_RATE, 60, duration_ms=200)
    truncated = real[: int(STANDARD_RATE * 0.15)]  # 只保留约 150ms 数据
    write_wav(s / "truncated.wav", truncated, STANDARD_RATE,
              declared_data_size=len(real.tobytes()))  # 头部仍声明完整长度
    write_timing(s / "truncated_timing.json", 60, [{"frame": 0, "x": 160, "y": 90}])

    # 8) tinydev：起音相差 0.4ms -> 偏差为 0
    #    48kHz 下 19 个采样 = 0.3958ms，标称 0ms -> |偏差| < 1ms -> 0
    samples = np.concatenate([np.zeros(19, dtype="<i2"),
                              (8000 * np.sin(2 * np.pi * 440
                                             * np.arange(4800) / STANDARD_RATE))
                              .astype("<i2")])
    write_wav(s / "tinydev.wav", samples, STANDARD_RATE)
    write_timing(s / "tinydev_timing.json", 0, [{"frame": 0, "x": 30, "y": 30}])

    manifest = {
        "standard_sample_rate": STANDARD_RATE,
        "output_dir": "output",
        "skills": [
            {"name": "fireball", "wav": "samples/fireball.wav",
             "timing": "samples/fireball_timing.json", "nominal_onset_ms": 50},
            {"name": "frostbolt", "wav": "samples/frostbolt.wav",
             "timing": "samples/frostbolt_timing.json", "nominal_onset_ms": 0},
            {"name": "slash", "wav": "samples/slash.wav",
             "timing": "samples/slash_timing.json", "nominal_onset_ms": 30},
            {"name": "thunder", "wav": "samples/thunder.wav",
             "timing": "samples/thunder_timing.json", "nominal_onset_ms": 80},
            {"name": "quake", "wav": "samples/quake.wav",
             "timing": "samples/quake_timing.json", "nominal_onset_ms": 20},
            {"name": "zerorate", "wav": "samples/zerorate.wav",
             "timing": "samples/zerorate_timing.json", "nominal_onset_ms": 0},
            {"name": "truncated", "wav": "samples/truncated.wav",
             "timing": "samples/truncated_timing.json", "nominal_onset_ms": 60},
            {"name": "tinydev", "wav": "samples/tinydev.wav",
             "timing": "samples/tinydev_timing.json", "nominal_onset_ms": 0},
            # 缺少 wav 字段的条目：不中止整体流程，总报告中记录
            {"name": "broken_entry", "timing": "samples/fireball_timing.json",
             "nominal_onset_ms": 0},
        ],
    }
    Path("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"样例已生成到 {SAMPLES_DIR}/，清单为 manifest.json")


if __name__ == "__main__":
    main()
