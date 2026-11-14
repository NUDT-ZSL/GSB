"""Generate a reproducible business sample for the lip-sync checker.

The generated dataset exercises every documented behaviour:

    L01  offset +10 ms with tolerance 8  -> out of tolerance
    L02  offset  +5 ms                  -> ok
    L03  all-zero WAV                   -> silent, excluded from statistics
    L04  stereo WAV                     -> unverifiable (channels != 1)
    L05  dialogue entry without a path  -> pending problem
    L06  offset -12 ms, shares L06/L07 WAV -> out of tolerance
    L07  offset   0 ms, same WAV as L06 -> decoded only once
    L08  three mouth times, earliest 250, onset 240 -> offset +10 ms

Usage:
    python tools/make_demo.py [--out sample]

Everything is standard library + numpy, no network access, and the whole
dataset stays well below 10 MB.
"""

from __future__ import annotations

import argparse
import json
import math
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
BIT_DEPTH_BYTES = 2


def _tone(duration_ms: float, onset_ms: float, fade_ms: float = 0.0, amplitude: float = 0.5,
          freq_hz: float = 220.0) -> np.ndarray:
    """A mono float signal that is exactly zero before ``onset_ms``."""
    n = int(round(SAMPLE_RATE * duration_ms / 1000.0))
    onset = int(round(SAMPLE_RATE * onset_ms / 1000.0))
    out = np.zeros(n, dtype=np.float64)
    if onset < n:
        t = np.arange(n - onset, dtype=np.float64) / SAMPLE_RATE
        # Cosine phase so the very first sample already carries full amplitude:
        # the onset is then detectable to the exact sample.
        wave_data = amplitude * np.cos(2.0 * math.pi * freq_hz * t)
        if fade_ms > 0:
            fade_n = max(1, int(round(SAMPLE_RATE * fade_ms / 1000.0)))
            env = np.ones_like(wave_data)
            ramp = min(fade_n, wave_data.size)
            env[:ramp] = np.linspace(0.0, 1.0, ramp, endpoint=False)
            if wave_data.size > ramp:
                env[-ramp:] = np.minimum(env[-ramp:], np.linspace(1.0, 0.0, ramp, endpoint=False))
            wave_data = wave_data * env
        out[onset:] = wave_data
    return out


def _write_wav(path: Path, mono: np.ndarray, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(mono, -1.0, 1.0)
    if channels == 1:
        data = (pcm * 32767.0).astype("<i2")
    else:
        stacked = np.repeat(pcm[:, None], channels, axis=1)
        data = (stacked * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(BIT_DEPTH_BYTES)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(data.tobytes())


def _interleave_silence_then(signal: np.ndarray) -> np.ndarray:
    return signal


def build(out_dir: Path) -> dict:
    wav_dir = out_dir / "wav"
    wav_dir.mkdir(parents=True, exist_ok=True)

    generated: dict[str, dict] = {}

    def make_file(name: str, signal: np.ndarray, channels: int = 1, onset_ms: float | None = None) -> str:
        _write_wav(wav_dir / name, signal, channels=channels)
        generated[name] = {
            "duration_ms": round(signal.size * 1000.0 / SAMPLE_RATE, 2),
            "channels": channels,
            "expected_onset_ms": onset_ms,
        }
        return f"wav/{name}"

    # L01: 300 ms digital silence, then a steady tone.  Mouth time is 10 ms late.
    l01 = _tone(1200, 300.0)
    l01_rel = make_file("l01_10ms_late.wav", l01, onset_ms=300.0)

    # L02: 500 ms silence, tone.  Mouth time only 5 ms late -> within tolerance.
    l02 = _tone(1200, 500.0)
    l02_rel = make_file("l02_5ms_late.wav", l02, onset_ms=500.0)

    # L03: all zeros -> silent line.
    l03 = np.zeros(int(SAMPLE_RATE * 0.8), dtype=np.float64)
    l03_rel = make_file("l03_silent.wav", l03, onset_ms=None)

    # L04: stereo rendering of a normal line -> unverifiable.
    l04 = _tone(1000, 200.0)
    l04_rel = make_file("l04_stereo.wav", l04, channels=2, onset_ms=200.0)

    # L05: dialogue entry with the WAV path removed.  The file exists on disk so
    # the point is unambiguously "field missing", not "file missing".
    l05 = _tone(1000, 350.0)
    make_file("l05_unused.wav", l05, onset_ms=350.0)

    # L06 / L07: one shared WAV, onset 200 ms.  Two independent conclusions.
    shared = _tone(1100, 200.0)
    shared_rel = make_file("shared_dialogue.wav", shared, onset_ms=200.0)

    # L08: three mouth time points; the earliest (250 ms) is the alignment basis.
    l08 = _tone(1100, 240.0)
    l08_rel = make_file("l08_multi_point.wav", l08, onset_ms=240.0)

    frames = {
        "frames": [
            {"line_id": "L01", "mouth_time_ms": 310},
            {"line_id": "L02", "mouth_time_ms": 505},
            {"line_id": "L03", "mouth_time_ms": 420},
            {"line_id": "L04", "mouth_time_ms": 210},
            {"line_id": "L05", "mouth_time_ms": 360},
            {"line_id": "L06", "mouth_time_ms": 188},
            {"line_id": "L07", "mouth_time_ms": 200},
            # Deliberately unsorted: 400 > 320 > 250.
            {"line_id": "L08", "mouth_time_ms": 400},
            {"line_id": "L08", "mouth_time_ms": 320},
            {"line_id": "L08", "mouth_time_ms": 250},
            # A frame entry with no dialogue counterpart -> reported, not fatal.
            {"line_id": "L99", "mouth_time_ms": 100},
        ]
    }

    dialogues = {
        "dialogues": [
            {"line_id": "L01", "wav_path": l01_rel, "text": "第一句，口型晚 10 毫秒"},
            {"line_id": "L02", "wav_path": l02_rel, "text": "第二句，口型晚 5 毫秒"},
            {"line_id": "L03", "wav_path": l03_rel, "text": "第三句，全零样本"},
            {"line_id": "L04", "wav_path": l04_rel, "text": "第四句，立体声"},
            # Intentionally missing wav_path -> pending problem.
            {"line_id": "L05", "text": "第五句，路径被删除"},
            {"line_id": "L06", "wav_path": shared_rel, "text": "第六句，共用录音，偏移 -12 毫秒"},
            {"line_id": "L07", "wav_path": shared_rel, "text": "第七句，共用录音，偏移 0"},
            {"line_id": "L08", "wav_path": l08_rel, "text": "第八句，多个口型时间点"},
        ]
    }

    (out_dir / "frames.json").write_text(
        json.dumps(frames, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "dialogues.json").write_text(
        json.dumps(dialogues, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    manifest = {
        "description": "可复现的业务样例：帧表 / 对白清单 / 含静音前缀的 44.1kHz 单声道 WAV",
        "sample_rate": SAMPLE_RATE,
        "tolerance_ms": 8,
        "files": generated,
        "expected": {
            "L01": {"status": "out_of_tolerance", "offset_ms": 10},
            "L02": {"status": "ok", "offset_ms": 5},
            "L03": {"status": "silent"},
            "L04": {"status": "unverifiable", "reason": "channels_not_mono"},
            "L05": {"status": "pending", "reason": "missing_wav_path"},
            "L06": {"status": "out_of_tolerance", "offset_ms": -12},
            "L07": {"status": "ok", "offset_ms": 0},
            "L08": {"status": "out_of_tolerance", "offset_ms": 10, "mouth_time_ms": 250},
            "summary": {
                "checked_lines": 5,
                "out_of_tolerance_lines": 3,
                "mean_offset_ms": 2.6,
                "max_offset_ms": -12,
            },
            "decoded_wav_files": 6,
        },
    }
    (out_dir / "expected.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成口型核对业务样例")
    parser.add_argument("--out", default="sample", help="输出目录（默认 sample）")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = build(out_dir)
    total = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    print(f"样例已生成: {out_dir.resolve()}")
    print(f"WAV 文件数: {len(manifest['files'])}, 数据总量: {total / 1024:.1f} KiB")
    print(f"帧表: {out_dir / 'frames.json'}")
    print(f"对白清单: {out_dir / 'dialogues.json'}")
    print(f"预期结果: {out_dir / 'expected.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
