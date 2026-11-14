# -*- coding: utf-8 -*-
"""make_samples.py — 确定性生成验收用小型 WAV 与标注清单 manifest.json。

不依赖 numpy，全部样本由固定公式生成，重复运行字节完全一致。
生成内容（samples/ 目录）：
  sine_a.wav     44.1kHz 单声道 16bit，循环区间 [10000,20000)，接缝跳变量已知=1000
  sine_b.wav     44.1kHz 单声道 16bit，循环区间 [5000,15000)，接缝跳变量已知=500
  stereo_c.wav   48kHz 立体声 16bit，循环区间 [2000,8000)，接缝跳变量已知=2000
  mismatch.wav   实际 44.1kHz，清单故意声明 22050Hz（参数不符 → 错误清单）
  corrupt.wav    随机垃圾字节（损坏 → 错误清单）
  manifest.json  标注清单（含无效区间、重复 id 等验收用例）
"""
import json
import math
import struct
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "samples"


def write_wav(path, rate, channels, frames):
    """frames: list of per-frame tuples（单声道为单元素元组），16bit PCM。"""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<" + "h" * channels, *f) for f in frames))


def sine_frames(rate, nframes, freq, amp, channels=1):
    out = []
    for i in range(nframes):
        v = int(round(amp * math.sin(2 * math.pi * freq * i / rate)))
        out.append(tuple(v for _ in range(channels)))
    return out


def main():
    SAMPLES.mkdir(exist_ok=True)

    # sine_a：平滑正弦，循环 [10000,20000) 内强制首帧=0、末帧=1000 → 跳变量 1000
    frames = sine_frames(44100, 44100, 440, 8000)
    for i in range(10000, 20000):
        frames[i] = (0,)
    frames[10000] = (0,)
    frames[19999] = (1000,)
    write_wav(SAMPLES / "sine_a.wav", 44100, 1, frames)

    # sine_b：循环 [5000,15000) 内首帧=0、末帧=500 → 跳变量 500
    frames = sine_frames(44100, 44100, 220, 6000)
    for i in range(5000, 15000):
        frames[i] = (0,)
    frames[14999] = (500,)
    write_wav(SAMPLES / "sine_b.wav", 44100, 1, frames)

    # stereo_c：循环 [2000,8000) 内首帧=(0,0)、末帧=(2000,-2000) → 跳变量 2000
    frames = sine_frames(48000, 24000, 330, 5000, channels=2)
    for i in range(2000, 8000):
        frames[i] = (0, 0)
    frames[7999] = (2000, -2000)
    write_wav(SAMPLES / "stereo_c.wav", 48000, 2, frames)

    # mismatch：实际是 44.1kHz，清单将声明 22050Hz
    write_wav(SAMPLES / "mismatch.wav", 44100, 1,
              sine_frames(44100, 22050, 440, 4000))

    # corrupt：固定垃圾字节（确定性）
    (SAMPLES / "corrupt.wav").write_bytes(
        b"NOT_A_REAL_WAV_" + bytes(range(256)) * 4)

    manifest = {
        "entries": [
            # 有效：跳变量 1000
            {"id": "skill_fire", "wav": "samples/sine_a.wav",
             "sample_rate": 44100, "channels": 1,
             "loop_start_frame": 10000, "loop_end_frame": 20000,
             "preview_ms": 100},
            # 有效：跳变量 500
            {"id": "skill_ice", "wav": "samples/sine_b.wav",
             "sample_rate": 44100, "channels": 1,
             "loop_start_frame": 5000, "loop_end_frame": 15000,
             "preview_ms": 50},
            # 有效：立体声，preview 500ms=24000 帧 > 循环一半 3000 帧 → 截取
            {"id": "skill_thunder", "wav": "samples/stereo_c.wav",
             "sample_rate": 48000, "channels": 2,
             "loop_start_frame": 2000, "loop_end_frame": 8000,
             "preview_ms": 500},
            # 无效：结束帧 < 起始帧
            {"id": "bad_reversed", "wav": "samples/sine_a.wav",
             "sample_rate": 44100, "channels": 1,
             "loop_start_frame": 20000, "loop_end_frame": 10000,
             "preview_ms": 100},
            # 无效：结束帧超出实际帧数
            {"id": "bad_overflow", "wav": "samples/sine_a.wav",
             "sample_rate": 44100, "channels": 1,
             "loop_start_frame": 10000, "loop_end_frame": 99999,
             "preview_ms": 100},
            # 错误：声明采样率与实际不符
            {"id": "bad_rate", "wav": "samples/mismatch.wav",
             "sample_rate": 22050, "channels": 1,
             "loop_start_frame": 0, "loop_end_frame": 10000,
             "preview_ms": 100},
            # 错误：源 WAV 损坏
            {"id": "bad_corrupt", "wav": "samples/corrupt.wav",
             "sample_rate": 44100, "channels": 1,
             "loop_start_frame": 0, "loop_end_frame": 100,
             "preview_ms": 100},
            # 重复 id：两条 skill_fire，仅保留先出现的一条
            {"id": "skill_fire", "wav": "samples/sine_b.wav",
             "sample_rate": 44100, "channels": 1,
             "loop_start_frame": 5000, "loop_end_frame": 15000,
             "preview_ms": 50},
        ]
    }
    (HERE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"样例已生成: {SAMPLES}")
    print(f"清单已生成: {HERE / 'manifest.json'}")


if __name__ == "__main__":
    main()
