#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可复现的端到端演示：生成样本 -> 运行 CLI -> 打印实际结果。

用法（在项目根目录执行）：
    python demo.py

演示会写入 demo_output/ 目录，包含：
* samples/track0.wav、track1.wav      确定性输入样本
* normal/out.wav、out_report.json      全有效区间
* out_of_range/...                    越界截断 + warning
* broken_track/...                    轨1不存在
* all_invalid/...                     全部条目无效（空 WAV）
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "demo_output"
SCRIPT = ROOT / "audio_merge.py"

CASES = {
    "normal": [
        {"track": 0, "start": 0.20, "end": 0.80, "fade": 0.05},
        {"track": 1, "start": 0.00, "end": 0.50},
        {"track": 0, "start": 1.00, "end": 1.40, "fade": 0.02},
    ],
    "out_of_range": [
        {"track": 0, "start": 0.50, "end": 5.00},
    ],
    "broken_track": [
        {"track": 0, "start": 0.10, "end": 0.60},
        {"track": 1, "start": 0.00, "end": 0.50},
    ],
    "all_invalid": [
        {"track": 5, "start": 0.0, "end": 0.5},
        {"track": 0, "start": 1.0, "end": 0.2},
    ],
}


def run_case(name: str, track0: Path, track1: Path) -> None:
    case_dir = OUT / name
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    intervals_path = case_dir / "intervals.json"
    intervals_path.write_text(
        json.dumps(CASES[name], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    command = [sys.executable, str(SCRIPT), str(track0), str(track1),
               str(intervals_path), str(case_dir)]
    print(f"\n=== {name} ===")
    print("$ " + " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    print(result.stdout.strip())
    if result.stderr.strip():
        print("[stderr]", result.stderr.strip())
    print(f"exit code: {result.returncode}")

    wav_path = case_dir / "out.wav"
    if wav_path.exists():
        with wave.open(str(wav_path), "rb") as wf:
            print(f"out.wav: {wf.getframerate()}Hz, "
                  f"{wf.getsampwidth() * 8}bit, {wf.getnchannels()}ch, "
                  f"{wf.getnframes()} frames "
                  f"({wf.getnframes() / wf.getframerate():.3f}s)")
    report_path = case_dir / "out_report.json"
    if report_path.exists():
        print("out_report.json:")
        print(report_path.read_text(encoding="utf-8").strip())


def main() -> int:
    OUT.mkdir(exist_ok=True)
    samples_dir = OUT / "samples"
    from make_samples import generate
    track0, track1 = generate(samples_dir)
    print(f"样本已生成: {track0}")
    print(f"样本已生成: {track1}")

    for name in CASES:
        track1_path = track1
        if name == "broken_track":
            track1_path = OUT / "samples" / "missing_track1.wav"
        run_case(name, track0, track1_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
