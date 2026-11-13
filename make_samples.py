# -*- coding: utf-8 -*-
"""
make_samples.py — 确定性生成测试用 WAV 样本（仅标准库）

生成内容（固定公式、无随机源，可重复复现）：
  tone_440.wav        单声道 16 位 48kHz，1.0s 440Hz 正弦波，幅度 20000
  tone_1k.wav         单声道 16 位 48kHz，1.0s 1000Hz 正弦波，幅度 12000
  dialogue_demo.wav   单声道 16 位 48kHz，2.0s 双音(220/330Hz)确定性混合
  stereo_16bit.wav    立体声 16 位（非法：应在报告中记错误）
  mono_8bit.wav       单声道 8 位 PCM（非法：应在报告中记错误）
  broken.wav           头部被截断的损坏 WAV（非法：应在报告中记错误）

用法:
  python make_samples.py [输出目录]      默认 ./input
"""

import math
import os
import struct
import sys
import wave

SAMPLE_RATE = 48000
AMP_440 = 20000
AMP_1K = 12000


def _force_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def sine_samples(freq, duration_sec, amplitude, phase0=0.0):
    """生成确定性正弦波整数样本列表。"""
    n = int(round(duration_sec * SAMPLE_RATE))
    return [
        int(round(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE + phase0)))
        for i in range(n)
    ]


def write_mono16(path, samples, rate=SAMPLE_RATE):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<%dh" % len(samples), *samples))


def main(argv):
    _force_utf8_stdio()
    out_dir = argv[1] if len(argv) > 1 else "input"
    os.makedirs(out_dir, exist_ok=True)

    # 1) 440Hz 正弦波 1s（用于峰值/RMS/过零率理论值校验）
    write_mono16(
        os.path.join(out_dir, "tone_440.wav"),
        sine_samples(440, 1.0, AMP_440),
    )

    # 2) 1kHz 正弦波 1s
    write_mono16(
        os.path.join(out_dir, "tone_1k.wav"),
        sine_samples(1000, 1.0, AMP_1K),
    )

    # 3) 模拟对白：2s，220Hz 与 330Hz 混合（确定性，无随机源）
    n2 = 2 * SAMPLE_RATE
    dialogue = []
    for i in range(n2):
        t = i / SAMPLE_RATE
        # 简单幅度包络，模拟语音起伏；公式固定
        env = 0.6 + 0.4 * math.sin(2 * math.pi * 2 * t)
        v = 0.7 * math.sin(2 * math.pi * 220 * t) + 0.3 * math.sin(2 * math.pi * 330 * t)
        dialogue.append(int(round(18000 * env * v)))
    write_mono16(os.path.join(out_dir, "dialogue_demo.wav"), dialogue)

    # 4) 立体声 16 位（非法格式样本）
    stereo_path = os.path.join(out_dir, "stereo_16bit.wav")
    with wave.open(stereo_path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        frames = []
        for i in range(SAMPLE_RATE // 2):  # 0.5s
            v = int(round(10000 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE)))
            frames.extend((v, -v))
        wf.writeframes(struct.pack("<%dh" % len(frames), *frames))

    # 5) 单声道 8 位 PCM（非法格式样本）
    mono8_path = os.path.join(out_dir, "mono_8bit.wav")
    with wave.open(mono8_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(SAMPLE_RATE)
        samples8 = [
            128 + int(round(60 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE)))
            for i in range(SAMPLE_RATE // 2)
        ]
        wf.writeframes(struct.pack("<%dB" % len(samples8), *samples8))

    # 6) 损坏 WAV：取合法文件前 64 字节截断，wave 模块无法解析
    good = os.path.join(out_dir, "tone_1k.wav")
    broken = os.path.join(out_dir, "broken.wav")
    with open(good, "rb") as f:
        head = f.read(64)
    with open(broken, "wb") as f:
        f.write(head)

    print("已在 %s 生成确定性样本：" % os.path.abspath(out_dir))
    for name in sorted(os.listdir(out_dir)):
        size = os.path.getsize(os.path.join(out_dir, name))
        print("  %-18s %8d 字节" % (name, size))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
