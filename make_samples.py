#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_samples.py — 用标准库 wave/json 确定性生成演示样本。

生成:
  demo/src/interview_16k.wav  16kHz 16位单声道 3秒 正弦 峰值20000
  demo/src/phone_8k.wav        8kHz 16位单声道 2秒
  demo/src/stereo.wav         16kHz 立体声(用于演示格式拒绝)
  demo/src/8bit.wav           16kHz 8位(用于演示格式拒绝)
  demo/src/corrupt.wav        损坏文件(用于演示 error 条目)
  demo/edl_valid.json         全合法清单(退出码0)
  demo/edl_mixed.json         含缺失文件/越界/起点大于终点/合法(退出码1)
  demo/edl_rate_mismatch.json 混用 8kHz 与 16kHz(整体拒绝)
"""
import json
import math
import struct
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "demo" / "src"


def write_wav(path: Path, rate: int, samples, channels: int = 1,
              width: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        if width == 2:
            wf.writeframes(b"".join(struct.pack("<h", s) for s in samples))
        else:
            wf.writeframes(bytes(samples))


def sine(rate: int, seconds: float, freq: float, amp: int):
    n = int(rate * seconds)
    return [int(amp * math.sin(2 * math.pi * freq * i / rate))
            for i in range(n)]


def main() -> None:
    write_wav(SRC / "interview_16k.wav", 16000,
              sine(16000, 3.0, 440.0, 20000))
    write_wav(SRC / "phone_8k.wav", 8000,
              sine(8000, 2.0, 440.0, 12000))
    # 立体声样本(交错双声道)
    stereo = []
    for s in sine(16000, 1.0, 440.0, 10000):
        stereo.extend([s, s])
    write_wav(SRC / "stereo.wav", 16000, stereo, channels=2)
    # 8位样本
    write_wav(SRC / "8bit.wav", 16000,
              [128 + int(60 * math.sin(2 * math.pi * 440 * i / 16000))
               for i in range(16000)], width=1)
    # 损坏样本
    (SRC / "corrupt.wav").write_bytes(b"NOT-A-REAL-WAV-FILE")

    edl_dir = ROOT / "demo"
    src = lambda name: str(SRC / name)  # noqa: E731
    (edl_dir / "edl_valid.json").write_text(json.dumps([
        {"source": src("interview_16k.wav"), "start": 0.0, "end": 1.0,
         "normalize": True},
        {"source": src("interview_16k.wav"), "start": 1.0, "end": 2.0,
         "normalize": False},
    ], ensure_ascii=False, indent=2), encoding="utf-8")

    (edl_dir / "edl_mixed.json").write_text(json.dumps([
        {"source": src("interview_16k.wav"), "start": 0.0, "end": 1.0,
         "normalize": True},
        {"source": src("missing.wav"), "start": 0.0, "end": 1.0,
         "normalize": False},
        {"source": src("corrupt.wav"), "start": 0.0, "end": 1.0,
         "normalize": False},
        {"source": src("interview_16k.wav"), "start": 2.0, "end": 1.0,
         "normalize": False},
        {"source": src("interview_16k.wav"), "start": 2.5, "end": 9.0,
         "normalize": False},
        {"source": src("interview_16k.wav"), "start": 1.0, "end": 2.0,
         "normalize": False},
    ], ensure_ascii=False, indent=2), encoding="utf-8")

    (edl_dir / "edl_rate_mismatch.json").write_text(json.dumps([
        {"source": src("interview_16k.wav"), "start": 0.0, "end": 1.0,
         "normalize": False},
        {"source": src("phone_8k.wav"), "start": 0.0, "end": 1.0,
         "normalize": False},
    ], ensure_ascii=False, indent=2), encoding="utf-8")

    (edl_dir / "edl_bad_format.json").write_text(json.dumps([
        {"source": src("stereo.wav"), "start": 0.0, "end": 0.5,
         "normalize": False},
        {"source": src("8bit.wav"), "start": 0.0, "end": 0.5,
         "normalize": False},
    ], ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"样本已生成于 {edl_dir}")


if __name__ == "__main__":
    main()
