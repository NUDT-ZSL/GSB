"""生成确定性本地测试样本（仅标准库）。

布局（相对 --out 指定目录）：
    sfx/clean_44100.wav    有效：44100Hz 单声道16bit，首尾各 0.5s 静音 + 1s 1kHz 正弦（0.3 满幅）
    sfx/tone_22050.wav     有效：22050Hz，0.5s 正弦
    sfx/short_8000.wav     有效：8000Hz（支持范围下限）
    sfx/high_rate_96k.wav  无效：96000Hz 超出 8000–48000 范围
    sfx/legacy_8bit.wav    无效：8bit PCM
    sfx/corrupt.wav        无效：合法 WAV 被截断损坏
    sfx/notes.txt          无效：文本文件
    sub/nested_16k.wav     有效：子目录中的 16000Hz WAV

运行：python tests/make_fixtures.py --out testdata/input
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path


def _sine_16bit(
    sample_rate: int,
    duration_s: float,
    freq_hz: float = 1000.0,
    amplitude: float = 0.3,
) -> list[int]:
    n = int(round(sample_rate * duration_s))
    peak = amplitude * 32767
    return [int(round(peak * math.sin(2.0 * math.pi * freq_hz * i / sample_rate)))
            for i in range(n)]


def _silence(duration_s: float, sample_rate: int) -> list[int]:
    return [0] * int(round(sample_rate * duration_s))


def write_pcm_wav(
    path: Path,
    samples: list[int],
    sample_rate: int,
    sampwidth: int = 2,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sampwidth == 2:
        payload = struct.pack("<%dh" % len(samples), *samples)
    elif sampwidth == 1:
        # 8bit PCM 是无符号的，中点 128
        payload = bytes(max(0, min(255, int(round(s / 256.0)) + 128)) for s in samples)
    else:
        raise ValueError(f"不支持的样本宽度 {sampwidth}")

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(payload)


def build_fixtures(out_dir: Path) -> None:
    sr = 44100
    # 任务验收样本：首尾各 0.5s 静音，中间 1s、0.3 满幅 1kHz 正弦
    framed = (
        _silence(0.5, sr)
        + _sine_16bit(sr, 1.0, freq_hz=1000.0, amplitude=0.3)
        + _silence(0.5, sr)
    )
    write_pcm_wav(out_dir / "sfx" / "clean_44100.wav", framed, sr)

    write_pcm_wav(out_dir / "sfx" / "tone_22050.wav",
                  _sine_16bit(22050, 0.5, 880.0, 0.5), 22050)
    write_pcm_wav(out_dir / "sfx" / "short_8000.wav",
                  _sine_16bit(8000, 0.2, 440.0, 0.4), 8000)

    # 96kHz：PCM 本身合法，但采样率超出允许范围
    write_pcm_wav(out_dir / "sfx" / "high_rate_96k.wav",
                  _sine_16bit(96000, 0.2, 1000.0, 0.3), 96000)

    # 8bit：位深不符
    write_pcm_wav(out_dir / "sfx" / "legacy_8bit.wav",
                  _sine_16bit(sr, 0.3, 1000.0, 0.5), sr, sampwidth=1)

    # 损坏文件：先写合法 WAV，再截断尾部数据
    corrupt_target = out_dir / "sfx" / "corrupt.wav"
    write_pcm_wav(corrupt_target, framed, sr)
    raw = corrupt_target.read_bytes()
    corrupt_target.write_bytes(raw[: len(raw) - 300])

    # 文本文件
    txt = out_dir / "sfx" / "notes.txt"
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text("这不是音频文件，仅用于验证跳过逻辑。\n", encoding="utf-8")

    # 子目录有效素材（验证相对路径结构保持）
    write_pcm_wav(out_dir / "sub" / "nested_16k.wav",
                  _silence(0.1, 16000) + _sine_16bit(16000, 0.4, 660.0, 0.4)
                  + _silence(0.1, 16000),
                  16000)


def _force_utf8_console() -> None:
    """Windows 管道/重定向场景强制 UTF-8，避免中文输出乱码。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def main() -> None:
    _force_utf8_console()
    parser = argparse.ArgumentParser(description="生成确定性 WAV 测试样本")
    parser.add_argument("--out", required=True, help="样本输出目录")
    args = parser.parse_args()
    out_dir = Path(args.out)
    build_fixtures(out_dir)
    print(f"测试样本已生成到: {out_dir.resolve()}")
    for p in sorted(out_dir.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(out_dir).as_posix()}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
