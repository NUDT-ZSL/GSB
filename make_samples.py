#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_samples.py — 确定性生成演示用 WAV 样本与清单 JSON（仅标准库）。

生成到 samples/ 目录：
  intro.wav       1s 440Hz 正弦，RMS≈1000（有效）
  main.wav        1s 550Hz 正弦，RMS≈1000（有效）
  quiet_ad.wav    1s 330Hz 正弦，RMS≈400 （有效，应被标注“音量过低”）
  wrong_rate.wav  48000Hz（格式不符）
  stereo.wav      双声道（格式不符）
  corrupt.wav     0 字节空文件（文件损坏）
  manifest.json   含以上片段及一个不存在的 missing.wav
"""
import array
import json
import math
import wave
from pathlib import Path

RATE = 44100
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


def sine_frames(freq: float, seconds: float, amplitude: float, rate: int = RATE) -> bytes:
    n = int(rate * seconds)
    frames = array.array("h", (
        int(round(amplitude * math.sin(2.0 * math.pi * freq * i / rate)))
        for i in range(n)
    ))
    if frames.itemsize != 2:
        raise RuntimeError("平台不支持 16bit 样本")
    return frames.tobytes()


def write_wav(path: Path, data: bytes, rate: int = RATE, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)


def main() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    # 正弦 RMS ≈ 幅度/√2：1414→≈1000，566→≈400
    write_wav(SAMPLES_DIR / "intro.wav", sine_frames(440, 1.0, 1414))
    write_wav(SAMPLES_DIR / "main.wav", sine_frames(550, 1.0, 1414))
    write_wav(SAMPLES_DIR / "quiet_ad.wav", sine_frames(330, 1.0, 566))
    write_wav(SAMPLES_DIR / "wrong_rate.wav", sine_frames(440, 1.0, 1414, rate=48000),
              rate=48000)
    # 双声道：左右声道交错同一信号
    mono = sine_frames(440, 1.0, 1414)
    samples = array.array("h")
    samples.frombytes(mono)
    stereo = array.array("h")
    for s in samples:
        stereo.append(s)
        stereo.append(s)
    write_wav(SAMPLES_DIR / "stereo.wav", stereo.tobytes(), channels=2)
    (SAMPLES_DIR / "corrupt.wav").write_bytes(b"")  # 空文件 → 文件损坏

    manifest = {
        "segments": [
            {"path": "intro.wav", "title": "开场白"},
            {"path": "missing.wav", "title": "丢失的访谈"},
            {"path": "main.wav", "title": "正片"},
            {"path": "wrong_rate.wav", "title": "采样率错误的广告"},
            {"path": "stereo.wav", "title": "立体声插曲"},
            {"path": "corrupt.wav", "title": "损坏的片尾"},
            {"path": "quiet_ad.wav", "title": "低音量口播"},
        ]
    }
    (SAMPLES_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"样本已生成到 {SAMPLES_DIR}")


if __name__ == "__main__":
    main()
