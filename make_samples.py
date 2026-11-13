"""生成确定性的本地演示样本（仅标准库，无需网络、无需第三方库）。

在目标目录中生成：
  a.wav   —— 合法单声道 16 位 PCM，8kHz，3 秒；
             0~1s 静音、1~2s 440Hz 正弦（含 80ms 短静音间隙，应被 500ms 门槛过滤）、
             2~3s 静音
  b.wav   —— 合法单声道 16 位 PCM，8kHz，2 秒；
             0~1s 静音、1~2s 330Hz 正弦
  bad.wav —— 纯文本文件改名为 .wav（wave 模块必然解析失败）
  stereo_sample.wav —— 立体声 16 位 PCM（格式不符，应被跳过并记录原因）
  config.json       —— 正常配置（-40dB / 500ms）
  config_missing_threshold.json —— 缺少 threshold_db，验证默认值回退
  config_empty.json —— 空 JSON 对象，两个字段都回退默认
"""

from __future__ import annotations

import json
import math
import os
import sys
import wave

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SAMPLE_RATE = 8000


def _tone(freq_hz: float, duration_s: float, amplitude: int = 12000) -> bytes:
    """生成 16 位单声道正弦波字节。"""
    frames = int(duration_s * SAMPLE_RATE)
    return b"".join(
        int(amplitude * math.sin(2.0 * math.pi * freq_hz * i / SAMPLE_RATE))
        .to_bytes(2, "little", signed=True)
        for i in range(frames)
    )


def _silence(duration_s: float) -> bytes:
    return b"\x00\x00" * int(duration_s * SAMPLE_RATE)


def _write_wav(path: str, pcm: bytes, channels: int, sample_width: int = 2) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)


def generate(target_dir: str) -> None:
    os.makedirs(target_dir, exist_ok=True)

    # a.wav: 静音 1s + 440Hz 0.46s + 短静音 0.08s（低于 500ms 门槛）+ 440Hz 0.46s + 静音 1s
    a_pcm = (
        _silence(1.0)
        + _tone(440, 0.46)
        + _silence(0.08)
        + _tone(440, 0.46)
        + _silence(1.0)
    )
    _write_wav(os.path.join(target_dir, "a.wav"), a_pcm, channels=1)

    # b.wav: 前 1s 静音 + 1s 330Hz 正弦，总长 2s
    b_pcm = _silence(1.0) + _tone(330, 1.0)
    _write_wav(os.path.join(target_dir, "b.wav"), b_pcm, channels=1)

    # bad.wav: 纯文本改名，wave 解析必失败
    with open(os.path.join(target_dir, "bad.wav"), "w", encoding="utf-8") as f:
        f.write("this is not a wav file, just plain text renamed to .wav\n")

    # stereo_sample.wav: 立体声文件（格式不符场景）
    mono = _tone(440, 0.5)
    stereo = b"".join(mono[i:i + 2] + mono[i:i + 2] for i in range(0, len(mono), 2))
    _write_wav(os.path.join(target_dir, "stereo_sample.wav"), stereo, channels=2)

    with open(os.path.join(target_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"threshold_db": -40, "min_silence_ms": 500}, f, indent=2)

    with open(
        os.path.join(target_dir, "config_missing_threshold.json"), "w", encoding="utf-8"
    ) as f:
        json.dump({"min_silence_ms": 500}, f, indent=2)

    with open(os.path.join(target_dir, "config_empty.json"), "w", encoding="utf-8") as f:
        f.write("{}")

    print(f"演示样本已生成到: {os.path.abspath(target_dir)}")
    for name in sorted(os.listdir(target_dir)):
        size = os.path.getsize(os.path.join(target_dir, name))
        print(f"  {name}  ({size} bytes)")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "demo_data"
    )
    generate(out)
