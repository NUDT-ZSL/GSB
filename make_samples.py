#!/usr/bin/env python3
"""确定性生成测试样本：单声道16位WAV、立体声WAV、8位WAV、损坏文件。"""
import math
import os
import struct
import wave

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
RATE = 8000
DURATION_SEC = 5


def sine_frames(channels):
    frames = bytearray()
    for i in range(RATE * DURATION_SEC):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / RATE))
        for _ in range(channels):
            frames += struct.pack("<h", v)
    return bytes(frames)


def main():
    os.makedirs(SAMPLES_DIR, exist_ok=True)

    with wave.open(os.path.join(SAMPLES_DIR, "mono_16bit.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(sine_frames(1))

    with wave.open(os.path.join(SAMPLES_DIR, "stereo_16bit.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(sine_frames(2))

    with wave.open(os.path.join(SAMPLES_DIR, "mono_8bit.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(RATE)
        w.writeframes(bytes(128 for _ in range(RATE * DURATION_SEC)))

    with open(os.path.join(SAMPLES_DIR, "corrupt.txt"), "w", encoding="utf-8") as f:
        f.write("这不是WAV文件，只是一段纯文本，用于验证损坏输入的友好提示。\n")

    print("样本已生成于", SAMPLES_DIR)


if __name__ == "__main__":
    main()
