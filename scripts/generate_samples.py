"""确定性生成演示 / 测试所需的 WAV 样本（纯标准库，无外部依赖）。

生成内容（采样率均为 44100Hz）：
- voice_mono.wav   单声道 16-bit PCM，10s，多频率正弦扫频，幅度约 12000
- ad_mono.wav      单声道 16-bit PCM，3s，  独特频率/幅度，模拟广告
- jingle_mono.wav  单声道 16-bit PCM，2s，  大音量（接近满幅），用于验证钳位防削波
- bg_stereo.wav    立体声 16-bit PCM，2s，  用于验证非法格式被跳过

所有波形由确定性公式生成，同参数重复运行得到字节级一致的文件。
"""

from __future__ import annotations

import array
import math
import os
import sys
import wave

SR = 44100


def _mono_samples(duration: float, freqs: tuple, amplitude: int) -> array.array:
    """多正弦叠加 + 轻微幅度包络，确定性生成单声道样本。"""
    n = int(round(duration * SR))
    out = array.array("h", [0]) * n
    # 用秒级相位索引，保证不同时长文件间同一时刻的样本可对比
    for i in range(n):
        t = i / SR
        envelope = 0.85 + 0.15 * math.sin(2 * math.pi * 0.5 * t)
        value = 0.0
        for f in freqs:
            value += math.sin(2 * math.pi * f * t)
        value = value / len(freqs) * amplitude * envelope
        out[i] = max(-32768, min(32767, int(round(value))))
    return out


def _write_mono(path: str, samples: array.array, sr: int = SR) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())


def _write_stereo(path: str, duration: float = 2.0, sr: int = SR) -> None:
    n = int(round(duration * sr))
    frames = bytearray()
    for i in range(n):
        t = i / sr
        left = int(8000 * math.sin(2 * math.pi * 300 * t))
        right = int(8000 * math.cos(2 * math.pi * 300 * t))
        frames += int(left).to_bytes(2, "little", signed=True)
        frames += int(right).to_bytes(2, "little", signed=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))


def generate(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    paths = {}

    voice = _mono_samples(10.0, (220.0, 330.0, 440.0), 12000)
    paths["voice_mono"] = os.path.join(out_dir, "voice_mono.wav")
    _write_mono(paths["voice_mono"], voice)

    ad = _mono_samples(3.0, (880.0, 1320.0), 9000)
    paths["ad_mono"] = os.path.join(out_dir, "ad_mono.wav")
    _write_mono(paths["ad_mono"], ad)

    jingle = _mono_samples(2.0, (660.0, 990.0), 30000)  # 接近满幅，验证钳位
    paths["jingle_mono"] = os.path.join(out_dir, "jingle_mono.wav")
    _write_mono(paths["jingle_mono"], jingle)

    paths["bg_stereo"] = os.path.join(out_dir, "bg_stereo.wav")
    _write_stereo(paths["bg_stereo"])

    return paths


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "input"
    )
    created = generate(target)
    for name, path in created.items():
        print(f"generated {name}: {path}")
