"""生成确定性测试 WAV 与剪辑方案 JSON（仅标准库 wave/json/math）。

用法::

    python tools/make_samples.py [输出目录]   # 默认 demo_data/

生成内容：
    tone_16bit_mono_1sec.wav     44.1kHz 单声道 16-bit，1 秒 440Hz 正弦波，峰值 20000
    tone_16bit_stereo_1sec.wav   同上，立体声（左右声道反相）
    long_16bit_mono_2sec.wav     单声道 16-bit，2 秒 440Hz 正弦波
    tone_8bit_mono_1sec.wav      44.1kHz 单声道 8-bit（unsigned），用于格式拒绝验收
    plan_gain_minus6.json        整段 -6dB、无淡变（增益验收）
    plan_fades.json              带 50ms 线性淡入淡出（淡变验收）
    plan_mixed.json              1 个越界片段 + 2 个有效片段（跳过隔离验收）
    plan_all_skipped.json        3 个非法片段（空 WAV 验收）
    plan_missing_gain.json       缺少 gain_db 字段（方案校验验收）
    plan_wrong_type.json         gain_db 类型错误（方案校验验收）
    plan_stereo.json             立体声方案（通道一致性验收）
"""

from __future__ import annotations

import json
import math
import os
import sys
import wave

AMPLITUDE = 20000
SAMPLE_RATE = 44100
FREQ = 440


def _sine_samples(frame_count: int, amplitude: int, freq: int, sample_rate: int):
    return [
        int(round(amplitude * math.sin(2 * math.pi * freq * n / sample_rate)))
        for n in range(frame_count)
    ]


def write_sine_wav(
    path: str,
    duration: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    freq: int = FREQ,
    amplitude: int = AMPLITUDE,
    channels: int = 1,
    sampwidth: int = 2,
) -> None:
    """生成确定性正弦波 WAV。sampwidth=2 写 16-bit PCM，sampwidth=1 写 8-bit unsigned。"""
    frame_count = int(round(duration * sample_rate))
    mono = _sine_samples(frame_count, amplitude, freq, sample_rate)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.setcomptype("NONE", "not compressed")
        if sampwidth == 2:
            if channels == 1:
                frames = b"".join(int(s).to_bytes(2, "little", signed=True) for s in mono)
            else:
                parts = []
                for s in mono:
                    parts.append(int(s).to_bytes(2, "little", signed=True))
                    parts.append(int(-s).to_bytes(2, "little", signed=True))  # 右声道反相
                frames = b"".join(parts)
        elif sampwidth == 1:
            # 8-bit PCM 为无符号，静音点在 128
            frames = bytes(max(0, min(255, 128 + (s >> 8))) for s in mono)
        else:
            raise ValueError(f"样本生成器仅支持 sampwidth 1 或 2，收到 {sampwidth}")
        wf.writeframes(frames)


def write_plan(path: str, segments: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"segments": segments}, fh, ensure_ascii=False, indent=2)


def generate_all(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    write_sine_wav(os.path.join(out_dir, "tone_16bit_mono_1sec.wav"), duration=1.0, channels=1)
    write_sine_wav(
        os.path.join(out_dir, "tone_16bit_stereo_1sec.wav"), duration=1.0, channels=2
    )
    write_sine_wav(os.path.join(out_dir, "long_16bit_mono_2sec.wav"), duration=2.0, channels=1)
    write_sine_wav(
        os.path.join(out_dir, "tone_8bit_mono_1sec.wav"), duration=1.0, sampwidth=1
    )

    write_plan(
        os.path.join(out_dir, "plan_gain_minus6.json"),
        [{"start": 0.0, "end": 1.0, "fade_in": 0.0, "fade_out": 0.0, "gain_db": -6.0}],
    )
    write_plan(
        os.path.join(out_dir, "plan_fades.json"),
        [{"start": 0.0, "end": 1.0, "fade_in": 0.05, "fade_out": 0.05, "gain_db": 0.0}],
    )
    write_plan(
        os.path.join(out_dir, "plan_mixed.json"),
        [
            # 越界：源文件仅 2 秒
            {"start": 5.0, "end": 6.0, "fade_in": 0.0, "fade_out": 0.0, "gain_db": 0.0},
            # 有效片段 1
            {"start": 0.0, "end": 0.5, "fade_in": 0.01, "fade_out": 0.01, "gain_db": 0.0},
            # 有效片段 2
            {"start": 1.0, "end": 1.5, "fade_in": 0.0, "fade_out": 0.0, "gain_db": -3.0},
        ],
    )
    write_plan(
        os.path.join(out_dir, "plan_all_skipped.json"),
        [
            # 零时长
            {"start": 1.0, "end": 1.0, "fade_in": 0.0, "fade_out": 0.0, "gain_db": 0.0},
            # 越界
            {"start": 3.0, "end": 4.0, "fade_in": 0.0, "fade_out": 0.0, "gain_db": 0.0},
            # 终点早于起点
            {"start": 0.5, "end": 0.2, "fade_in": 0.0, "fade_out": 0.0, "gain_db": 0.0},
        ],
    )
    write_plan(
        os.path.join(out_dir, "plan_missing_gain.json"),
        [{"start": 0.0, "end": 1.0, "fade_in": 0.0, "fade_out": 0.0}],
    )
    write_plan(
        os.path.join(out_dir, "plan_wrong_type.json"),
        [
            {
                "start": 0.0,
                "end": 1.0,
                "fade_in": 0.0,
                "fade_out": 0.0,
                "gain_db": "-6 dB",  # 故意写成字符串
            }
        ],
    )
    write_plan(
        os.path.join(out_dir, "plan_stereo.json"),
        [{"start": 0.0, "end": 1.0, "fade_in": 0.01, "fade_out": 0.01, "gain_db": 0.0}],
    )


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "demo_data"
    generate_all(out_dir)
    print(f"已在 {os.path.abspath(out_dir)} 生成 {len(os.listdir(out_dir))} 个样本文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
