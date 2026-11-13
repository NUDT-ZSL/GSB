#!/usr/bin/env python3
"""生成任务自带的确定性 PCM WAV 样本与 JSON 清单。

仅使用标准库，不依赖任何外部音频文件。

用法::

    python make_samples.py [--dest samples]

会创建如下演示场景（每个目录自带 manifest.json）：::

    samples/
      basic/      3 段单声道 16-bit 片段，含边界静音
      silence/    0.250s 音 + 0.500s 静音 + 0.250s 音（0.5 秒静音验收）
      stereo/     2 段立体声片段（左右交错顺序验收）
      mono8/      2 段 8-bit 单声道片段
      bad_rate/   混入不同采样率的片段（格式不一致验收）
      empty/      空片段列表（“无片段可拼接”验收）
      missing/    清单引用一个不存在的文件（缺失验收）
      existing/   清单指向一个已存在的输出（不覆盖验收）
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import wave
from pathlib import Path
from typing import Sequence

SAMPLE_RATE = 8000


# ---------------------------------------------------------------------------
# WAV 写出辅助
# ---------------------------------------------------------------------------


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


def _encode_samples(samples: Sequence[float], sample_width: int) -> bytes:
    """把归一化样本值写成小端 PCM 字节。"""
    if sample_width == 1:
        return bytes(_clamp(128 + value * 127, 0, 255) for value in samples)
    if sample_width == 2:
        return b"".join(
            struct.pack("<h", _clamp(value * 32767, -32768, 32767))
            for value in samples
        )
    if sample_width == 3:
        chunks = []
        for value in samples:
            raw = _clamp(value * 8388607, -8388608, 8388607) & 0xFFFFFF
            chunks.append(raw.to_bytes(3, "little"))
        return b"".join(chunks)
    if sample_width == 4:
        return b"".join(
            struct.pack(
                "<i", _clamp(value * 2147483647, -2147483648, 2147483647)
            )
            for value in samples
        )
    raise ValueError(f"不支持的采样位宽: {sample_width}")


def write_wav(
    path: Path,
    samples: Sequence[float],
    *,
    sample_rate: int = SAMPLE_RATE,
    sample_width: int = 2,
    channels: int = 1,
) -> None:
    """写出规范 PCM WAV；samples 为已按声道交错的归一化值。"""
    if len(samples) % channels != 0:
        raise ValueError("样本数量必须是声道数的整数倍")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(_encode_samples(samples, sample_width))


def tone(
    seconds: float,
    frequency: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    amplitude: float = 0.5,
) -> list[float]:
    """确定性正弦波，避免任何随机数。"""
    count = int(round(seconds * sample_rate))
    return [
        amplitude * math.sin(2.0 * math.pi * frequency * i / sample_rate)
        for i in range(count)
    ]


def tones(
    seconds: float,
    frequencies: Sequence[float],
    *,
    sample_rate: int = SAMPLE_RATE,
    amplitude: float = 0.5,
) -> list[float]:
    """多声道：按声道顺序交错生成不同频率的音调。"""
    channels = len(frequencies)
    frames: list[float] = []
    count = int(round(seconds * sample_rate))
    for i in range(count):
        for frequency in frequencies:
            frames.append(
                amplitude * math.sin(2.0 * math.pi * frequency * i / sample_rate)
            )
    assert len(frames) == count * channels
    return frames


def silence(seconds: float, channels: int = 1) -> list[float]:
    return [0.0] * (int(round(seconds * SAMPLE_RATE)) * channels)


def write_manifest(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


def build_basic(root: Path) -> None:
    """3 段单声道片段：音 / 音 / 短音，输出到 out/track.wav。"""
    scene = root / "basic"
    clips = scene / "clips"
    write_wav(clips / "clip1.wav", tone(0.25, 440.0))
    write_wav(clips / "clip2.wav", tone(0.25, 660.0))
    write_wav(clips / "clip3.wav", tone(0.10, 880.0))
    write_manifest(
        scene / "manifest.json",
        {
            "clips": [
                "clips/clip1.wav",
                "clips/clip2.wav",
                "clips/clip3.wav",
            ],
            "output": "track.wav",
            "report": "report.json",
        },
    )


def build_silence(root: Path) -> None:
    """0.250s 音 + 0.500s 静音 + 0.250s 音，总时长 1.000s。"""
    scene = root / "silence"
    clips = scene / "clips"
    write_wav(clips / "before.wav", tone(0.25, 440.0))
    write_wav(clips / "gap.wav", silence(0.5))
    write_wav(clips / "after.wav", tone(0.25, 440.0))
    write_manifest(
        scene / "manifest.json",
        {
            "clips": [
                "clips/before.wav",
                "clips/gap.wav",
                "clips/after.wav",
            ],
            "output": "track.wav",
            "report": "report.json",
        },
    )


def build_stereo(root: Path) -> None:
    """2 段立体声：左 440Hz / 右 880Hz，验证交错顺序。"""
    scene = root / "stereo"
    clips = scene / "clips"
    write_wav(clips / "left_right_a.wav", tones(0.20, (440.0, 880.0)), channels=2)
    write_wav(clips / "left_right_b.wav", tones(0.10, (330.0, 550.0)), channels=2)
    write_manifest(
        scene / "manifest.json",
        {
            "clips": ["clips/left_right_a.wav", "clips/left_right_b.wav"],
            "output": "track.wav",
            "report": "report.json",
        },
    )


def build_mono8(root: Path) -> None:
    """2 段 8-bit 单声道片段，验证位深保留。"""
    scene = root / "mono8"
    clips = scene / "clips"
    write_wav(clips / "a.wav", tone(0.20, 440.0), sample_width=1)
    write_wav(clips / "b.wav", tone(0.20, 880.0), sample_width=1)
    write_manifest(
        scene / "manifest.json",
        {
            "clips": ["clips/a.wav", "clips/b.wav"],
            "output": "track.wav",
            "report": "report.json",
        },
    )


def build_bad_rate(root: Path) -> None:
    """片段 2 的采样率为 16000 Hz，其余为 8000 Hz。"""
    scene = root / "bad_rate"
    clips = scene / "clips"
    write_wav(clips / "ok.wav", tone(0.20, 440.0), sample_rate=8000)
    write_wav(clips / "wrong.wav", tone(0.20, 440.0), sample_rate=16000)
    write_manifest(
        scene / "manifest.json",
        {
            "clips": ["clips/ok.wav", "clips/wrong.wav"],
            "output": "track.wav",
            "report": "report.json",
        },
    )


def build_empty(root: Path) -> None:
    scene = root / "empty"
    write_manifest(
        scene / "manifest.json",
        {"clips": [], "output": "track.wav", "report": "report.json"},
    )


def build_missing(root: Path) -> None:
    scene = root / "missing"
    clips = scene / "clips"
    write_wav(clips / "present.wav", tone(0.20, 440.0))
    write_manifest(
        scene / "manifest.json",
        {
            "clips": ["clips/present.wav", "clips/absent.wav"],
            "output": "track.wav",
            "report": "report.json",
        },
    )


def build_existing(root: Path) -> None:
    """预置输出文件，用于验证绝不覆盖。"""
    scene = root / "existing"
    clips = scene / "clips"
    write_wav(clips / "a.wav", tone(0.20, 440.0))
    write_manifest(
        scene / "manifest.json",
        {"clips": ["clips/a.wav"], "output": "track.wav"},
    )
    existing = scene / "out" / "track.wav"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"EXISTING-OUTPUT-DO-NOT-TOUCH")


def build_all(dest: Path, *, clean: bool = False) -> Path:
    dest = dest.resolve()
    if clean and dest.exists():
        shutil.rmtree(dest)
    for builder in (
        build_basic,
        build_silence,
        build_stereo,
        build_mono8,
        build_bad_rate,
        build_empty,
        build_missing,
        build_existing,
    ):
        builder(dest)
    return dest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_samples.py",
        description="生成确定性的 PCM WAV 演示样本与清单。",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parent / "samples",
        help="样本输出目录（默认 ./samples）",
    )
    parser.add_argument(
        "--clean", action="store_true", help="生成前删除目标目录"
    )
    args = parser.parse_args(argv)
    dest = build_all(args.dest, clean=args.clean)
    print(f"样本已生成: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
