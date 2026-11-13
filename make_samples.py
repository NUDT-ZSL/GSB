#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_samples — 确定性生成测试用 PCM16 WAV 样本与 JSON 清单（无外部依赖、无随机）。"""
import json
import os
import struct
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
RATE = 44100


def write_wav(path, rate, samples, channels=1, sampwidth=2):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        if sampwidth == 2:
            w.writeframes(struct.pack("<%dh" % len(samples), *samples))
        elif sampwidth == 3:  # 24 位小端
            w.writeframes(b"".join(
                int(s).to_bytes(3, "little", signed=True) for s in samples))
        else:
            w.writeframes(bytes(samples))


def const(n, v):
    return [v] * n


def ramp(n):
    # 确定性伪波形：固定步进的锯齿，便于逐样本核对
    return [((i * 37) % 20001) - 10000 for i in range(n)]


def main():
    os.makedirs(SAMPLES, exist_ok=True)

    # ---- 合法片段（44100 Hz / 16 位 / 单声道）----
    write_wav(os.path.join(SAMPLES, "a.wav"), RATE, const(4000, 8000))
    write_wav(os.path.join(SAMPLES, "b.wav"), RATE, const(3000, -6000))
    write_wav(os.path.join(SAMPLES, "c.wav"), RATE, const(5000, 10000))
    write_wav(os.path.join(SAMPLES, "const.wav"), RATE, const(2000, 12000))
    write_wav(os.path.join(SAMPLES, "single.wav"), RATE, ramp(1234))
    # 满幅片段：交替 +32767 / -32768，用于饱和与符号检查
    write_wav(os.path.join(SAMPLES, "full.wav"), RATE,
              [32767 if i % 2 == 0 else -32768 for i in range(1000)])

    # ---- 非法/边界片段 ----
    write_wav(os.path.join(SAMPLES, "stereo.wav"), RATE,
              [v for i in range(500) for v in (1000, -1000)], channels=2)
    write_wav(os.path.join(SAMPLES, "wav24.wav"), RATE, const(500, 100000),
              sampwidth=3)
    write_wav(os.path.join(SAMPLES, "rate48000.wav"), 48000, const(480, 5000))
    write_wav(os.path.join(SAMPLES, "rate6000.wav"), 6000, const(600, 5000))
    write_wav(os.path.join(SAMPLES, "zero.wav"), RATE, [])
    write_wav(os.path.join(SAMPLES, "short.wav"), RATE, const(40, 7000))
    with open(os.path.join(SAMPLES, "corrupt.wav"), "wb") as f:
        f.write(b"RIFF\x00\x00\x00\x00WAVEjunk-not-a-real-wav-file")

    manifests = {
        # 正常：三个已知帧数片段，两条接缝各 10ms 淡变（441 帧，满足半帧数约束）
        "manifest.json": {"files": [
            {"path": "a.wav", "fade_ms": 10},
            {"path": "b.wav", "fade_ms": 10},
            {"path": "c.wav"},
        ]},
        # 单片段：输出须与输入逐样本一致
        "single.json": {"files": [{"path": "single.wav", "fade_ms": 50}]},
        # 恒定幅度两片段，用于核对线性增益序列
        "linear.json": {"files": [
            {"path": "const.wav", "fade_ms": 10},
            {"path": "const.wav"},
        ]},
        # 满幅片段，用于饱和/符号检查
        "full.json": {"files": [
            {"path": "full.wav", "fade_ms": 10},
            {"path": "full.wav"},
        ]},
        # 各类错误清单
        "missing_path.json": {"files": [
            {"path": "a.wav"}, {"fade_ms": 10}]},
        "fade_600.json": {"files": [
            {"path": "a.wav", "fade_ms": 600}, {"path": "b.wav"}]},
        "fade_float.json": {"files": [
            {"path": "a.wav"}, {"path": "b.wav", "fade_ms": 1.5}]},
        "mixed_rate.json": {"files": [
            {"path": "a.wav"}, {"path": "rate48000.wav"}]},
        "rate_6000.json": {"files": [{"path": "rate6000.wav"}]},
        "empty.json": {"files": []},
        "escape.json": {"files": [{"path": "../outside.wav"}]},
        "missing_file.json": {"files": [
            {"path": "a.wav"}, {"path": "nope.wav"}]},
        "corrupt.json": {"files": [{"path": "corrupt.wav"}]},
        "zero_frame.json": {"files": [{"path": "zero.wav"}]},
        "long_fade.json": {"files": [
            {"path": "short.wav", "fade_ms": 500}, {"path": "a.wav"}]},
        "stereo.json": {"files": [{"path": "stereo.wav"}]},
        "wav24.json": {"files": [{"path": "wav24.wav"}]},
    }
    for name, data in manifests.items():
        with open(os.path.join(SAMPLES, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    print("样本已生成于 %s（%d 个 WAV，%d 个清单）"
          % (SAMPLES, 12, len(manifests)))


if __name__ == "__main__":
    main()
